from __future__ import annotations
import datetime as dt
from collections import defaultdict,deque
class CaseOrchestrator:
    def graph(self,steps):
        nodes={str(s['id']):dict(s) for s in steps};children=defaultdict(set)
        for node in nodes.values():
            for dep in node.get('requires',[]):
                if dep not in nodes:raise ValueError('unknown_dependency:'+str(dep))
                children[str(dep)].add(str(node['id']))
        return nodes,children
    def topological(self,steps):
        nodes,children=self.graph(steps);indegree={k:0 for k in nodes}
        for node in nodes.values():
            for dep in node.get('requires',[]):indegree[str(node['id'])]+=1
        queue=deque(sorted(k for k,v in indegree.items() if v==0));order=[]
        while queue:
            current=queue.popleft();order.append(current)
            for child in sorted(children[current]):
                indegree[child]-=1
                if indegree[child]==0:queue.append(child)
        if len(order)!=len(nodes):raise ValueError('cyclic_workflow')
        return order
    def material_gaps(self,case,steps):
        supplied={str(x['kind']) for x in case.get('materials',[]) if x.get('verified')};gaps={}
        for step in steps:
            missing=sorted(set(step.get('materials',()))-supplied)
            if missing:gaps[str(step['id'])]=missing
        return gaps
    def available(self,case,steps):
        completed=set(case.get('completed',()));failed=set(case.get('failed',()));gaps=self.material_gaps(case,steps);available=[]
        for step in steps:
            sid=str(step['id'])
            if sid in completed or sid in failed or sid in gaps:continue
            if all(dep in completed for dep in step.get('requires',())):available.append(sid)
        return sorted(available)
    def deadline(self,opened_at,business_hours,holidays):
        remaining=business_hours;cursor=opened_at;holiday_dates={dt.date.fromisoformat(x) for x in holidays}
        while remaining>0:
            cursor+=dt.timedelta(hours=1)
            if cursor.weekday()<5 and cursor.date() not in holiday_dates and 9<=cursor.hour<17:remaining-=1
        return cursor
    def compensation_plan(self,completed,steps):
        nodes={str(x['id']):x for x in steps};plan=[]
        for sid in reversed(list(completed)):
            action=nodes.get(sid,{}).get('compensate')
            if action:plan.append({'step':sid,'action':str(action)})
        return plan
    def evaluate(self,item,context):
        steps=item.get('steps') or context.get('steps',());return {'order':self.topological(steps),'available':self.available(item,steps),'material_gaps':self.material_gaps(item,steps),'progress':len(item.get('completed',()))/max(1,len(steps)),'compensation':self.compensation_plan(item.get('completed',()),steps)}
