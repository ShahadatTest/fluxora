"""Recorded payment compensation, disputed collections and literal financial exports."""
import copy
import csv
from datetime import date
from io import BytesIO, StringIO
import json
import urllib.request
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from concurrent.futures import ThreadPoolExecutor

import pytest

from cashflow_copilot import web, recovery
from cashflow_copilot.accounting import correct_bill_payment, set_collection_hold, save_forecast, compare_forecast
from cashflow_copilot.adaptive import generate_proposals, validate_current_request
from cashflow_copilot.agent import TOOLS, execute_tool, ToolRejectedError, ToolValidationError
from cashflow_copilot.attention import attention_items
from cashflow_copilot.errors import CashflowError, AuthorizationError
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account, Plan, ProjectedReceipt, Receivable
from cashflow_copilot.operations import collection_priorities
from cashflow_copilot.owner_workflows import review_revision, daily_preview
from cashflow_copilot.planning import plan_draft_preview, stress_test
from cashflow_copilot.reports import report_tables, xlsx_report, csv_report, csv_archive, csv_text, Money
from cashflow_copilot.settlement import cash_events, bill_payment_entries, effective_bill_payment, bill_schedule
from tests.test_adaptive_priorities import own
from tests.test_owner_workflows import ctx, save, receipt, request
from tests.test_accounting_recovery import payment
from tests.test_web import live_server, _get, _post, _load


