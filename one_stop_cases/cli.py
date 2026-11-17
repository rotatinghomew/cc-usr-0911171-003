from __future__ import annotations
import argparse
import json
from pathlib import Path
from .reporting import Reporter
from .service import ApplicationService
from .storage import Database
from .timeliness import TimelinessAnalyzer, analyze_document

def load_json(path:str):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def main(argv=None):
    parser=argparse.ArgumentParser(prog='one_stop_cases')
    parser.add_argument('--database',default=':memory:')
    sub=parser.add_subparsers(dest='command',required=True)
    run=sub.add_parser('run'); run.add_argument('--fixture',required=True); run.add_argument('--output',required=True)
    report=sub.add_parser('report'); report.add_argument('--kind'); report.add_argument('--timeliness',action='store_true'); report.add_argument('--as-of')
    timeliness=sub.add_parser('timeliness'); timeliness.add_argument('--fixture',required=True); timeliness.add_argument('--case'); timeliness.add_argument('--as-of'); timeliness.add_argument('--output')
    args=parser.parse_args(argv); database=Database(args.database)
    if args.command=='run':
        fixture=load_json(args.fixture); service=ApplicationService(database); outputs=[]
        for index,item in enumerate(fixture.get('items',[])):
            outputs.append(service.process(item,fixture.get('context',{}),item.get('request_key',f'item-{index}')))
        payload={'product':'人才服务一网通办事项编排与补偿平台','processed':len(outputs),'outputs':outputs,'report':Reporter(database).overview()}
        Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(payload,ensure_ascii=False,sort_keys=True)); return 0
    if args.command=='timeliness':
        fixture=load_json(args.fixture)
        report=TimelinessAnalyzer().analyze(fixture,as_of=args.as_of)
        payload=report.case(args.case).as_dict() if args.case else report.as_dict(include_segments=False)
        text=json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2,default=str)
        if args.output:
            Path(args.output).parent.mkdir(parents=True,exist_ok=True); Path(args.output).write_text(text,encoding='utf-8')
        print(text); return 0
    if args.timeliness:
        payload=Reporter(database).timeliness(as_of=args.as_of)
    else:
        payload=Reporter(database).owner_breakdown(args.kind) if args.kind else Reporter(database).overview()
    print(json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str)); return 0
