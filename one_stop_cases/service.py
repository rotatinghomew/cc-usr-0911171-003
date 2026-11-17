from __future__ import annotations
import datetime as dt
import uuid
from typing import Any, Mapping, Sequence
from .contracts import Event, fingerprint
from .storage import Database
from .orchestration.engine import WorkflowEngine

class ApplicationService:
    def __init__(self,database:Database):
        self.database=database; self.engine=WorkflowEngine()

    def process(self,payload:Mapping[str,Any],context:Mapping[str,Any],request_key:str) -> dict[str,Any]:
        digest=fingerprint({'payload':payload,'context':context})
        recalled=self.database.recalled('process',request_key,digest)
        if recalled is not None:return recalled
        # Executability is decided exactly as before; timeliness data is only
        # carried alongside it and never feeds back into the verdict above.
        result=self.engine.evaluate(payload,context)
        identifier=result.key
        response={'id':identifier,'accepted':result.accepted,'value':str(result.value),'reasons':list(result.reasons),'facts':dict(result.facts)}
        with self.database.transaction() as connection:
            record={'service_id':identifier,'owner_id':str(payload.get('owner_id','owner-default')),'category':'workflow','status':'active' if result.accepted else 'cancelled','effective_from':str(payload.get('effective_from',dt.date.today())),'effective_to':payload.get('effective_to'),'quantity':int(payload.get('demand',0)),'amount':str(payload.get('amount','0')),'attributes':response}
            version=self.database.put_record(connection,'service',record)
            self._store_case(connection,payload,result,response)
            event=Event(str(uuid.uuid4()),'service',identifier,'processed',dt.datetime.now(dt.timezone.utc),response,version)
            self.database.append_event(connection,event)
            self.database.enqueue(connection,'result:'+identifier,'workflow'+'.result',identifier,response)
            self.database.remember(connection,'process',request_key,digest,response)
        return response

    def _store_case(self,connection,payload:Mapping[str,Any],result,response:Mapping[str,Any]) -> None:
        """Record the accepted case plus its timeliness timeline verbatim.

        The record keeps exactly what the acceptance case supplied - opening and
        closing timestamps, the step timeline, target and due time, service
        version and acceptance batch - so the timeliness report can later
        recompute every attribution second.  Nothing is inferred here.
        """
        if not result.accepted:
            # Only accepted cases are supervised for timeliness; a rejected
            # submission never became an acceptance case.
            return
        detail=dict(payload.get('timeliness') or {}) if isinstance(payload.get('timeliness'),Mapping) else {}
        for field in ('opened_at','accepted_at','closed_at','target_seconds','due_at','outcome','service_version','batch_id','acceptance_batch','timeline','excluded_categories','work_periods','holidays'):
            if field in payload and field not in detail:
                detail[field]=payload[field]
        if not any(detail.get(field) not in (None,'',(),[]) for field in ('opened_at','accepted_at','closed_at','target_seconds','due_at','timeline')):
            # Nothing time-related was supplied, so there is no attribution
            # evidence to preserve; the run result stays exactly as before.
            return
        steps=[]
        for step in payload.get('steps') or ():
            if isinstance(step,Mapping):
                steps.append({'id':step.get('id'),'requires':list(step.get('requires') or ()),'materials':list(step.get('materials') or ())})
            elif step is not None:
                steps.append({'id':str(step),'requires':[],'materials':[]})
        if not steps:
            return
        detail.setdefault('steps',steps)
        detail.setdefault('completed',list(payload.get('completed') or ()))
        detail.setdefault('timeline',payload.get('timeline') or ())
        record={'case_id':str(result.key),'owner_id':str(payload.get('owner_id','owner-default')),'category':str(payload.get('category','workflow')),'status':'completed' if payload.get('closed_at') else 'active','effective_from':str(payload.get('opened_at') or payload.get('effective_from') or dt.date.today()),'effective_to':payload.get('closed_at'),'quantity':int(payload.get('demand',0)),'amount':str(payload.get('amount','0')),'attributes':{'executability':dict(response),'timeliness':detail}}
        self.database.put_record(connection,'case',record)

    def batch(self,subjects:Sequence[Mapping[str,Any]],context:Mapping[str,Any]) -> dict[str,Any]:
        results=self.engine.rank(subjects,context)
        return {'summary':self.engine.summarize(results),'results':[{'id':r.key,'accepted':r.accepted,'value':str(r.value),'reasons':list(r.reasons)} for r in results]}