def correct(ledger,amount=15000,day=None,revision=None,caller=None,pid='observed-p1',bill='materials'):
    return correct_bill_payment(ledger,bill,pid,amount,day or ledger.as_of,'supplier-source','Original synthetic payment source checked.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


def hold(ledger,active=True,invoice='custom-x',revision=None,caller=None):
    return set_collection_hold(ledger,invoice,active,'customer-x','Synthetic dispute source / resolution checked.',
        revision or review_revision(ledger),ctx=caller or ctx(ledger))


def test_correct_payment_immutable_original_signed_cash_residual_and_restart_replay(own,tmp_path):
    payment(own);original=copy.deepcopy(own.outflows['materials'].observed_payments)
    rev=review_revision(own);result=correct(own,revision=rev)
    out=own.outflows['materials']
    assert out.observed_payments==original and out.paid_minor==15000 and out.remaining_minor==35000
    assert own.actual_cash('USD')==85000
    assert result['cash_delta_minor']==5000 and bill_schedule(out)==[(date(2027,1,7),35000)]
    assert effective_bill_payment(out,'observed-p1')['amount_minor']==15000
    rows=forecast(own,own.as_of,30).currencies['USD'].days
    assert rows[0].outflow_minor==20000 and rows[0].inflow_minor==5000 and rows[6].outflow_minor==35000
    db=str(tmp_path/'correct.sqlite');save(db,own);loaded=_load(db);before=copy.deepcopy(vars(loaded))
    assert correct(loaded,revision=rev)==dict(result,duplicate=True)
    assert payment(loaded)['duplicate'] and vars(loaded)==before
    with pytest.raises(AuthorizationError):correct(loaded,revision=rev,caller=ctx(loaded,'agent'))


def test_reversal_then_new_payment_preserves_both_ids_and_original_terms(own):
    payment(own,50000);out=own.outflows['materials'];assert out.status=='paid'
    correct(own,0)
    assert out.status=='scheduled' and out.paid_date is None and out.paid_minor==0 and out.remaining_minor==50000
    assert own.actual_cash('USD')==100000 and len(out.observed_payments)==1
    payment(own,5000,pid='new-p2')
    assert out.paid_minor==5000 and out.remaining_minor==45000 and own.actual_cash('USD')==95000
    assert out.amount_minor==50000 and out.date==date(2027,1,7) and not own.consent_map and not own.receipts


def test_legacy_paid_reversal_repayment_and_original_mark_paid_retry_do_not_repay(own,tmp_path):
    own.mark_paid('staff',own.as_of,'owner','pay',ctx=ctx(own));out=own.outflows['staff']
    assert own.actual_cash('USD')==30000
    correct(own,0,bill='staff',pid='legacy-paid-staff')
    assert out.protected and out.status=='scheduled' and out.paid_minor==0
    assert own.actual_cash('USD')==100000 and out.legacy_payment['amount_minor']==70000
    before=copy.deepcopy(vars(own));assert own.mark_paid('staff',own.as_of,'owner','pay',ctx=ctx(own))['duplicate']
    assert vars(own)==before
    payment(own,10000,pid='new-payroll',bill='staff')
    assert out.paid_minor==10000 and own.actual_cash('USD')==90000
    own.mark_paid('staff',date(2027,1,2),'owner','pay',ctx=ctx(own))
    assert out.paid_minor==70000 and out.remaining_minor==0
    db=str(tmp_path/'legacy.sqlite');save(db,own);loaded=_load(db)
    assert loaded.outflows['staff'].legacy_payment==out.legacy_payment
    assert loaded.actual_cash('USD',date(2027,1,2))==30000


def test_absorbed_payment_correction_is_dated_once_and_variance_comparison_includes_it(own):
    sid=save_forecast(own,'Before observations',review_revision(own),ctx=ctx(own))['snapshot_id']
    payment(own);own.roll_forward(date(2027,1,2),ctx=ctx(own))
    assert own.accounts['operating'].opening_minor==80000
    correct(own,15000)
    assert own.accounts['operating'].opening_minor==80000 and own.actual_cash('USD')==85000
    preview=daily_preview(own,date(2027,1,3),ctx=ctx(own))
    assert preview['accounts'][0]['observed_inflow_minor']==0 and preview['accounts'][0]['observed_outflow_minor']==-5000
    report=compare_forecast(own,sid,date(2027,1,2),ctx=ctx(own))
    assert report['currencies']['USD'][-1]['bill_payment_corrections_minor']==5000
    assert report['currencies']['USD'][-1]['recorded_closing_minor']==85000
    own.roll_forward(date(2027,1,3),ctx=ctx(own));assert own.actual_cash('USD')==85000
    assert forecast(own,own.as_of,30).currencies['USD'].days[4].outflow_minor==35000


def test_correction_cannot_overpay_other_observations_and_original_retry_never_revives_correction(own):
    payment(own);payment(own,10000,pid='p2')
    with pytest.raises(CashflowError):correct(own,40001)
    correct(own,40000)
    assert own.outflows['materials'].paid_minor==50000 and own.outflows['materials'].status=='paid'
    assert own.actual_cash('USD')==50000
    assert payment(own)['duplicate']
    assert own.outflows['materials'].paid_minor==50000


def test_later_payment_cannot_use_remaining_capacity_before_its_correction_date(own):
    payment(own);correct(own,10000,day=date(2027,1,3))
    with pytest.raises(CashflowError):payment(own,5000,pid='too-early',day=date(2027,1,2))
    payment(own,5000,pid='dated-after',day=date(2027,1,3))
    assert own.outflows['materials'].paid_minor==15000


@pytest.mark.parametrize('bad',['negative','bool','overpay','unchanged','unknown_bill','unknown_payment','past','before_prior','source','note','stale','agent','viewer','business'])
def test_bad_payment_correction_is_atomic(own,bad):
    payment(own)
    args=['materials','observed-p1',15000,own.as_of,'supplier-source','Checked source.',review_revision(own)];caller=ctx(own)
    if bad=='negative':args[2]=-1
    if bad=='bool':args[2]=False
    if bad=='overpay':args[2]=50001
    if bad=='unchanged':args[2]=20000
    if bad=='unknown_bill':args[0]='missing'
    if bad=='unknown_payment':args[1]='missing'
    if bad=='past':args[3]=date(2026,12,31)
    if bad=='before_prior':correct(own,10000,day=date(2027,1,3));args[-1]=review_revision(own);args[3]=date(2027,1,2)
    if bad=='source':args[4]='missing'
    if bad=='note':args[5]=' '
    if bad=='stale':args[-1]='old'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='different')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):correct_bill_payment(own,*args,ctx=caller)
    assert vars(own)==before


