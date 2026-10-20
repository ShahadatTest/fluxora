"""Accounting facts and local recovery: no live provider or payment execution."""
import copy
from datetime import date
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from cashflow_copilot import actions, recovery, web
from cashflow_copilot.accounting import correct_receipt, record_bill_payment, save_forecast, compare_forecast
from cashflow_copilot.adaptive import generate_proposals, input_problems
from cashflow_copilot.agent import AGENT_RUNS_SCHEMA, list_agent_runs, TOOLS, execute_tool, ToolRejectedError
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.cli import cmd_reset
from cashflow_copilot.errors import AuthorizationError, CashflowError, IdempotencyConflict
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account, Outflow, OutflowAmendment, Plan, Receivable
from cashflow_copilot.owner_workflows import review_revision, review_receipt, reconcile_existing_receipt, daily_preview, close_day
from cashflow_copilot.settlement import effective_receipt, cash_events, bill_schedule
from cashflow_copilot.store import SQLiteStore
from tests.test_adaptive_priorities import own
from tests.test_owner_workflows import ctx, receipt, save, request
from tests.test_web import live_server, _get, _post, _load, _mpost


def correction(ledger, amount=6000, invoice='custom-x', day=None, revision=None, **kw):
    return correct_receipt(ledger,'existing-r',invoice,amount,day or ledger.as_of,'customer-x',
        'Owner checked the correction source.', revision or review_revision(ledger),ctx=kw.get('ctx',ctx(ledger)))


def payment(ledger, amount=20000, pid='observed-p1', bill='materials', day=None, revision=None, **kw):
    return record_bill_payment(ledger,bill,pid,amount,day or ledger.as_of,'supplier-source',
        'Verified synthetic observed payment.',revision or review_revision(ledger),ctx=kw.get('ctx',ctx(ledger)))


def snapshot(ledger):
    return save_forecast(ledger,'Before owner observations',review_revision(ledger),ctx=ctx(ledger))['snapshot_id']


def test_receipt_correction_compensates_without_rewriting_original_and_preserves_replay(own,tmp_path):
    original = copy.deepcopy(receipt(own))
    revision = review_revision(own)
    result = correction(own,revision=revision)
    assert own.receipts['existing-r'] == original
    assert own.receivables['custom-x'].allocated_minor == 6000
    assert [a.amount_minor for a in own.receivables['custom-x'].allocations] == [10000,-10000,6000]
    assert effective_receipt(own,'existing-r').amount_minor == 6000
    assert own.actual_cash('USD') == 106000
    db = str(tmp_path/'corrections.sqlite'); save(db,own); loaded = _load(db)
    before = copy.deepcopy(vars(loaded))
    assert correction(loaded,revision=revision) == dict(result,duplicate=True)
    assert vars(loaded) == before
    assert loaded.record_receipt(original,ctx=ctx(loaded))['duplicate']


def test_reallocation_and_full_reversal_have_separate_cash_and_allocation_effects(own):
    receipt(own)
    correction(own,10000,'custom-y')
    assert own.actual_cash('USD') == 110000
    assert own.receivables['custom-x'].remaining_minor == 65000
    assert own.receivables['custom-y'].remaining_minor == 35000
    correction(own,0,'custom-y')
    assert own.actual_cash('USD') == 100000
    assert own.receivables['custom-y'].remaining_minor == 45000
    assert own.receipts['existing-r'].amount_minor == 10000
    assert len(own.receipt_adjustments) == 2 and not own.actions and not own.consent_map
    with pytest.raises(CashflowError):
        review_receipt(own,'existing-r','','customer-x','Do not review reversed.',True,review_revision(own),ctx=ctx(own))


