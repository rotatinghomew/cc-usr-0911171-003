from __future__ import annotations
import csv
import io
import json
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence
from .audit import AuditVerifier
from .storage import Database
from .timeliness import TimelinessAnalyzer, analyze_records

class Reporter:
    def __init__(self,database:Database): self.database=database
    def overview(self) -> dict[str,Any]:
        counts=self.database.counts(); pending=self.database.pending_messages()
        return {'counts':counts,'total_records':sum(counts.values()),'pending_messages':len(pending),'topics':dict(Counter(item['topic'] for item in pending))}
    def owner_breakdown(self,kind:str) -> dict[str,Any]:
        groups=defaultdict(list)
        for record in self.database.list_records(kind): groups[record['owner_id']].append(record)
        result={}
        for owner,rows in sorted(groups.items()):
            result[owner]={'count':len(rows),'quantity':sum(int(row['quantity']) for row in rows),'amount':sum(float(row['amount']) for row in rows),'states':dict(Counter(row['status'] for row in rows))}
        return result
    def export_json(self) -> str:
        records=[]
        records.extend(self.database.list_records('service'))
        records.extend(self.database.list_records('step'))
        records.extend(self.database.list_records('case'))
        records.extend(self.database.list_records('material'))
        records.extend(self.database.list_records('execution'))
        records.extend(self.database.list_records('deadline'))
        records.extend(self.database.list_records('outbox'))
        records.extend(self.database.list_records('compensation'))
        manifest=AuditVerifier().manifest(records)
        return json.dumps({'overview':self.overview(),'records':records,'manifest':manifest},ensure_ascii=False,sort_keys=True,indent=2,default=str)
    def export_csv(self,kind:str) -> str:
        rows=self.database.list_records(kind)
        output=io.StringIO(); fields=['kind',f'{kind}_id','owner_id','category','status','effective_from','effective_to','quantity','amount','version']
        writer=csv.DictWriter(output,fieldnames=fields,extrasaction='ignore'); writer.writeheader(); writer.writerows(rows); return output.getvalue()
    def quality(self,kind:str) -> dict[str,Any]:
        rows=self.database.list_records(kind); issues=[]
        for row in rows:
            if not row['owner_id']: issues.append({'id':row[f'{kind}_id'],'code':'missing_owner'})
            if row['effective_to'] and row['effective_to']<row['effective_from']: issues.append({'id':row[f'{kind}_id'],'code':'reversed_period'})
            if int(row['quantity'])<0: issues.append({'id':row[f'{kind}_id'],'code':'negative_quantity'})
        return {'kind':kind,'records':len(rows),'issues':issues,'valid':not issues}

    def timeliness(self,as_of=None,minimum_bottleneck_cases:int=1) -> dict[str,Any]:
        """Attribution summary grouped by step, service version and batch.

        Uses the timeline stored on case records; when records were written
        without a timeline the cases are reported as evidence insufficient
        rather than assumed on time.
        """
        from .timeliness import summarize
        report=analyze_records(self.database.list_records('case'),as_of=as_of)
        payload=report.as_dict(include_segments=False)
        payload['summary']=summarize(report.cases,minimum_bottleneck_cases)
        payload['summary']['minimum_bottleneck_cases']=minimum_bottleneck_cases
        return payload

    def timeliness_case(self,case_id:str,as_of=None) -> dict[str,Any]:
        """Full calculation basis for one case, including every segment.

        Raises ``NotFoundError`` when the case record does not exist so a
        supervisor cannot mistake a missing record for a compliant one.
        """
        record=self.database.get_record('case',case_id)
        report=TimelinessAnalyzer().analyze_records([record],as_of=as_of)
        return report.cases[0].as_dict()