def test_hold_keeps_claim_receipt_history_dates_cash_and_excludes_only_remaining_future_collection(own):
    receipt(own);original=copy.deepcopy(own.receivables['custom-x']);cash=own.actual_cash('USD')
    hold(own)
    rec=own.receivables['custom-x']
    assert rec.original_minor==original.original_minor and rec.allocations==original.allocations
    assert rec.remaining_minor==55000 and rec.expected_date==original.expected_date and own.actual_cash('USD')==cash
    fc=forecast(own,own.as_of,30)
    assert fc.currencies['USD'].days[11].inflow_minor==0
    assert fc.currencies['USD'].days[0].inflow_minor==10000
    assert any(c['code']=='collection_on_hold' for c in fc.clarifications)
    assert all(d.get('receivable_id')!='custom-x' for p in generate_proposals(own,ctx=ctx(own)) for d in p.drafts)
    assert any(i['code']=='collection_hold' for i in attention_items(own,ctx=ctx(own)))
    row=next(r for r in collection_priorities(own,ctx=ctx(own))['currencies']['USD']['invoices'] if r['id']=='custom-x')
    assert row['collection_held'] and row['gap_reduction_minor']==0
    receipt(own,rid='while-held',amount=1000)
    assert own.actual_cash('USD')==cash+1000 and rec.remaining_minor==54000


def test_hold_blocks_projected_plan_manual_approval_model_draft_and_stress(own):
    action=request(own);hold(own)
    with pytest.raises(CashflowError):validate_current_request(own,action)
    with pytest.raises(ValueError):forecast(own,own.as_of,30,Plan('p','p','p',projected_receipts=[ProjectedReceipt('custom-x',1000,own.as_of,'assumed')]))
    with pytest.raises(CashflowError):stress_test(own,'custom-x',7,ctx=ctx(own))
    with pytest.raises(ToolValidationError):execute_tool(ctx(own,'agent'),own,'draft_action',dict(action_id='held-draft',kind='request_early_payment',receivable_id='custom-x',amount_minor=1000,date='2027-01-02'))
    hold(own,invoice='custom-y')
    assert not any(p.financially_sufficient for p in generate_proposals(own,ctx=ctx(own)))


def test_hold_release_restart_and_old_retries_preserve_latest_status(own,tmp_path):
    rev=review_revision(own);hold(own,revision=rev)
    release_rev=review_revision(own);hold(own,False,revision=release_rev)
    db=str(tmp_path/'holds.sqlite');save(db,own);loaded=_load(db);before=copy.deepcopy(vars(loaded))
    assert hold(loaded,revision=rev)['duplicate'] and hold(loaded,False,revision=release_rev)['duplicate']
    assert vars(loaded)==before and loaded.receivables['custom-x'].collection_hold['active'] is False
    assert forecast(loaded,loaded.as_of,30).currencies['USD'].days[11].inflow_minor==65000
    assert len([v for v in loaded.owner_operations.values() if v['kind']=='collection_hold_changed'])==2


@pytest.mark.parametrize('bad',['agent','viewer','business','unknown','boolean','source','blank_note','stale','already_status','no_balance'])
def test_bad_hold_has_no_effect(own,bad):
    args=['custom-x',True,'customer-x','Source checked.',review_revision(own)];caller=ctx(own)
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='different')
    if bad=='unknown':args[0]='missing'
    if bad=='boolean':args[1]='true'
    if bad=='source':args[2]='missing'
    if bad=='blank_note':args[3]=' '
    if bad=='stale':args[-1]='old'
    if bad=='already_status':args[1]=False
    if bad=='no_balance':receipt(own,amount=65000);args[-1]=review_revision(own)
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):set_collection_hold(own,*args,ctx=caller)
    assert vars(own)==before


def populated(own):
    own.accounts['euro']=Account('euro','EUR',20000,5000)
    receipt(own);payment(own);correct(own);hold(own)
    return report_tables(own,ctx=ctx(own,'viewer'))


def test_report_reconciles_cash_claims_payables_and_held_forecast_by_currency_without_mutation(own):
    report=populated(own);before=copy.deepcopy(vars(own));again=report_tables(own,ctx=ctx(own))
    assert report==again and vars(own)==before
    tables={t['name']:t for t in report['tables']}
    usd=tables['Summary']['rows'][0];assert usd[0]=='USD' and usd[2]==Money(95000,'USD')
    assert usd[4]==Money(100000,'USD') and usd[5]==Money(55000,'USD') and usd[6]==Money(125000,'USD')
    assert tables['Summary']['rows'][1][2]==Money(20000,'EUR')
    assert sum(r[4].minor for r in tables['Forecast']['rows'] if r[0]=='USD')==45000
    assert any(r[1]=='bill_payment_correction' and r[4]==Money(5000,'USD') for r in tables['Cash events']['rows'])
    row=next(r for r in tables['Receivables']['rows'] if r[0]=='custom-x');assert row[9]=='held' and row[12]