@pytest.mark.parametrize('bad',['negative','bool','over','currency','invoice','past','source','reason','stale','agent','viewer','other_business','unchanged'])
def test_bad_correction_is_atomic(own,bad):
    receipt(own)
    own.receivables['foreign'] = Receivable('foreign','EUR',100000,own.as_of,'customer-y')
    args = ['existing-r','custom-x',6000,own.as_of,'customer-x','Checked source.',review_revision(own)]
    caller = ctx(own)
    if bad=='negative': args[2]=-1
    if bad=='bool': args[2]=True
    if bad=='over': args[2]=65001
    if bad=='currency': args[1]='foreign'
    if bad=='invoice': args[1]='missing'
    if bad=='past': args[3]=date(2026,12,31)
    if bad=='source': args[4]='unknown'
    if bad=='reason': args[5]=' '
    if bad=='stale': args[6]='old'
    if bad in {'agent','viewer'}: caller=ctx(own,bad)
    if bad=='other_business': caller=ctx(own,business='elsewhere')
    if bad=='unchanged': args[2]=10000
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError): correct_receipt(own,*args,ctx=caller)
    assert vars(own)==before


def test_correction_reopens_review_without_rewriting_historical_action(own):
    action=request(own,state='counterparty_accepted')
    receipt(own)
    actions.observe_payment(action,'existing-r',ctx=ctx(own))
    review_receipt(own,'existing-r',action.id,'customer-x','Checked original source.',False,review_revision(own),ctx=ctx(own))
    reconcile_existing_receipt(own,'existing-r','customer-x','Checked reconciliation.',review_revision(own),ctx=ctx(own))
    historic=copy.deepcopy(action)
    correction(own)
    assert action==historic and action.state=='reconciled'
    assert 'existing-r' not in own.receipt_reviews
    assert next(iter(own.receipt_review_archive.values()))['previous_review']['status']=='reconciled'
    review_receipt(own,'existing-r','','customer-x','New correction independently checked.',False,review_revision(own),ctx=ctx(own))
    reconcile_existing_receipt(own,'existing-r','customer-x','New independent reconciliation.',review_revision(own),ctx=ctx(own))
    assert action==historic and own.receipt_reviews['existing-r']['status']=='reconciled'


def test_correction_after_absorbed_receipt_does_not_change_old_opening_or_double_count(own):
    receipt(own)
    own.roll_forward(date(2027,1,2),ctx=ctx(own))
    correction(own,6000)
    assert own.accounts['operating'].opening_minor==110000
    assert own.actual_cash('USD')==106000
    own.roll_forward(date(2027,1,3),ctx=ctx(own))
    assert own.accounts['operating'].opening_minor==106000
    assert own.actual_cash('USD')==106000
    correction(own,7000,day=date(2027,1,4))
    with pytest.raises(CashflowError): correction(own,8000,day=date(2027,1,3))


def test_partial_payments_reserve_only_remaining_keep_protected_dates_and_complete_once(own,tmp_path):
    original=copy.deepcopy(own.outflows['materials'])
    payment(own)
    out=own.outflows['materials']
    assert (out.amount_minor,out.paid_minor,out.remaining_minor,out.status)==(50000,20000,30000,'scheduled')
    assert out.date==original.date and own.actual_cash('USD')==80000
    rows=forecast(own,own.as_of,30).currencies['USD'].days
    assert rows[0].outflow_minor==20000 and rows[6].outflow_minor==30000
    assert sum(r.outflow_minor for r in rows)==140000
    payment(own,10000,'payroll-part','staff')
    assert own.outflows['staff'].protected and own.outflows['staff'].date==date(2027,1,5)
    own.mark_paid('materials',date(2027,1,2),'owner','supplier-source',ctx=ctx(own))
    assert out.status=='paid' and out.paid_minor==50000
    assert own.mark_paid('materials',date(2027,1,2),'owner','supplier-source',ctx=ctx(own))['duplicate']
    db=str(tmp_path/'payment.sqlite');save(db,own);loaded=_load(db)
    assert loaded.outflows['materials'].observed_payments==out.observed_payments
    assert payment(loaded)['duplicate']
    loaded.roll_forward(date(2027,1,3),ctx=ctx(loaded))
    assert loaded.actual_cash('USD')==40000
    assert not loaded.consent_map and not loaded.actions


