"""Bounded, environment-only synthetic workflow evaluation. No demo DB is used."""
import argparse,json,os,tempfile,time
from pathlib import Path
from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
from .workflow_cases import CASES,evaluate_case,diagnostic

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--case',action='append',choices=[c.id for c in CASES])
    p.add_argument('--list-cases',action='store_true')
    args=p.parse_args(argv);cases=[c for c in CASES if not args.case or c.id in args.case]
    if args.list_cases:print(json.dumps([vars(c) for c in cases],indent=2));return 0
    if args.output.exists():p.error('Report exists; choose a fresh output to preserve evidence.')
    report=dict(mode='live model; isolated synthetic workflows',planned_cases=[c.id for c in cases],questions=[],
        manual_review_status='pending',financial_prose_verified=False,status='pending')
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True);serialized=json.dumps(report,indent=2)
        key=os.environ.get('CASHFLOW_MODEL_API_KEY')
        if key:serialized=serialized.replace(key,'[redacted]')
        temporary=args.output.with_suffix('.tmp');temporary.write_text(serialized,encoding='utf-8');temporary.replace(args.output)
    if not os.environ.get('CASHFLOW_MODEL_API_KEY') or not os.environ.get('CASHFLOW_MODEL_NAME'):
        report['status']='pending: environment configuration absent; zero live calls';save();return 2
    try:adapter=OpenAICompatibleAdapter.from_env()
    except Exception:
        report['status']='failed: local provider configuration rejected; no raw exception exported'
        report['unattempted_cases']=[c.id for c in cases];save();return 1
    report['provider_label']=adapter.provider_label;report['request_policy']=adapter.request_policy()
    # Connectivity is tested by each real question; do not add a redundant ping.
    with tempfile.TemporaryDirectory(prefix='cashflow-live-workflows-') as folder:
        for i,case in enumerate(cases):
            report['status']='in progress';save()
            try:row=evaluate_case(case,adapter,Path(folder)/(case.id+'.sqlite'))
            except Exception:
                row=dict(id=case.id,automated_passed=False,http_status=None,outcome='local_evaluation_rejected',
                    safe_error='Local evaluation failed; no raw exception exported.',financial_prose_verified=False,
                    manual_review_status='pending',checks=[dict(name='valid_evaluation_result',passed=False)])
            report['questions'].append(row);save()
            print(f"{i+1}/{len(cases)} {case.id}: {'automated checks passed' if row['automated_passed'] else 'failed; review report'}",flush=True)
            if row['http_status'] in {401,402,403,429}:
                report['status']='stopped: provider rejected request; remaining cases unattempted';break
            if row.get('outcome')=='local_evaluation_rejected':
                report['status']='stopped: local evaluation failed; remaining cases unattempted';break
            if i+1<len(cases):time.sleep(2)
        else:report['status']='automated checks passed; commentary/refusal review pending' if all(r['automated_passed'] for r in report['questions']) else 'failed: one or more workflow checks'
    report['unattempted_cases']=[c.id for c in cases if c.id not in {r['id'] for r in report['questions']}]
    save();print(report['status']);return 0 if len(report['questions'])==len(cases) and all(r['automated_passed'] for r in report['questions']) else 1

if __name__=='__main__':raise SystemExit(main())