def test_report_distinguishes_no_breach_and_includes_updated_collection_date_source(own):
    from cashflow_copilot.owner_workflows import confirm_collection_date
    own.outflows.clear()
    changed=confirm_collection_date(own,'custom-x',date(2027,1,11),'customer-x','Reconfirmed synthetic expectation.',review_revision(own),ctx=ctx(own))
    report=report_tables(own,ctx=ctx(own))
    assert report['tables'][0]['rows'][0][8]=='none in window'
    row=next(r for r in report['tables'][2]['rows'] if r[0]=='custom-x')
    assert row[13]==changed['confirmation_evidence']
    forecastrow=next(r for r in report['tables'][1]['rows'] if r[1]==date(2027,1,11))
    assert changed['confirmation_evidence'] in forecastrow[10]


@pytest.mark.parametrize('text',['=SUM(A1:A2)','+CMD','-CMD','@SUM(A1)','  =1+2','\t=2+3','\r=1','\n@evil','\ufeff=1+2',' \x00=2+3'])
def test_csv_formula_text_is_literal_but_genuine_negative_money_stays_numeric(text):
    assert csv_text(text).startswith("'")
    assert csv_text(Money(-12345,'USD'))=='-123.45'


def test_xlsx_literal_cells_typed_money_dates_no_formulas_or_external_relationships(own):
    own.receivables['custom-x'].counterparty='=HYPERLINK("https://invalid.example","x")'
    report=populated(own);data=xlsx_report(report);ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    with ZipFile(BytesIO(data)) as archive:
        assert archive.testzip() is None
        allxml=b''.join(archive.read(n) for n in archive.namelist())
        assert b'<f>' not in allxml and b'TargetMode="External"' not in allxml and b'vbaProject' not in allxml
        summary=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        cell=summary.find(".//s:c[@r='C6']",ns);assert cell.attrib.get('t') is None and cell.find('s:v',ns).text=='950.00'
        receivables=ET.fromstring(archive.read('xl/worksheets/sheet3.xml'))
        literal=receivables.find(".//s:c[@r='C6']",ns);assert literal.attrib['t']=='inlineStr' and literal.find('.//s:t',ns).text.startswith('=HYPERLINK')
        datecell=receivables.find(".//s:c[@r='H6']",ns);assert datecell.attrib['s']=='5' and datecell.find('s:v',ns) is not None
        assert summary.find('.//s:pane',ns).attrib['topLeftCell']=='B6'
    csvdata=csv_report(report,'Receivables').decode('utf-8-sig')
    rows=list(csv.reader(StringIO(csvdata)));assert rows[1][5].startswith("'=HYPERLINK")
    with ZipFile(BytesIO(csv_archive(report))) as archive:assert len(archive.namelist())==7 and 'README.txt' in archive.namelist()


def test_xlsx_large_exact_amount_uses_text_and_jpy_numeric_precision(own):
    own.accounts['yen']=Account('yen','JPY',123456,0)
    own.accounts['operating'].opening_minor=1234567890123456789
    report=report_tables(own,ctx=ctx(own));data=xlsx_report(report)
    with ZipFile(BytesIO(data)) as archive:
        root=ET.fromstring(archive.read('xl/worksheets/sheet1.xml'));ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        large=root.find(".//s:c[@r='B6']",ns);assert large.attrib['t']=='inlineStr' and large.find('.//s:t',ns).text=='12345678901234567.89'
        yen=root.find(".//s:c[@r='B7']",ns);assert yen.attrib['s']=='3' and yen.find('s:v',ns).text=='123456'
    assert csv_text(Money(1234567890123456789,'USD'))=="'12345678901234567.89"
    assert csv_text(Money(12345678901234567890123456789012345,'USD'))=="'123456789012345678901234567890123.45"


def test_negative_correction_and_repeated_rebase_are_separate_from_receipts(own):
    payment(own,10000);correct(own,20000)
    event=next(e for e in cash_events(own) if e['kind']=='bill_payment_correction')
    assert event['amount_minor']==-10000 and not own.receipts
    assert own.actual_cash('USD')==80000
    own.roll_forward(date(2027,1,2),ctx=ctx(own));own.roll_forward(date(2027,1,3),ctx=ctx(own))
    assert own.actual_cash('USD')==80000