@pytest.mark.parametrize('bad',['zero','negative','bool','over','id','other_bill_id','past','source','note','stale','agent','viewer','business'])
def test_bad_partial_payment_has_no_effect(own,bad):
    args=['materials','p1',20000,own.as_of,'supplier-source','Checked observed payment.',review_revision(own)]
    caller=ctx(own)
    if bad=='zero':args[2]=0
    if bad=='negative':args[2]=-1
    if bad=='bool':args[2]=True
    if bad=='over':args[2]=50001
    if bad=='id':args[1]='../bad'
    if bad=='other_bill_id':args[0]='missing'
    if bad=='past':args[3]=date(2026,12,31)
    if bad=='source':args[4]='missing'
    if bad=='note':args[5]=' '
    if bad=='stale':args[6]='old'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='elsewhere')
    before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):record_bill_payment(own,*args,ctx=caller)
    assert vars(own)==before


def test_payment_id_conflict_and_role_are_checked_before_replay(own):
    payment(own)
    for kw in [dict(amount=20001),dict(bill='overhead'),dict(day=date(2027,1,2))]:
        with pytest.raises(IdempotencyConflict):payment(own,**kw)
    for role in ['viewer','agent']:
        with pytest.raises(AuthorizationError): payment(own,ctx=ctx(own,role))
    assert len(own.outflows['materials'].observed_payments)==1


def test_partial_payment_applies_fifo_to_amended_splits_and_excludes_new_deferral(own):
    out=own.outflows['overhead']
    out.recorded_splits=[(date(2027,1,8),10000),(date(2027,1,20),10000)]
    payment(own,15000,bill='overhead')
    assert bill_schedule(out)==[(date(2027,1,20),5000)]
    assert all(d.get('outflow_id')!='overhead' for p in generate_proposals(own,ctx=ctx(own)) for d in p.drafts)
    with pytest.raises(ValueError):
        forecast(own,own.as_of,30,Plan('bad','bad','bad',amendments=[OutflowAmendment('overhead',date(2027,1,22))]))
    assert not own.consent_map
    own.roll_forward(date(2027,1,9),ctx=ctx(own))
    assert not any(p.startswith('overhead:') for p in input_problems(own))


def test_multiple_correction_order_survives_storage_and_negative_delta_is_conservative(own,tmp_path):
    receipt(own)
    correction(own,5000,day=date(2027,1,2))
    correction(own,7000,day=date(2027,1,3))
    correction(own,3000,day=date(2027,1,4))
    db=str(tmp_path/'sequence.sqlite');save(db,own);loaded=_load(db)
    assert list(loaded.receipt_adjustments)==list(own.receipt_adjustments)
    assert effective_receipt(loaded,'existing-r').amount_minor==3000
    assert loaded.receivables['custom-x'].remaining_minor==62000
    assert loaded.actual_cash('USD',date(2027,1,4))==103000
    row=forecast(loaded,loaded.as_of,30).currencies['USD'].days[3]
    assert row.outflow_minor==4000 and row.intraday_low_minor==103000


def test_zero_decimal_bill_currency_is_separate(own):
    own.accounts['yen']=Account('yen','JPY',10000,1000)
    own.outflows['yenbill']=Outflow('yenbill','JPY',5000,date(2027,1,3),False,'supplier-source')
    payment(own,1234,bill='yenbill')
    assert own.actual_cash('JPY')==8766 and own.actual_cash('USD')==100000
    assert own.outflows['yenbill'].remaining_minor==3766


def test_saved_forecast_is_immutable_and_comparison_traces_events_variance_and_currencies(own,tmp_path):
    own.accounts['eu']=Account('eu','EUR',20000,0)
    sid=snapshot(own); frozen=copy.deepcopy(own.forecast_snapshots[sid])
    receipt(own);payment(own)
    correction(own)
    preview=daily_preview(own,date(2027,1,3),ctx=ctx(own))
    values={r['account_id']:dict(opening_minor=r['calculated_opening_minor']+(-1000 if r['currency']=='USD' else 0),evidence_id='pay') for r in preview['accounts']}
    close_day(own,date(2027,1,3),values,'Owner checked bank-like synthetic statement.',review_revision(own),ctx=ctx(own))
    report=compare_forecast(own,sid,date(2027,1,3),ctx=ctx(own,'viewer'))
    usd=report['currencies']['USD']
    assert usd[0]['recorded_receipts_minor']==10000 and usd[0]['receipt_corrections_minor']==-4000
    assert usd[0]['recorded_paid_minor']==20000
    assert usd[-1]['snapshot_variance_minor']==-1000
    assert usd[-1]['recorded_closing_minor']==85000==own.actual_cash('USD')
    assert report['currencies']['EUR'][-1]['recorded_closing_minor']==20000
    assert 'customer-x' in usd[0]['evidence_ids'] and usd[-1]['evidence_ids']
    assert own.forecast_snapshots[sid]==frozen
    assert next(r for r in report['invoices'] if r['id']=='custom-x')['recorded_net_allocation_minor']==6000
    db=str(tmp_path/'snapshot.sqlite');save(db,own)
    assert compare_forecast(_load(db),sid,date(2027,1,3),ctx=ctx(own))==report


