"""Financial/authority/storage regression coverage for six v0.12 workflows."""
import base64,copy,hashlib,json,re,urllib.parse,urllib.request,urllib.error
from datetime import date,timedelta
from concurrent.futures import ThreadPoolExecutor
import pytest

from cashflow_copilot import web,recovery
from cashflow_copilot.accounting import correct_receipt,save_forecast,compare_forecast
from cashflow_copilot.agent import TOOLS,execute_tool,_tool_get_evidence
from cashflow_copilot.authz import RequestContext
from cashflow_copilot.business_tools import data_freshness
from cashflow_copilot.completion import correct_split_payment,correct_cash_movement,statement_report
from cashflow_copilot.errors import CashflowError,AuthorizationError
from cashflow_copilot.exploration import search_records,cash_calendar
from cashflow_copilot.extension import correct_payment_date,observation_ids
from cashflow_copilot.forecast import forecast
from cashflow_copilot.models import Account,Receivable
from cashflow_copilot.owner_workflows import review_revision,daily_preview
from cashflow_copilot.payment_review import observed_movements
from cashflow_copilot.planning import _hash,_revision
from cashflow_copilot.reports import report_tables
from cashflow_copilot.settlement import cash_events,split_unallocated
from cashflow_copilot.workbench import (setup_business,create_promise,review_promise,promise_report,
    review_net_settlement,record_unallocated_refund,correct_unallocated_refund,party_statement,party_csv,
    attach_evidence,attachment_bytes,attachment_inventory,retire_attachment,validate_attachment)
from tests.test_adaptive_priorities import own
from tests.test_business_tools import imported,split,more
from tests.test_completion import cash,statement,match
from tests.test_owner_workflows import ctx,save,receipt
from tests.test_web import live_server,_load,_get,_post,_mpost


def state(ledger):return copy.deepcopy(vars(ledger))

def setup_spec():
    return dict(id='new-synthetic',name='Synthetic Starter',base_currency='USD',timezone='Asia/Dhaka',as_of='2027-02-01',horizon_days=30,synthetic=True,
        accounts=[dict(id='usd',currency='USD',opening_minor=100000,floor_minor=20000,source_note='Checked synthetic USD opening.'),
            dict(id='eur',currency='EUR',opening_minor=-10000,floor_minor=5000,source_note='Checked synthetic EUR overdraft.')])

def promise(ledger,ident='promise-one',amount=10000,due=None,invoice='custom-x',revision=None,caller=None):
    return create_promise(ledger,ident,invoice,amount,due or ledger.as_of,'customer-x','Checked synthetic customer promise.',revision or review_revision(ledger),ctx=caller or ctx(ledger))

def link(ledger,step='link',rid='existing-r',amount=5000,pid='promise-one',revision=None,caller=None):
    return review_promise(ledger,pid,step,rid,amount,'customer-x','Checked observed receipt source and portion.',revision or review_revision(ledger),ctx=caller or ctx(ledger))

def returned(ledger,amount=10000,ident='return-one',day=None,revision=None,caller=None,ack=False):
    return record_unallocated_refund(ledger,'bulk-one',ident,amount,day or ledger.as_of,'customer-x','Checked observed return of excess.',revision or review_revision(ledger),ctx=caller or ctx(ledger),acknowledge_duplicates=ack)

def attachment(ledger,filename='source.txt',data=b'SYNTHETIC ORIGINAL FILE BODY',revision=None,caller=None):
    return attach_evidence(ledger,'customer-x',filename,data,'Checked original synthetic source.',revision or review_revision(ledger),ctx=caller or ctx(ledger))

def net(ledger,bid,keys=None,rid='bank-one',revision=None,caller=None):
    return review_net_settlement(ledger,bid,rid,keys or ['receipt:receipt-a','receipt:receipt-b','other_cash:fee-one'],
        'customer-x','Checked gross receipts and separately observed fee.',revision or review_revision(ledger),ctx=caller or ctx(ledger))

def net_data(ledger):
    receipt(ledger,'receipt-a',20000);receipt(ledger,'receipt-b',10000,invoice='custom-y');cash(ledger,amount=1000)
    return statement(ledger,[dict(id='bank-one',currency='USD',signed_minor=29000,date='2027-01-01',reference='Two receipts net of fee')])


