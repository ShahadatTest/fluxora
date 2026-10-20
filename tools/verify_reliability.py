"""Run bounded English robustness cases on isolated synthetic databases."""
import argparse
import json
import os
import tempfile
from pathlib import Path
from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
from .reliability_cases import CASES, evaluate


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--case',action='append',choices=[c.id for c in CASES])
    args=parser.parse_args(argv)
    if args.output.exists():
        parser.error('Preserve prior evidence; choose a new report path.')
    cases=[c for c in CASES if not args.case or c.id in args.case]
    report=dict(mode='live provider, English synthetic robustness cases; direct runner, not browser or human test',
                planned=[c.id for c in cases],questions=[],status='pending',financial_prose_verified=False,
                independent_human_participants=0)
    def save():
        args.output.parent.mkdir(parents=True,exist_ok=True)
        serialized=json.dumps(report,indent=2)
        key=os.environ.get('CASHFLOW_MODEL_API_KEY')
        if key:serialized=serialized.replace(key,'[redacted]')
        temporary=args.output.with_suffix('.tmp')
        temporary.write_text(serialized,encoding='utf-8');temporary.replace(args.output)
    if not all(os.environ.get(n) for n in ('CASHFLOW_MODEL_API_KEY','CASHFLOW_MODEL_BASE_URL','CASHFLOW_MODEL_NAME')):
        report['status']='pending: environment configuration absent; zero live calls';save();return 2
    try:adapter=OpenAICompatibleAdapter.from_env()
    except Exception:
        report['status']='failed: local configuration rejected; no raw exception exported';save();return 1
    report['provider']=adapter.provider_label
    report['request_policy']=adapter.request_policy()
    consecutive_failures=0
    with tempfile.TemporaryDirectory(prefix='cashflow-reliability-') as folder:
        for case in cases:
            report['status']='in progress';save()
            try:row=evaluate(case,adapter,Path(folder)/(case.id+'.sqlite'))
            except Exception:
                row=dict(id=case.id,automated_passed=False,request_status='evaluation_rejected',http_status=None,
                         error='Local evaluation rejected; raw exception not exported.',financial_prose_verified=False)
            report['questions'].append(row);save()
            print(case.id+': '+('automated checks passed' if row['automated_passed'] else 'failed; retained in report'),flush=True)
            consecutive_failures=0 if row['request_status']=='completed' else consecutive_failures+1
            if row.get('retry_after_seconds',0) or row.get('http_status') in {401,402,403,429} or consecutive_failures>=3 or row['request_status']=='evaluation_rejected':
                report['status']='stopped: provider/evaluation failure; remaining cases unattempted';break
        else:
            report['status']='automated checks passed; manual prose review pending' if all(r['automated_passed'] for r in report['questions']) else 'failed: inspect retained cases'
    attempted={r['id'] for r in report['questions']}
    report['unattempted']=[c.id for c in cases if c.id not in attempted]
    save()
    return 0 if not report['unattempted'] and all(r['automated_passed'] for r in report['questions']) else 1


if __name__=='__main__':raise SystemExit(main())