@pytest.mark.parametrize('bad',['early','late','unknown','cross_business','agent_save','stale_save'])
def test_invalid_forecast_capture_or_comparison_is_read_only(own,bad):
    sid=snapshot(own); before=copy.deepcopy(vars(own))
    with pytest.raises(CashflowError):
        if bad=='agent_save':save_forecast(own,'No owner','old',ctx=ctx(own,'agent'))
        elif bad=='stale_save':save_forecast(own,'Stale','old',ctx=ctx(own))
        else:compare_forecast(own,'missing' if bad=='unknown' else sid,
            date(2026,12,31) if bad=='early' else date(2028,1,1) if bad=='late' else own.as_of,
            ctx=ctx(own,business='different') if bad=='cross_business' else ctx(own))
    assert vars(own)==before


def test_restore_native_backup_retains_rollback_and_replay_survives_restart_and_later_work(own,tmp_path):
    db=str(tmp_path/'recover.sqlite');save(db,own)
    backup=recovery.create_backup(db,'Clean snapshot',ctx=ctx(own))
    receipt(own);payment(own);snapshot(own);save(db,own)
    before=copy.deepcopy(vars(_load(db)))
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(own))
    assert vars(_load(db))==before and preview['removed_receipt_ids']==['existing-r']
    restored=recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))
    current=_load(db)
    assert not current.receipts and not current.forecast_snapshots and not current.outflows['materials'].observed_payments
    _,_,rollback=recovery.checked_backup(db,restored['rollback_backup_id'])
    assert vars(rollback)==before
    receipt(current,rid='later-work');save(db,current)
    later=copy.deepcopy(vars(_load(db)))
    assert recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))['duplicate']
    assert vars(_load(db))==later
    with pytest.raises(AuthorizationError):recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own,'agent'))


@pytest.mark.parametrize('bad',['checksum','size','version','manifest_business','path','missing','stale','typed_target','agent','viewer','business'])
def test_invalid_restore_preserves_active_ledger(own,tmp_path,bad):
    db=str(tmp_path/'recover.sqlite');save(db,own)
    backup=recovery.create_backup(db,'Source',ctx=ctx(own))
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(own))
    bid=backup['id'];revision=preview['revision'];target=own.business_id;caller=ctx(own)
    folder=recovery.recovery_directory(db);manifest=folder/(bid+'.json');info=json.loads(manifest.read_text())
    if bad=='checksum':info['sha256']='0'*64
    if bad=='size':info['bytes']=0
    if bad=='version':info['app_version']='0.99.0'
    if bad=='manifest_business':info['business_id']='other'
    manifest.write_text(json.dumps(info))
    if bad=='path':bid='../outside'
    if bad=='missing':bid='0'*32
    if bad=='stale':receipt(own);save(db,own)
    if bad=='typed_target':target='wrong'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='wrong')
    before=copy.deepcopy(vars(_load(db)))
    with pytest.raises(CashflowError):recovery.restore_backup(db,bid,revision,target,ctx=caller)
    assert vars(_load(db))==before


def test_restore_failure_rolls_back_transaction_and_keeps_pre_restore_copy(own,tmp_path,monkeypatch):
    db=str(tmp_path/'recover.sqlite');save(db,own)
    backup=recovery.create_backup(db,'Before',ctx=ctx(own))
    receipt(own);save(db,own)
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(own));before=copy.deepcopy(vars(_load(db)))
    original=SQLiteStore.save_ledger
    def fail(self,ledger,**kwargs):
        original(self,ledger,**kwargs)
        raise RuntimeError('Injected failure after SQL writes, before commit')
    monkeypatch.setattr(SQLiteStore,'save_ledger',fail)
    with pytest.raises(RuntimeError):recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))
    assert vars(_load(db))==before
    assert len(recovery.list_backups(db,ctx=ctx(own)))==2