def test_guided_setup_creates_only_separate_attested_openings_and_replays_after_restart(own,tmp_path):
    receipt(own);rev=review_revision(own);spec=setup_spec();result=setup_business(own,spec,rev,ctx=ctx(own))
    assert own.business_id=='new-synthetic' and own.actual_cash('USD')==100000 and own.actual_cash('EUR')==-10000
    assert not own.receipts and not own.outflows and not own.actions and not own.runtime_evidence_ids
    assert all(r['status']=='fresh' for r in data_freshness(own,ctx=ctx(own))['rows'])
    assert forecast(own,own.as_of,30).currencies['EUR'].first_breach_date==own.as_of
    db=tmp_path/'setup.sqlite';save(db,own);loaded=_load(db);before=state(loaded)
    assert setup_business(loaded,spec,rev,ctx=ctx(loaded))==dict(result,duplicate=True) and state(loaded)==before
    with pytest.raises(AuthorizationError):setup_business(loaded,spec,rev,ctx=ctx(loaded,'agent'))


@pytest.mark.parametrize('bad',['same_id','bad_id','name','real','timezone','date','horizon','horizon_bool','base_currency','no_accounts','duplicate_currency','duplicate_id','currency','opening_bool','opening_big','floor_negative','source','extra','agent','viewer','business','stale'])
def test_bad_setup_atomic(own,bad):
    spec=setup_spec();rev=review_revision(own);caller=ctx(own)
    if bad=='same_id':spec['id']=own.business_id
    if bad=='bad_id':spec['id']='../bad'
    if bad=='name':spec['name']=' '
    if bad=='real':spec['synthetic']=False
    if bad=='timezone':spec['timezone']='Not/AZone'
    if bad=='date':spec['as_of']='2027-02-30'
    if bad=='horizon':spec['horizon_days']=121
    if bad=='horizon_bool':spec['horizon_days']=True
    if bad=='base_currency':spec['base_currency']='JPY'
    if bad=='no_accounts':spec['accounts']=[]
    if bad=='duplicate_currency':spec['accounts'][1]['currency']='USD'
    if bad=='duplicate_id':spec['accounts'][1]['id']='usd'
    if bad=='currency':spec['accounts'][1]['currency']='BAD'
    if bad=='opening_bool':spec['accounts'][0]['opening_minor']=True
    if bad=='opening_big':spec['accounts'][0]['opening_minor']=10**15+1
    if bad=='floor_negative':spec['accounts'][0]['floor_minor']=-1
    if bad=='source':spec['accounts'][0]['source_note']=' '
    if bad=='extra':spec['accounts'][0]['consent']=True
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':rev='old'
    before=state(own)
    with pytest.raises(CashflowError):setup_business(own,spec,rev,ctx=caller)
    assert state(own)==before


def test_setup_older_replay_cannot_revive_other_business_and_ids_are_not_reused(own):
    first=setup_spec();rev=review_revision(own);setup_business(own,first,rev,ctx=ctx(own));second=setup_spec();second['id']='second-new'
    setup_business(own,second,review_revision(own),ctx=ctx(own));before=state(own)
    with pytest.raises(CashflowError):setup_business(own,first,rev,ctx=ctx(own))
    with pytest.raises(CashflowError):setup_business(own,first,review_revision(own),ctx=ctx(own))
    assert state(own)==before


def test_promise_is_not_cash_expected_date_approval_or_consent_and_uses_partial_observed_portions(own):
    financial=_revision(own,'test',[]);curve=forecast(own,own.as_of,30);promise(own,amount=10000)
    assert _revision(own,'test',[])==financial and forecast(own,own.as_of,30)==curve
    receipt(own,amount=7000);before=own.actual_cash('USD');link(own,amount=5000)
    row=promise_report(own,ctx=ctx(own))[0];assert row['fulfilled_minor']==5000 and row['remaining_minor']==5000 and row['status']=='partially_observed'
    promise(own,'second-promise',amount=5000);link(own,pid='second-promise',amount=2000)
    with pytest.raises(CashflowError):link(own,pid='second-promise',amount=1)
    assert own.actual_cash('USD')==before and not own.actions and not own.consent_map
    link(own,step='unlink',amount=0);link(own,amount=5000)
    assert own.actual_cash('USD')==before and own.receivables['custom-x'].expected_date==date(2027,1,12)


