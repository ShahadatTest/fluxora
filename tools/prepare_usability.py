"""Prepare new isolated synthetic session files. Never replace an existing folder."""
import argparse
import json
from pathlib import Path

from cashflow_copilot.authz import owner_context_for
from cashflow_copilot.exploration import save_scenario
from cashflow_copilot.fixture import validate_fixture
from cashflow_copilot.funding import assign_forecast_route,account_funding
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.owner_workflows import review_revision
from cashflow_copilot.statement_mapping import DEFAULT_MAPPING,normalize_statement,import_mapped_statement
from cashflow_copilot.store import SQLiteStore
from cashflow_copilot import treasury as t


def prepare(output):
    output=Path(output).resolve()
    if output.exists():raise ValueError('Choose a NEW session folder. Existing files and participant observations are never overwritten.')
    examples=Path(__file__).resolve().parents[1]/'examples'
    data=json.loads((examples/'synthetic-treasury.json').read_text(encoding='utf-8'))
    validate_fixture(data);ledger=Ledger.from_fixture(data);ctx=owner_context_for(ledger.business_id)
    def call(fn,*args):return fn(ledger,*args,'src','Invented source assertion for an isolated synthetic human task session.',review_revision(ledger),ctx=ctx)
    call(t.register_party,'customer-one','customer','Example Ltd')
    call(t.update_party,'customer-one','alias','Example Trading')
    for ident in ('a','b'):call(t.link_party,'invoice',ident,'customer-one')
    call(t.add_account,'usd-savings','USD','Payroll savings')
    t.observed_transfer(ledger,'own-transfer','usd','usd-savings',3000,ledger.as_of,'src','Invented already-observed own transfer for this test session.',review_revision(ledger),ctx=ctx)
    call(t.restrict_cash,'held-deposit','usd',1000,'restricted_deposit')
    for kind,group in [('invoice',ledger.receivables),('bill',ledger.outflows)]:
        for item in group.values():call(assign_forecast_route,kind,item.id,'usd-savings' if item.id=='payroll' else 'usd')
    save_scenario(ledger,'Partial and missed collection',[],[],review_revision(ledger),ctx=ctx,
        collections=[dict(invoice_id='a',percent=50),dict(invoice_id='b',percent=0)])
    normal=normalize_statement(ledger,b'id,date,currency,amount,reference\n',DEFAULT_MAPPING,'usd','2027-01-01','2027-01-31',ctx=ctx)
    import_mapped_statement(ledger,normal,'Invented zero-activity source for explicit period review.','Standard synthetic statement',review_revision(ledger),ctx=ctx)
    funding=account_funding(ledger,ctx=ctx)
    baseline=Ledger.seed();baseline.apply_event('evt-delay-001',ctx=owner_context_for(baseline.business_id))
    output.mkdir(parents=True,exist_ok=False)
    for name,state in [('baseline',baseline),('treasury',ledger)]:
        s=SQLiteStore(output/(name+'.sqlite'))
        try:s.save_ledger(state)
        finally:s.close()
    report=dict(mode='session preparation only; not a human study',human_status='pending',participant_count=0,
        live_model_calls=0,databases=['baseline.sqlite','treasury.sqlite'],observations=[],
        expected_treasury=dict(business_date=str(ledger.as_of),usd_book_minor=ledger.actual_cash('USD'),usd_available_minor=99000,
            eur_book_minor=ledger.actual_cash('EUR'),payroll_account='usd-savings',payroll_shortfall_minor=2000,
            sources=['src'],funding_complete=funding['complete']))
    (output/'session-preparation.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    (output/'OBSERVATIONS.md').write_text('''# Fresh human walkthrough - blank observations

Status: PENDING. Actual participants: 0. Preparation/automation is not a user session.

Participant code: ____  Observer: ____  Date: ____  App version: ____
Device/viewport: ____  Bookkeeping familiarity: ____  Prior app involvement: ____

Use a person who did not build the app. Use synthetic data only. Do not collect company/bank/identity/credential data. The observer records help and exact words; do not lead the participant.

| Task | Outcome (unassisted / assisted / failed / skipped) | Time | Hints and errors | Exact words |
| --- | --- | --- | --- | --- |
| Find snapshot date, separate currencies and main issue | | | | |
| Trace one number to source evidence | | | | |
| Compare plan and distinguish payroll protection / consent / draft | | | | |
| Ask labelled mock via data preview; explain commentary vs tool result | | | | |
| Reopen saved request and refresh without duplicate effects | | | | |
| Explain book vs restricted vs available cash | | | | |
| Find payroll-account deficit despite sufficient pooled USD | | | | |
| Compare partial/zero collection and explain unchanged claims | | | | |
| Load statement mapping profile; explain account/period and zero activity | | | | |
| Find a week's daily minimum and trace a driver | | | | |
| Read Decision Brief and separate proposal/approval/acceptance/observed/reconciled | | | | |
| Recover from a validation error and navigate by keyboard | | | | |

What was hardest to find? ____  Which figure did you trust, and why? ____
What did you expect the app to do after confirmation? ____
Ease (1 very difficult to 5 very easy): ____  Reason: ____
Critical misunderstandings (cash/consent/currency/source truth): ____
Fix required: ____  Follow-up task/result (do not erase original): ____
''',encoding='utf-8')
    (output/'START-SESSION.md').write_text('''# Isolated session preparation

No human session has occurred. Keep OBSERVATIONS.md blank until a real participant acts. These local databases are synthetic and disposable; do not reset/restore another active demo. Use a separate fresh session folder for each participant.

From the source directory, start one prepared database on a free loopback port:

`python -m cashflow_copilot serve --db PATH_TO_THIS_FOLDER/baseline.sqlite --port 8824`

The baseline has the labelled delayed-client event applied. Open /review and /usability. Use the original scripted mock questions only; it is not a live model. Ask Agent now requires a data preview and explicit confirmation in the normal UI. Inspect the separate deterministic result and saved-request link.

For current treasury/scenario/mapping tasks use the other prepared database:

`python -m cashflow_copilot serve --db PATH_TO_THIS_FOLDER/treasury.sqlite --port 8825`

Treasury has USD 1,000.00 book cash, USD 10.00 restricted and USD 990.00 available; EUR 50.00 stays separate. USD 30.00 was synthetically observed in the payroll account; protected payroll is USD 50.00. Pooled USD floors hold while the payroll account is USD 20.00 short. No balancing transfer is assumed. Invoice a is USD 100.00 and b USD 200.00; saved 50%/0% stress creates no receipt or write-off. The mapped statement is a declared January zero-activity source, not a verified bank balance. Use /statement-balances for a separate boundary check; no opening/closing was invented.

Do not give this answer sheet to the participant before tasks. Use the neutral observation tasks and docs/USABILITY-SESSION.md. Ask them to think aloud; record assistance/time/errors exactly. Choose a short subset first and record skips honestly. Bank/authenticity, live provider, market validity and full accessibility remain untested. No external invitation or message is sent by this preparation tool.
''',encoding='utf-8')
    return report


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True,type=Path)
    args=p.parse_args(argv)
    try:report=prepare(args.output)
    except ValueError as exc:p.error(str(exc))
    print('Prepared two NEW isolated synthetic databases and blank observations. Actual participants:',report['participant_count'])
    return 0


if __name__=='__main__':raise SystemExit(main())