def test_reset_automatically_preserves_current_state_and_cross_business_restore_requires_target(own,tmp_path):
    db=str(tmp_path/'reset.sqlite');receipt(own);save(db,own)
    cmd_reset(db)
    seeded=_load(db); inventory=recovery.list_backups(db,ctx=ctx(seeded))
    assert len(inventory)==1
    preview=recovery.restore_preview(db,inventory[0]['id'],ctx=ctx(seeded))
    assert preview['cross_business']
    recovery.restore_backup(db,inventory[0]['id'],preview['revision'],own.business_id,ctx=ctx(seeded))
    assert 'existing-r' in _load(db).receipts and _load(db).business_id==own.business_id


def test_restore_agent_history_from_backup(own,tmp_path):
    db=str(tmp_path/'history.sqlite');save(db,own)
    conn=sqlite3.connect(db);conn.executescript(AGENT_RUNS_SCHEMA)
    conn.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?,?,?)',('r1',own.business_id,'Synthetic question','DETERMINISTIC MOCK','[]','Synthetic safe answer','a','b'));conn.commit();conn.close()
    backup=recovery.create_backup(db,'Includes synthetic history',ctx=ctx(own))
    conn=sqlite3.connect(db);conn.execute('DELETE FROM agent_runs');conn.commit();conn.close()
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(own))
    recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))
    assert list_agent_runs(db)[0]['run_id']=='r1'


def test_http_new_pages_confirmation_csrf_and_concurrent_payment_replay(own,live_server):
    base,db=live_server;receipt(own);save(db,own)
    for path in ['/payments','/corrections','/variance','/backups']:
        status,html=_get(base,path)
        assert status==200 and 'SYNTHETIC DEMO' in html
    fields=dict(csrf=web.CSRF_TOKEN,confirmed='yes',bill_id='materials',payment_id='http-payment',amount='200.00',date='2027-01-01',evidence_id='supplier-source',note='Checked payment source.',revision=review_revision(own))
    assert _post(base,'/payments/confirm',dict(fields,confirmed='no'))[0]==400
    assert _post(base,'/payments/confirm',dict(fields,csrf='wrong'))[0]==403
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(r[0]==200 for r in pool.map(lambda _: _post(base,'/payments/confirm',fields),range(2)))
    loaded=_load(db)
    assert len(loaded.outflows['materials'].observed_payments)==1
    assert loaded.actual_cash('USD')==90000
    status,_=_post(base,'/backups/create',dict(csrf=web.CSRF_TOKEN,confirmed='yes',label='HTTP backup'))
    assert status==200 and len(recovery.list_backups(db,ctx=ctx(loaded)))==1


def test_model_tool_surface_has_no_accounting_or_recovery_mutations(own):
    assert len(TOOLS)==8
    for name in ['correct_receipt','record_bill_payment','restore_backup','create_backup','save_forecast']:
        with pytest.raises(ToolRejectedError): execute_tool(ctx(own,'agent'),own,name,{})
    assert own.receipt_adjustments==own.forecast_snapshots=={}


def test_http_fixture_replacement_creates_recoverable_previous_state(own,live_server):
    from pathlib import Path
    import re
    base,db=live_server;receipt(own);save(db,own)
    data=(Path(__file__).parent.parent/'examples'/'synthetic-workshop.json').read_bytes()
    status,html=_mpost(base,'/ledger/import/fixture',{'csrf':web.CSRF_TOKEN},{'file':('synthetic.json',data,'application/json')})
    assert status==200
    token=re.search(r"name=['\"]token['\"] value=['\"]([^'\"]+)",html).group(1)
    assert _post(base,'/ledger/import/confirm',dict(csrf=web.CSRF_TOKEN,token=token))[0]==200
    current=_load(db)
    assert not current.receipts
    backups=recovery.list_backups(db,ctx=ctx(current))
    assert len(backups)==1
    _,_,old=recovery.checked_backup(db,backups[0]['id'])
    assert 'existing-r' in old.receipts