def test_promise_missed_met_late_and_correction_invalidates_link(own):
    promise(own,amount=10000);own.roll_forward(date(2027,1,2),ctx=ctx(own));assert promise_report(own,ctx=ctx(own))[0]['status']=='missed'
    receipt(own,amount=10000);link(own,amount=10000);row=promise_report(own,ctx=ctx(own))[0]
    assert row['status']=='met_late' and row['late_observed_minor']==10000
    correct_receipt(own,'existing-r','custom-x',9000,own.as_of,'customer-x','Source amount changed.',review_revision(own),ctx=ctx(own))
    assert promise_report(own,ctx=ctx(own))[0]['status']=='needs_review'
    link(own,step='unlink',amount=0);link(own,amount=9000);assert promise_report(own,ctx=ctx(own))[0]['fulfilled_minor']==9000


@pytest.mark.parametrize('bad',['over','zero','bool','negative','past','far','missing','held','overcommit','id','source','agent','viewer','business','stale'])
def test_bad_promise_atomic(own,bad):
    amount=10000;due=own.as_of;invoice='custom-x';ident='promise-one';ev='customer-x';caller=ctx(own);rev=review_revision(own)
    if bad=='over':amount=65001
    if bad=='zero':amount=0
    if bad=='bool':amount=True
    if bad=='negative':amount=-1
    if bad=='past':due-=timedelta(days=1)
    if bad=='far':due+=timedelta(days=367)
    if bad=='missing':invoice='missing'
    if bad=='held':own.receivables[invoice].collection_hold=dict(active=True,evidence_id=ev);rev=review_revision(own)
    if bad=='overcommit':promise(own,'prior',amount=60000);rev=review_revision(own)
    if bad=='id':ident='../../bad'
    if bad=='source':ev='missing'
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':rev='old'
    before=state(own)
    with pytest.raises(CashflowError):create_promise(own,ident,invoice,amount,due,ev,'Checked.',rev,ctx=caller)
    assert state(own)==before


@pytest.mark.parametrize('bad',['missing','wrong_invoice','future','too_much','bool','agent','viewer','business','stale','unknown_step'])
def test_bad_promise_link_atomic(own,bad):
    promise(own);receipt(own,day=date(2027,1,2) if bad=='future' else own.as_of,invoice='custom-y' if bad=='wrong_invoice' else 'custom-x')
    rid='missing' if bad=='missing' else 'existing-r';amount=True if bad=='bool' else 10001 if bad=='too_much' else 5000
    caller=ctx(own,bad) if bad in {'agent','viewer'} else ctx(own,business='other') if bad=='business' else ctx(own)
    before=state(own)
    with pytest.raises(CashflowError):review_promise(own,'promise-one','invent_receipt' if bad=='unknown_step' else 'link',rid,amount,'customer-x','Checked.','old' if bad=='stale' else review_revision(own),ctx=caller)
    assert state(own)==before


def test_cancelled_promise_old_replays_do_not_restore_links_or_terms(own,tmp_path):
    promise(own);receipt(own);rev=review_revision(own);link(own,revision=rev);link(own,step='unlink',amount=0);link(own,step='cancel',rid='',amount=0)
    db=tmp_path/'promise.sqlite';save(db,own);loaded=_load(db);before=state(loaded)
    assert link(loaded,revision=rev)['duplicate'] and state(loaded)==before and loaded.payment_promises['promise-one']['cancelled']
    with pytest.raises(CashflowError):link(loaded)


def test_net_match_exact_signed_components_create_no_cash_and_all_readers_agree(own):
    bid=net_data(own);cash_before=own.actual_cash('USD');net(own,bid)
    report=statement_report(own,bid,ctx=ctx(own));assert report['rows'][0]['status']=='matched' and not report['ledger_movements_without_match']
    assert own.actual_cash('USD')==cash_before==129000 and not own.actions and not own.consent_map
    assert search_records(own,'bank-one',kind='statement',status='matched',ctx=ctx(own))['total']==1
    correct_cash_movement(own,'fee-one',1200,own.as_of,'supplier-source','Correct fee source.',review_revision(own),ctx=ctx(own))
    assert statement_report(own,bid,ctx=ctx(own))['rows'][0]['status']=='needs_review'
    assert search_records(own,'bank-one',kind='statement',status='needs_review',ctx=ctx(own))['total']==1


