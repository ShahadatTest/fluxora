"""Compare bounded environment-only live workflows; no automatic model selection."""
import argparse,json,math,os,tempfile,time
from decimal import Decimal
from pathlib import Path
from cashflow_copilot.model_adapter import OpenRouterAdapter
from .workflow_cases import CASES,evaluate_case

CANDIDATES=('openai/gpt-6-luna','z-ai/glm-5.3-flash','deepseek/deepseek-v4-flash')

class MeasuredAdapter(OpenRouterAdapter):
    """Keep only typed cost/token telemetry, never raw responses or reasoning."""
    def __init__(self,**kwargs):super().__init__(**kwargs);self.measurements=[]
    def _post(self,payload,**kwargs):
        started=time.monotonic()
        try:data=super()._post(payload,**kwargs)
        except Exception:
            self.measurements.append(dict(elapsed_seconds=round(time.monotonic()-started,3),response_received=False,reported_cost_usd=None));raise
        usage=data.get('usage') if isinstance(data,dict) else None
        cost=usage.get('cost') if isinstance(usage,dict) else None
        valid=type(cost) in {int,float} and math.isfinite(cost) and 0<=cost<=1000
        self.measurements.append(dict(elapsed_seconds=round(time.monotonic()-started,3),response_received=True,
            reported_cost_usd=str(Decimal(str(cost))) if valid else None))
        return data

def summary(rows,planned):
    samples=[m for r in rows for m in r.get('transport_measurements',[])]
    costs=[Decimal(m['reported_cost_usd']) for m in samples if m.get('reported_cost_usd') is not None]
    return dict(attempted=len(rows),planned=planned,automated_passed=sum(bool(r['automated_passed']) for r in rows),
        all_planned_automated_passed=len(rows)==planned and all(r['automated_passed'] for r in rows),
        elapsed_case_seconds=round(sum(r.get('elapsed_seconds',0) for r in rows),2),
        reported_cost_subtotal_usd=str(sum(costs,Decimal(0))) if costs else None,
        cost_coverage_complete=bool(samples) and len(costs)==len(samples) and all(m['response_received'] for m in samples),
        financial_prose_verified=False,note='Reported response costs only, not an invoice or spending cap. Missing/error/provider work after timeout may be unmeasured. Manual review required before choosing a default.')

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--model',action='append',choices=CANDIDATES)
    p.add_argument('--case',action='append',choices=[c.id for c in CASES])
    p.add_argument('--list-plan',action='store_true')
    args=p.parse_args(argv);models=args.model or CANDIDATES;cases=[c for c in CASES if not args.case or c.id in args.case]
    report=dict(mode='live model comparison; isolated synthetic workflows',models=[],planned_models=list(models),
        planned_cases=[c.id for c in cases],application_configuration_changed=False,financial_prose_verified=False,status='pending')
    if args.list_plan:print(json.dumps(report,indent=2));return 0
    if args.output.exists():p.error('Report exists; preserve it and choose a fresh output.')
    def save():
        text=json.dumps(report,indent=2);key=os.environ.get('CASHFLOW_MODEL_API_KEY')
        if key:text=text.replace(key,'[redacted]')
        args.output.parent.mkdir(parents=True,exist_ok=True);temp=args.output.with_suffix('.tmp')
        temp.write_text(text,encoding='utf-8');temp.replace(args.output)
    if not os.environ.get('CASHFLOW_MODEL_API_KEY') or os.environ.get('CASHFLOW_MODEL_BASE_URL','').rstrip('/')!='https://openrouter.ai/api/v1':
        report['status']='pending: existing OpenRouter environment required; zero calls';save();return 2
    report['status']='in progress';save()
    with tempfile.TemporaryDirectory(prefix='cashflow-comparison-') as folder:
        for mi,model in enumerate(models):
            entry=dict(model=model,cases=[],manual_review_status='pending');report['models'].append(entry);save();failures=0
            for case in cases:
                adapter=MeasuredAdapter(api_key=os.environ['CASHFLOW_MODEL_API_KEY'],base_url='https://openrouter.ai/api/v1',model=model,max_retries=0)
                entry['request_policy']=adapter.request_policy()
                try:row=evaluate_case(case,adapter,Path(folder)/(str(mi)+'-'+case.id+'.sqlite'))
                except Exception:row=dict(id=case.id,automated_passed=False,outcome='local_evaluation_rejected',http_status=None,safe_error='No raw exception exported')
                row['transport_measurements']=adapter.measurements;entry['cases'].append(row)
                entry['summary']=summary(entry['cases'],len(cases));save()
                print(model,case.id,'passed='+str(row['automated_passed']),flush=True)
                infrastructure=row.get('outcome') in {'failed','provider_error','timeout','local_evaluation_rejected'}
                failures=failures+1 if infrastructure else 0
                if row.get('http_status') in {401,402,403,429} or failures>=2:
                    entry['stopped']='Provider rejection or two consecutive incomplete runs; remaining cases unattempted';break
                time.sleep(2)
            entry['unattempted_cases']=[c.id for c in cases if c.id not in {r['id'] for r in entry['cases']}];save()
    report['status']='comparison complete; manual review and deliberate default selection required';save()
    print(report['status']);return 0

if __name__=='__main__':raise SystemExit(main())
