"""Qualify the environment-configured model on synthetic guided tasks."""
import argparse,json
from pathlib import Path
from cashflow_copilot.model_adapter import OpenAICompatibleAdapter
from cashflow_copilot.task_qualification import qualify

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--list-plan',action='store_true')
    a=p.parse_args()
    from cashflow_copilot.task_qualification import CASES
    if a.list_plan:
        print(json.dumps(dict(cases=[c[0] for c in CASES],external_calls=0)));return 0
    if a.output.exists():p.error('Preserve the report. Choose a fresh output path.')
    adapter=OpenAICompatibleAdapter.from_env()
    if adapter.status().state=='disconnected':
        print('Pending: configure the three CASHFLOW_MODEL_* environment variables locally. No call made.');return 2
    result=qualify(adapter,a.output)
    print(json.dumps(dict(status=result['status'],passed=sum(r['passed'] for r in result['cases']),attempted=len(result['cases']),planned=result['planned'])))
    return 0 if result['status']=='qualified' else 1

if __name__=='__main__':raise SystemExit(main())