@pytest.mark.parametrize('bad',['sum','duplicate','one','missing','currency','date','agent','viewer','business','stale','row','source'])
def test_bad_net_match_atomic(own,bad):
    bid=net_data(own);keys=['receipt:receipt-a','receipt:receipt-b','other_cash:fee-one'];caller=ctx(own);rev=review_revision(own);rid='bank-one';ev='customer-x'
    if bad=='sum':keys=['receipt:receipt-a','receipt:receipt-b']
    if bad=='duplicate':keys=['receipt:receipt-a','receipt:receipt-a']
    if bad=='one':keys=['receipt:receipt-a']
    if bad=='missing':keys[0]='receipt:missing'
    if bad=='currency':own.cash_movements['fee-one']['currency']='EUR';rev=review_revision(own)
    if bad=='date':own.cash_movements['fee-one']['date']='2027-01-20';rev=review_revision(own)
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':rev='old'
    if bad=='row':rid='missing'
    if bad=='source':ev='missing'
    before=state(own)
    with pytest.raises(CashflowError):review_net_settlement(own,bid,rid,keys,ev,'Checked.',rev,ctx=caller)
    assert state(own)==before


def test_single_and_net_matches_share_global_exclusivity_and_old_retry_does_not_revive(own):
    bid=net_data(own);rev=review_revision(own);net(own,bid,revision=rev)
    second=statement(own,[dict(id='bank-one',currency='USD',signed_minor=20000,date='2027-01-01',reference='alias')],digest='c'*64)
    with pytest.raises(CashflowError):match(own,second,movement='receipt:receipt-a')
    match(own,bid,movement='',status='unmatched');match(own,second,movement='receipt:receipt-a')
    before=state(own);assert net(own,bid,revision=rev)['duplicate'] and state(own)==before
    assert statement_report(own,bid,ctx=ctx(own))['rows'][0]['status']=='unmatched'
    with pytest.raises(CashflowError):net(own,bid)