@pytest.mark.parametrize('bad',['business','missing_context','account_id_mismatch','missing_currency','long_text','long_horizon'])
def test_invalid_report_scope_or_data_refused_without_changes(own,bad):
    caller=ctx(own,'viewer')
    if bad=='business':caller=ctx(own,business='other')
    if bad=='missing_context':caller=None
    if bad=='account_id_mismatch':own.accounts['same']=Account('different-id','USD',0,0)
    if bad=='missing_currency':own.receivables['unknown']=Receivable('unknown','JPY',1000,None,'customer-x')
    if bad=='long_text':own.evidence['customer-x'].summary='a'*32768
    if bad=='long_horizon':own.horizon_days=121
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):report_tables(own,ctx=caller)
    assert vars(own)==before


def test_http_controls_read_exports_csrf_and_concurrent_exact_correction_replay(own,live_server):
    base,db=live_server;payment(own);save(db,own)
    for path in ['/payment-corrections','/holds','/reports']:assert _get(base,path)[0]==200
    fields=dict(csrf=web.CSRF_TOKEN,confirmed='yes',bill_id='materials',payment_id='observed-p1',amount='150.00',date='2027-01-01',
        evidence_id='supplier-source',note='Source observation corrected.',revision=review_revision(own))
    assert _post(base,'/payment-corrections/confirm',dict(fields,confirmed='no'))[0]==400
    assert _post(base,'/payment-corrections/confirm',dict(fields,csrf='bad'))[0]==403
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(r[0]==200 for r in pool.map(lambda _: _post(base,'/payment-corrections/confirm',fields),range(2)))
    loaded=_load(db);assert len(loaded.outflows['materials'].payment_adjustments)==1
    status,_=_post(base,'/holds/confirm',dict(csrf=web.CSRF_TOKEN,confirmed='yes',invoice_id='custom-x',active='yes',evidence_id='customer-x',note='Source dispute.',revision=review_revision(loaded)))
    assert status==200
    before=copy.deepcopy(vars(_load(db)))
    for path,magic in [('/reports/workbook.xlsx',b'PK'),('/reports/csv.zip',b'PK'),('/reports/csv/summary',b'\xef\xbb\xbf')]:
        with urllib.request.urlopen(base+path) as response:
            assert response.status==200 and response.read().startswith(magic)
            assert response.headers['Cache-Control']=='no-store' and 'attachment' in response.headers['Content-Disposition']
    assert vars(_load(db))==before


def test_model_cannot_correct_hold_release_or_export_files(own):
    assert len(TOOLS)==8
    for name in ['correct_bill_payment','set_collection_hold','release_hold','xlsx_report','csv_report']:
        with pytest.raises(ToolRejectedError):execute_tool(ctx(own,'agent'),own,name,{})


def test_http_rejects_forged_currency_or_authority_on_payment_correction(own,live_server):
    base,db=live_server;payment(own);save(db,own)
    fields=dict(csrf=web.CSRF_TOKEN,confirmed='yes',bill_id='materials',payment_id='observed-p1',amount='150.00',date='2027-01-01',
        evidence_id='supplier-source',note='Source checked.',revision=review_revision(own),currency='EUR',role='owner')
    assert _post(base,'/payment-corrections/confirm',fields)[0]==400
    assert not _load(db).outflows['materials'].payment_adjustments


def test_backup_restores_correction_and_hold_metadata_without_changing_original(own,tmp_path):
    payment(own);correct(own);hold(own)
    db=str(tmp_path/'new.sqlite');save(db,own);backup=recovery.create_backup(db,'Corrected and held',ctx=ctx(own))
    loaded=_load(db);hold(loaded,False);correct(loaded,0);save(db,loaded)
    p=recovery.restore_preview(db,backup['id'],ctx=ctx(loaded))
    recovery.restore_backup(db,backup['id'],p['revision'],own.business_id,ctx=ctx(loaded))
    loaded=_load(db);assert loaded.receivables['custom-x'].collection_hold['active']
    assert loaded.outflows['materials'].paid_minor==15000 and loaded.outflows['materials'].observed_payments[0]['amount_minor']==20000
