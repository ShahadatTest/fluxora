"""Environment-only live evaluation on synthetic ledgers; never certify prose."""
import argparse,json,os,time
from pathlib import Path
from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
from .live_cases import CASES,evaluate


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('work/live-evaluation.json'))
    parser.add_argument('--case',action='append',choices=[c.id for c in CASES],dest='case_ids',help='Limit calls to selected cases; repeat to choose several.')
    parser.add_argument('--list-cases',action='store_true',help='List the plan without making any provider call.')
    parser.add_argument('--data-mode',choices=['minimized','full_metadata'],default='minimized',help='Match the normal UI default, or explicitly evaluate full structured metadata.')
    parser.add_argument('--interval-seconds',type=float,default=2,help='Pause between cases (0–60 seconds). Failed cases are retained, never silently retried.')
    args=parser.parse_args(argv);cases=[c for c in CASES if not args.case_ids or c.id in args.case_ids]
    if not 0 <= args.interval_seconds <= 60:parser.error('--interval-seconds must be between 0 and 60')
    if args.list_cases:
        print(json.dumps([dict(id=c.id,question=c.question,required_tools=c.tools,manual_review=c.manual_review) for c in cases],indent=2));return 0
    present={n:bool(os.environ.get(n)) for n in ('CASHFLOW_MODEL_API_KEY','CASHFLOW_MODEL_BASE_URL','CASHFLOW_MODEL_NAME')}
    report=dict(mode='live-model evaluation',data_mode=args.data_mode,environment_present=present,financial_prose_verified=False,manual_review_status='pending',planned_cases=[c.id for c in cases],questions=[])
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        serialized=json.dumps(report,indent=2)
        key=os.environ.get('CASHFLOW_MODEL_API_KEY')
        if key:serialized=serialized.replace(key,'[redacted]')
        temporary=args.output.with_suffix(args.output.suffix+'.tmp')
        temporary.write_text(serialized,encoding='utf-8');temporary.replace(args.output)
    def checkpoint(row):
        row['tool_smoke_passed']=row['automated_passed']
        report['questions'].append(row);save()
        print(f"{len(report['questions'])}/{len(cases)} {row['id']}: {'automated checks passed' if row['automated_passed'] else 'failed; review report'}",flush=True)
        if len(report['questions'])<len(cases):time.sleep(args.interval_seconds)
    code=2
    if not present['CASHFLOW_MODEL_API_KEY'] or not present['CASHFLOW_MODEL_NAME']:
        report['status']='pending: API key and model must be set locally; zero live calls'
    else:
        adapter=OpenAICompatibleAdapter.from_env()
        report['provider_label']=adapter.provider_label
        report['request_policy']=adapter.request_policy()
        status=adapter.verify();report['connectivity_state']=status.state
        if status.state!='verified':report['status']='failed: provider connectivity verification';code=1
        else:
            report['status']='in progress: completed cases checkpointed; manual review pending';save()
            evaluate(lambda _:adapter,cases,mode='live',data_mode=args.data_mode,on_result=checkpoint)
            passed=all(row['automated_passed'] for row in report['questions'])
            report['status']='automated checks passed; manual commentary/refusal review pending' if passed else 'failed: one or more automated reference/safety checks'
            code=0 if passed else 1
    save()
    print(report['status']);print('Report:',args.output);return code


if __name__=='__main__':raise SystemExit(main())