def test_unallocated_refund_cash_capacity_correction_and_allocations_are_separate(own):
    imported(own);split(own);old=copy.deepcopy(own.receipts);returned(own)
    assert own.actual_cash('USD')==210000 and split_unallocated(own,own.split_receipts['bulk-one'])==50000 and own.receipts==old
    correct_unallocated_refund(own,'return-one',20000,own.as_of,'customer-x','Source total correction.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==200000 and split_unallocated(own,own.split_receipts['bulk-one'])==40000
    more(own,{'custom-y':10000});assert own.actual_cash('USD')==200000
    with pytest.raises(CashflowError):correct_split_payment(own,'bulk-one',80000,own.as_of,'customer-x','Unbacked total.',review_revision(own),ctx=ctx(own))
    correct_split_payment(own,'bulk-one',90000,own.as_of,'customer-x','Correct observed parent total.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==170000 and split_unallocated(own,own.split_receipts['bulk-one'])==0
    assert own.split_receipts['bulk-one']['amount_minor']==120000 and own.unallocated_refunds['return-one']['amount_minor']==10000


@pytest.mark.parametrize('bad',['over','zero','bool','negative','inbox_only','missing','past','far','collision','source','agent','viewer','business','stale'])
def test_invalid_unallocated_refund_atomic(own,bad):
    imported(own)
    if bad!='inbox_only':split(own)
    amount=60001 if bad=='over' else 0 if bad=='zero' else True if bad=='bool' else -1 if bad=='negative' else 10000
    day=own.as_of-timedelta(days=1) if bad=='past' else own.as_of+timedelta(days=367) if bad=='far' else own.as_of
    caller=ctx(own,bad) if bad in {'agent','viewer'} else ctx(own,business='other') if bad=='business' else ctx(own)
    before=state(own)
    with pytest.raises(CashflowError):record_unallocated_refund(own,'missing' if bad=='missing' else 'bulk-one','bulk-one' if bad=='collision' else 'return-one',amount,day,
        'missing' if bad=='source' else 'customer-x','Checked.','old' if bad=='stale' else review_revision(own),ctx=caller)
    assert state(own)==before


def test_refund_frees_capacity_only_on_correction_date_and_shared_reports_include_cash(own):
    imported(own);split(own);snap=save_forecast(own,'Before refund',review_revision(own),ctx=ctx(own))['snapshot_id']
    returned(own,day=date(2027,1,3))
    with pytest.raises(CashflowError):more(own,{'custom-y':1000},day=date(2027,1,2))
    with pytest.raises(CashflowError):correct_split_payment(own,'bulk-one',110000,date(2027,1,2),'customer-x','Predates refund.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==220000 and own.actual_cash('USD',date(2027,1,3))==210000
    assert daily_preview(own,date(2027,1,4),ctx=ctx(own))['accounts'][0]['other_cash_minor']==-10000
    assert compare_forecast(own,snap,date(2027,1,3),ctx=ctx(own))['currencies']['USD'][-1]['recorded_closing_minor']==210000
    report=report_tables(own,ctx=ctx(own));events=report['tables'][4]
    assert any(r[1]=='unallocated_refund_observed' and r[4].minor==-10000 for r in events['rows'])
    assert search_records(own,'return-one',kind='refund',ctx=ctx(own))['total']==1
    own.roll_forward(date(2027,1,4),ctx=ctx(own));assert own.actual_cash('USD')==210000
    correct_unallocated_refund(own,'return-one',5000,own.as_of,'customer-x','Refund source correction.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD')==215000


def test_unallocated_refund_id_and_date_correction_are_guarded(own):
    imported(own);split(own);returned(own,day=date(2027,1,3));correct_payment_date(own,'unallocated_refund:return-one',date(2027,1,2),'customer-x','Correct source date.',review_revision(own),ctx=ctx(own))
    assert own.actual_cash('USD',date(2027,1,2))==210000 and 'return-one' in observation_ids(own)
    with pytest.raises(CashflowError):receipt(own,rid='return-one',amount=10)
    with pytest.raises(CashflowError):cash(own,ident='return-one',amount=10)
    with pytest.raises(CashflowError):correct_payment_date(own,'split_payment:bulk-one',date(2027,1,3),'customer-x','Predates return.',review_revision(own),ctx=ctx(own))


def test_party_statement_exact_role_currency_net_balances_and_csv_injection(own):
    own.receivables['custom-x'].counterparty='=Same Party';own.receivables['custom-y'].counterparty='=Same Party';own.outflows['materials'].counterparty='=Same Party'
    own.accounts['euro']=Account('euro','EUR',10000,1000);own.receivables['eu']=Receivable('eu','EUR',5000,date(2027,1,15),'customer-x',counterparty='=Same Party')
    receipt(own,amount=10000);before=state(own);report=party_statement(own,'customer','=Same Party',ctx=ctx(own,'viewer'))
    assert report['summary']['USD']['source_total_minor']==110000 and report['summary']['USD']['remaining_minor']==100000 and report['summary']['EUR']['remaining_minor']==5000
    assert not any(r['kind']=='bill' for r in report['rows']) and state(own)==before
    assert "'=Same Party" in party_csv(report) and len(party_statement(own,'customer','=Same Party','EUR',ctx=ctx(own))['summary'])==1
    assert party_statement(own,'supplier','=Same Party',ctx=ctx(own))['summary']['USD']['remaining_minor']==50000
    with pytest.raises(CashflowError):party_statement(own,'customer','same party',ctx=ctx(own))
    with pytest.raises(AuthorizationError):party_statement(own,'customer','=Same Party',ctx=ctx(own,business='other'))


def test_party_split_receipts_do_not_claim_another_cash_stream(own):
    own.receivables['custom-x'].counterparty='Customer X';imported(own);split(own);returned(own)
    report=party_statement(own,'customer','Customer X',ctx=ctx(own))
    assert report['summary']['USD']['net_settled_minor']==40000 and report['summary']['USD']['remaining_minor']==25000
    assert any(r['kind']=='split_allocation' and 'no additional cash' in r['note'] for r in report['rows'])
    assert not any(r['id']=='return-one' for r in report['rows'])  # unallocated return has no assigned party


@pytest.mark.parametrize('filename,data',[('note.txt',b'Synthetic original note'),('rows.csv',b'id,amount\nx,1.00\n'),('original.pdf',b'%PDF-1.4\nSYNTHETIC\n%%EOF'),('image.png',b'\x89PNG\r\n\x1a\nSYNTHETIC'),('photo.jpg',b'\xff\xd8SYNTHETIC\xff\xd9')])
def test_attachment_original_bytes_roundtrip_download_integrity_and_no_model_or_audit_payload(own,tmp_path,filename,data):
    result=attachment(own,filename,data);aid=result['attachment_id'];db=tmp_path/'files.sqlite';save(db,own);loaded=_load(db)
    actual,meta=attachment_bytes(loaded,aid,ctx=ctx(loaded,'viewer'));assert actual==data and meta['sha256']==hashlib.sha256(data).hexdigest()
    assert 'payload_base64' not in json.dumps(attachment_inventory(loaded,ctx=ctx(loaded)))
    output=json.dumps(_tool_get_evidence(ctx(loaded,'agent'),loaded,{},'mock'))
    audit=json.dumps(web._audit_export(loaded))
    assert base64.b64encode(data).decode() not in output+audit and 'payload_base64' not in output+audit
    assert loaded.actual_cash('USD')==100000 and not loaded.runtime_evidence_ids


@pytest.mark.parametrize('bad',['empty','large','path','windows_path','control','html','exe','wrong_pdf','wrong_png','wrong_jpg','binary_text','bad_utf8','unknown_source','note','agent','viewer','business','stale'])
def test_invalid_attachment_atomic(own,bad):
    filename='note.txt';data=b'Synthetic';ev='customer-x';note='Checked.';caller=ctx(own);rev=review_revision(own)
    if bad=='empty':data=b''
    if bad=='large':data=b'x'*(1024*1024+1)
    if bad=='path':filename='../note.txt'
    if bad=='windows_path':filename='C:\\secret.txt'
    if bad=='control':filename='bad\nname.txt'
    if bad=='html':filename='page.html'
    if bad=='exe':filename='run.exe'
    if bad=='wrong_pdf':filename='fake.pdf'
    if bad=='wrong_png':filename='fake.png'
    if bad=='wrong_jpg':filename='fake.jpg'
    if bad=='binary_text':data=b'a\x00b'
    if bad=='bad_utf8':data=b'\xff\xfe'
    if bad=='unknown_source':ev='missing'
    if bad=='note':note=' '
    if bad in {'agent','viewer'}:caller=ctx(own,bad)
    if bad=='business':caller=ctx(own,business='other')
    if bad=='stale':rev='old'
    before=state(own)
    with pytest.raises(CashflowError):attach_evidence(own,ev,filename,data,note,rev,ctx=caller)
    assert state(own)==before


def test_attachment_retire_checksum_scope_and_exact_old_retry_never_revives(own):
    data=b'Original evidence bytes';rev=review_revision(own);aid=attachment(own,data=data,revision=rev)['attachment_id']
    with pytest.raises(AuthorizationError):attachment_bytes(own,aid,ctx=ctx(own,business='other'))
    own.evidence_attachments[aid]['payload_base64']=base64.b64encode(b'TAMPERED').decode()
    with pytest.raises(CashflowError):attachment_bytes(own,aid,ctx=ctx(own))
    own.evidence_attachments[aid]['payload_base64']=base64.b64encode(data).decode()
    retire_attachment(own,aid,'Retire active source bytes.',review_revision(own),ctx=ctx(own));before=state(own)
    assert attachment(own,data=data,revision=rev)['duplicate'] and state(own)==before
    assert own.evidence_attachments[aid]['payload_base64'] is None and not own.evidence_attachments[aid]['active']
    with pytest.raises(CashflowError):attachment_bytes(own,aid,ctx=ctx(own))
    with pytest.raises(CashflowError):attachment(own,data=data)


def test_attachment_total_storage_bound_and_backups_retain_explicit_originals(own,tmp_path):
    for n in range(8):attachment(own,filename=f'source-{n}.txt',data=b'x'*(1024*1024))
    with pytest.raises(CashflowError):attachment(own,filename='overflow.txt',data=b'x')
    db=tmp_path/'vault.sqlite';save(db,own);backup=recovery.create_backup(db,'Original bytes retained.',ctx=ctx(own));aid=next(iter(own.evidence_attachments))
    retire_attachment(own,aid,'Retire one active source.',review_revision(own),ctx=ctx(own));save(db,own)
    preview=recovery.restore_preview(db,backup['id'],ctx=ctx(own));assert preview['restored']['active_attachment_bytes']==8*1024*1024
    recovery.restore_backup(db,backup['id'],preview['revision'],own.business_id,ctx=ctx(own))
    assert len(attachment_bytes(_load(db),aid,ctx=ctx(own))[0])==1024*1024


def test_new_metadata_persists_and_no_new_model_mutation_tools(own,tmp_path):
    promise(own);bid=net_data(own);net(own,bid);imported(own);split(own);returned(own);attachment(own)
    db=tmp_path/'all.sqlite';save(db,own);loaded=_load(db);assert state(loaded)==state(own)
    assert len(TOOLS)==8
    for name in ['setup_business','create_promise','review_net_settlement','record_unallocated_refund','attach_evidence']:
        with pytest.raises(CashflowError):execute_tool(ctx(loaded,'agent'),loaded,name,{},'mock')


def test_promises_attachments_and_returns_remain_searchable_without_original_bytes(own):
    promise(own);aid=attachment(own)['attachment_id'];imported(own);split(own);returned(own)
    assert search_records(own,'promise-one',kind='promise',ctx=ctx(own))['total']==1
    assert search_records(own,'source.txt',kind='attachment',ctx=ctx(own))['total']==1
    assert 'payload_base64' not in json.dumps(search_records(own,kind='attachment',ctx=ctx(own)))
    calendar=cash_calendar(own,days=7,ctx=ctx(own));assert len(calendar['promises'])==1
    assert 'Original files (1)' in web.evidence_link(own,'customer-x')


def test_cancelled_promise_link_can_be_corrected_without_reviving_terms(own):
    promise(own);receipt(own);link(own);link(own,step='cancel',rid='',amount=0)
    link(own,step='unlink',amount=0)
    assert own.payment_promises['promise-one']['cancelled'] and not own.payment_promises['promise-one']['links']
    with pytest.raises(CashflowError):link(own)


@pytest.mark.parametrize('path',['/setup','/promises','/batch-matching','/unallocated-refunds','/parties','/attachments'])
def test_workbench_pages_readonly_honest_and_csrf(live_server,path):
    base,db=live_server;before=state(_load(db));status,body=_get(base,path)
    assert status==200 and 'SYNTHETIC DEMO' in body and 'AI not connected' in body and state(_load(db))==before
    assert _post(base,'/workbench/confirm',dict(csrf='wrong',confirmed='yes',operation='promise_create'))[0]==403


def token(base,path='/setup'):
    _,body=_get(base,path);return re.search("name='csrf' value='([^']+)'",body)[1]


def test_http_setup_preview_explicit_replace_backup_and_replay(live_server):
    base,db=live_server;old=_load(db);fields=dict(csrf=token(base),operation='setup',revision=review_revision(old),id='http-starter',name='Synthetic HTTP Starter',base_currency='USD',timezone='UTC',as_of='2027-02-01',horizon_days='30',synthetic='yes',
        account_0_id='cash',account_0_currency='USD',account_0_opening='1000.00',account_0_floor='200.00',account_0_source_note='Checked synthetic opening.')
    before=state(old);assert _post(base,'/workbench/preview',fields)[0]==200 and state(_load(db))==before
    assert _post(base,'/workbench/confirm',dict(fields,confirmed='yes',target_business_confirmation='wrong'))[0]==400
    assert _post(base,'/workbench/confirm',dict(fields,confirmed='yes',target_business_confirmation='http-starter'))[0]==200
    loaded=_load(db);assert loaded.business_id=='http-starter' and loaded.actual_cash('USD')==100000
    backups=recovery.list_backups(db,ctx=ctx(loaded));assert backups
    assert recovery.restore_preview(db,backups[0]['id'],ctx=ctx(loaded))['restored']['business_id']==old.business_id
    before=state(loaded);assert _post(base,'/workbench/confirm',dict(fields,confirmed='yes',target_business_confirmation='http-starter'))[0]==200
    assert state(_load(db))==before


def test_http_attachment_preview_bytes_download_headers_retire_and_post_restart_replay(live_server,own):
    base,db=live_server;save(db,own);csrf=token(base,'/attachments');data=b'SYNTHETIC FILE CONTENT THAT MUST NOT APPEAR IN AUDIT'
    fields=dict(csrf=csrf,revision=review_revision(own),evidence_id='customer-x',note='Original synthetic evidence.')
    before=state(_load(db));status,body=_mpost(base,'/attachments/preview',fields,dict(file=('source.txt',data,'text/plain')))
    assert status==200 and state(_load(db))==before and data.decode() not in body
    stage=re.search("name='token' value='([^']+)'",body)[1];confirm=dict(csrf=csrf,token=stage,confirmed='yes')
    assert _post(base,'/attachments/confirm',confirm)[0]==200;ledger=_load(db);aid=next(iter(ledger.evidence_attachments))
    with urllib.request.urlopen(base+f'/attachments/{aid}/download') as r:
        assert r.read()==data and r.headers['X-Content-Type-Options']=='nosniff' and 'attachment;' in r.headers['Content-Disposition'] and r.headers['Cache-Control']=='no-store'
    web._STAGED.pop(stage,None);assert _post(base,'/attachments/confirm',confirm)[0]==200
    retire_attachment(ledger,aid,'Remove active source bytes.',review_revision(ledger),ctx=ctx(ledger));save(db,ledger)
    before=state(_load(db));assert _post(base,'/attachments/confirm',confirm)[0]==200 and state(_load(db))==before
    assert _post(base,'/attachments/confirm',dict(confirm,role='agent'))[0]==400
    with pytest.raises(urllib.error.HTTPError) as err:urllib.request.urlopen(base+f'/attachments/{aid}/download')
    assert err.value.code==400


def test_http_net_multiselect_confirmation_concurrency_and_party_download(live_server,own):
    base,db=live_server;own.receivables['custom-x'].counterparty='Synthetic Customer';bid=net_data(own);save(db,own);csrf=token(base,'/batch-matching')
    fields=dict(csrf=csrf,operation='net_match',revision=review_revision(own),batch_id=bid,row_id='bank-one',movement_keys=['receipt:receipt-a','receipt:receipt-b','other_cash:fee-one'],evidence_id='customer-x',note='Checked exact net source.')
    def post(values):
        req=urllib.request.Request(base+'/workbench/confirm',data=urllib.parse.urlencode(values,doseq=True).encode(),headers={'Content-Type':'application/x-www-form-urlencoded'})
        with urllib.request.urlopen(req) as r:return r.status
    with ThreadPoolExecutor(max_workers=3) as pool:assert list(pool.map(lambda _:post(dict(fields,confirmed='yes')),range(3)))==[200]*3
    ledger=_load(db);assert ledger.actual_cash('USD')==129000 and statement_report(ledger,bid,ctx=ctx(ledger))['rows'][0]['status']=='matched'
    url='/parties/export.csv?'+urllib.parse.urlencode(dict(role='customer',party='Synthetic Customer'))
    with urllib.request.urlopen(base+url) as r:assert r.status==200 and 'Synthetic Customer' in r.read().decode('utf-8-sig')


def test_attachment_staging_is_bounded_expires_and_rejects_stale_source_review(live_server,own):
    base,db=live_server;save(db,own);csrf=token(base,'/attachments')
    # Only this feature's unconfirmed stages are changed; other import types stay untouched.
    old={k:v for k,v in web._STAGED.items() if v.get('kind')=='evidence-attachment'}
    for k in old:web._STAGED.pop(k)
    try:
        fields=dict(csrf=csrf,revision=review_revision(own),evidence_id='customer-x',note='Checked synthetic file.')
        stages=[]
        for n in range(4):
            status,body=_mpost(base,'/attachments/preview',fields,dict(file=(f'source-{n}.txt',b'synthetic','text/plain')))
            assert status==200;stages.append(re.search("name='token' value='([^']+)'",body)[1])
        assert _mpost(base,'/attachments/preview',fields,dict(file=('overflow.txt',b'synthetic','text/plain')))[0]==400
        web._STAGED[stages[0]]['expires']=0
        assert _post(base,'/attachments/confirm',dict(csrf=csrf,token=stages[0],confirmed='yes'))[0]==400
        assert _mpost(base,'/attachments/preview',fields,dict(file=('new-slot.txt',b'synthetic','text/plain')))[0]==200
        ledger=_load(db);receipt(ledger);save(db,ledger)
        assert _post(base,'/attachments/confirm',dict(csrf=csrf,token=stages[1],confirmed='yes'))[0]==400
        assert not _load(db).evidence_attachments
    finally:
        for k,v in list(web._STAGED.items()):
            if v.get('kind')=='evidence-attachment':web._STAGED.pop(k)
        web._STAGED.update(old)

