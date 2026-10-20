"""English local owner previews for statement balances, credits and existing debt."""
import copy
import csv
import re
from datetime import date
from io import StringIO
from urllib.parse import urlencode

from .errors import CashflowError
from .owner_workflows import review_revision
from .obligations import (check_statement_balance, statement_balance_report, carry_credit,
    reverse_credit, register_loan, loan_report, void_loan_registration)


def mount(web):
    esc,money=web.esc,web.money
    links="<p><a href='/entries'>Owner entries</a> · <a href='/statement-balances'>Statement balance checks</a> · <a href='/credits'>Credit carry-forward</a> · <a href='/loans'>Loans / EMI</a></p>"
    # Owner-entry navigation is composed before the shared workspace layout.
    web.CSS+='\n.debt{overflow-wrap:anywhere}.debt select{max-width:100%}.debt td,.debt th{white-space:normal}.debt code{word-break:break-all}.debt .kv b{white-space:normal;max-width:100%}.debt .table-wrap{overflow-x:auto;max-width:100%}.debt table{min-width:620px}.debt form>label{display:block;margin:10px 0}.debt form>label>input:not([type=checkbox]),.debt form>label>select,.debt form>label>textarea{display:block;width:100%;box-sizing:border-box}\n'
    def page(title,body):return web.page(title,"<div class='debt'><h1>"+esc(title)+'</h1>'+links+body+'</div>',active='/entries')
    def query(req,name,default=''):return req.query.get(name,[default])[0]
    def day(value):
        try:return date.fromisoformat(value)
        except (ValueError,TypeError):raise CashflowError('Use a valid calendar date.')
    def amount(value,currency,signed=False):
        if not re.fullmatch(r'-?[0-9]{1,13}(\.[0-9]{1,3})?' if signed else r'[0-9]{1,13}(\.[0-9]{1,3})?',value):
            raise CashflowError('Use an ordinary money amount in the selected currency.')
        return web._parse_major_to_minor(value,currency)
    def fields(req,allowed):
        if set(req.form)-set(allowed)-{'csrf','confirmed'} or any(len(v)!=1 for v in req.form.values()):
            raise CashflowError('Unexpected or repeated owner review field.')
    def hidden(ledger,op):return web.csrf_field()+f"<input type='hidden' name='operation' value='{op}'><input type='hidden' name='revision' value='{review_revision(ledger)}'>"
    def form(ledger,op):return "<form method='post' action='/obligations/preview'>"+hidden(ledger,op)
    def end(label='Preview exact terms'):return f"<button type='submit'>{label}</button></form>"
    def options(values):return ''.join(f"<option value='{esc(v)}'>{esc(label)}</option>" for v,label in values)
    def source(ledger):return "<label>Checked source evidence<select name='evidence_id' required><option value=''>Choose evidence</option>"+options((e.id,e.id+' — '+e.summary[:60]) for e in ledger.evidence.values())+"</select></label><label>Source checks / reason<textarea name='note' maxlength='1000' required></textarea></label>"
    def parse(ledger,req):
        op=req.field('operation');ctx=web._owner_ctx(ledger)
        ev,note,rev=req.field('evidence_id'),req.field('note'),req.field('revision')
        base={'operation','revision','evidence_id','note'}
        if op=='statement':
            fields(req,base|{'check_id','batch_id','account_id','start','end','opening','closing','previous_id'})
            account=ledger.accounts.get(req.field('account_id'))
            if account is None:raise CashflowError('Choose an account in this business.')
            result=check_statement_balance(ledger,req.field('check_id'),req.field('batch_id'),account.id,
                day(req.field('start')),day(req.field('end')),amount(req.field('opening'),account.currency,True),
                amount(req.field('closing'),account.currency,True),req.field('previous_id'),ev,note,rev,ctx=ctx)
            return result,'/statement-balances?'+urlencode(dict(check_id=result['check_id']))
        if op=='carry':
            fields(req,base|{'transfer_id','kind','source_id','target_id','amount'})
            kind=req.field('kind');group=ledger.receivables if kind=='invoice' else ledger.outflows if kind=='bill' else {}
            item=group.get(req.field('source_id'))
            if item is None:raise CashflowError('Choose a source in the selected role.')
            return carry_credit(ledger,req.field('transfer_id'),kind,item.id,req.field('target_id'),amount(req.field('amount'),item.currency),ev,note,rev,ctx=ctx),'/credits'
        if op=='reverse':
            fields(req,base|{'transfer_id'})
            return reverse_credit(ledger,req.field('transfer_id'),ev,note,rev,ctx=ctx),'/credits'
        if op=='loan':
            fields(req,base|{'id','lender','reference','currency','opening_principal','schedule','already_in_opening','not_recorded_elsewhere'})
            ccy=req.field('currency');rows=[]
            for row in csv.reader(StringIO(req.field('schedule'))):
                if not row or not any(v.strip() for v in row):continue
                if len(rows)>=60:raise CashflowError('Use at most 60 instalments.')
                if len(row)!=3:raise CashflowError('Each line needs date, principal and interest, separated by commas.')
                rows.append(dict(date=row[0].strip(),principal_minor=amount(row[1].strip(),ccy),interest_minor=amount(row[2].strip(),ccy)))
            spec=dict(id=req.field('id'),lender=req.field('lender'),reference=req.field('reference'),currency=ccy,
                opening_principal_minor=amount(req.field('opening_principal'),ccy),schedule=rows,evidence_id=ev,note=note,
                already_in_opening=req.field('already_in_opening')=='yes',not_recorded_elsewhere=req.field('not_recorded_elsewhere')=='yes')
            return register_loan(ledger,spec,rev,ctx=ctx),'/loans'
        if op=='loan_void':
            fields(req,base|{'loan_id'})
            return void_loan_registration(ledger,req.field('loan_id'),ev,note,rev,ctx=ctx),'/loans'
        raise CashflowError('Choose an owner operation from these forms.')

    def statement_view(ledger,ident):
        r=statement_balance_report(ledger,ident,ctx=web._owner_ctx(ledger));ccy=r['currency']
        body=f"<div class='card'><h2>{esc(ident)} — {esc(r['status'].replace('_',' '))}</h2><p>{esc(r['batch_id'])} · {esc(r['account_id'])} · {esc(r['start'])} through {esc(r['end'])} inclusive.</p>"
        for label,key in [('Statement opening','opening_minor'),('Signed imported rows','net_minor'),('Statement closing','closing_minor'),
            ('Arithmetic difference','arithmetic_delta_minor'),('Previous closing / opening difference','previous_delta_minor'),
            ('Ledger opening for this period','ledger_opening_minor'),('Ledger closing','ledger_closing_minor'),
            ('Statement minus ledger opening','opening_delta_minor'),('Statement minus ledger closing','closing_delta_minor')]:
            body+=f"<div class='kv stack'><span>{esc(label)}</span><b>{money(r[key],ccy) if r[key] is not None else 'Unconfirmed / unavailable'}</b></div>"
        body+=''.join('<p class="err">'+esc(p)+'</p>' for p in r['problems'])
        return body+'<p>'+esc(r['note_boundary'])+'</p>'+web.evidence_link(ledger,r['evidence_id'])+'</div>'

    @web.route('GET','/statement-balances')
    def statements(handler,req):
        def build(ledger):
            body="<p>Compare inclusive period arithmetic, linked statement boundaries and recorded ledger cash. These source-attested checks never change a cash balance. Imported CSV rows alone do not prove completeness.</p><p><a href='/statements'>Import / match statement transactions</a></p>"
            if ledger.statement_batches:
                body+="<div class='card'><h2>Record boundary terms</h2>"+form(ledger,'statement')+"<label>New check ID<input name='check_id' required></label>"
                body+="<label>Imported statement<select name='batch_id'>"+options((k,k) for k in ledger.statement_batches)+"</select></label><label>Account<select name='account_id'>"+options((a.id,a.id+' · '+a.currency) for a in ledger.accounts.values())+'</select></label>'
                body+="<label>Period start<input type='date' name='start' required></label><label>Period end (inclusive)<input type='date' name='end' required></label><label>Statement opening<input name='opening' inputmode='decimal' required></label><label>Statement closing<input name='closing' inputmode='decimal' required></label><label>Previous boundary check<select name='previous_id'><option value=''>Unlinked / first period</option>"+options((k,k+' · '+c['end']) for k,c in ledger.statement_balance_checks.items())+'</select></label>'+source(ledger)+end()+'</div>'
            ident=query(req,'check_id')
            if ident:body+=statement_view(ledger,ident)
            for k,c in reversed(list(ledger.statement_balance_checks.items())):
                body+=f"<p><a href='/statement-balances?{urlencode(dict(check_id=k))}'>{esc(k)}</a> · {esc(c['currency'])} · {esc(c['start'])} to {esc(c['end'])}.</p>"
            return page('Statement balance checks',body)
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/credits')
    def credits(handler,req):
        def build(ledger):
            body="<p>Apply existing credit to another invoice or bill for the exact same party, role and currency. This is noncash settlement: no receipt, refund, approval or counterparty consent is created. Checked sources must support the application.</p><p>Active carried credit locks related source/identity/refund corrections. Reverse dependent transfers in reverse order before correcting their sources. Original history is retained.</p>"
            for kind,group in [('invoice',ledger.receivables),('bill',ledger.outflows)]:
                sources=[r for r in group.values() if (r.credit_balance_minor if kind=='invoice' else r.overpaid_minor)>0]
                targets=[r for r in group.values() if r.remaining_minor>0]
                if sources and targets:
                    body+=f"<div class='card'><h2>Apply {'customer' if kind=='invoice' else 'supplier'} credit</h2>"+form(ledger,'carry')+f"<input type='hidden' name='kind' value='{kind}'><label>Unique transfer ID<input name='transfer_id' required></label><label>Source credit<select name='source_id'>"+options((r.id,r.id+' · '+r.currency+' · '+r.counterparty) for r in sources)+"</select></label><label>Target outstanding<select name='target_id'>"+options((r.id,r.id+' · '+r.currency+' · '+r.counterparty) for r in targets)+"</select></label><label>Credit amount<input name='amount' inputmode='decimal' required></label>"+source(ledger)+end()+'</div>'
                for item in sources:
                    body+=f"<p>{esc(kind)} {esc(item.id)}: available {money(item.credit_balance_minor if kind=='invoice' else item.overpaid_minor,item.currency)} · {esc(item.counterparty)}.</p>"
            body+="<p><a href='/refunds'>Review an observed customer / supplier refund instead</a>. Credit application and actual refund are separate owner operations; neither page executes a transfer.</p>"
            if not ledger.credit_transfers:body+='<p>No carried credit recorded.</p>'
            for t in reversed(list(ledger.credit_transfers.values())):
                body+=f"<div class='card'><h2>{esc(t['id'])} — {'reversed' if t['reversed'] else 'active'}</h2><p>{esc(t['source_id'])} → {esc(t['target_id'])} · {money(t['amount_minor'],t['currency'])} · {esc(t['party'])} · {esc(t['date'])}.</p>"+web.evidence_link(ledger,t['evidence_id'])
                if not t['reversed']:body+=form(ledger,'reverse')+f"<input type='hidden' name='transfer_id' value='{esc(t['id'])}'>"+source(ledger)+end('Preview reversal')
                body+='</div>'
            return page('Credit carry-forward',body)
        return handler._send_html(200,web.read_state(build))

    def loan_view(ledger,controls=True):
        body=''
        for loan in loan_report(ledger,ctx=web._owner_ctx(ledger)):
            if loan.get('voided'):
                body+=f"<div class='card'><h2>{esc(loan['id'])} — erroneous registration voided</h2><p>Excluded from current obligations. Original principal {money(loan['opening_principal_minor'],loan['currency'])} and original schedule remain in the audit. This corrects a duplicate/erroneous ledger registration; it is not debt forgiveness or payment.</p>"+web.evidence_link(ledger,loan['voided']['evidence_id'])+'</div>'
                continue
            ccy=loan['currency'];body+=f"<div class='card'><h2>{esc(loan['id'])} · {esc(loan['lender'])}</h2><p>Agreement {esc(loan['reference'])}; through snapshot {ledger.as_of}: outstanding principal {money(loan['outstanding_principal_minor'],ccy)}, unpaid interest {money(loan['unpaid_interest_minor'],ccy)}, total unpaid {money(loan['unpaid_total_minor'],ccy)}.</p><div class='table-wrap'><table><thead><tr><th>Due / bill</th><th>Principal</th><th>Interest</th><th>Recorded settled (all dates)</th><th>Remaining schedule</th></tr></thead><tbody>"
            for r in loan['rows']:
                body+=f"<tr><td>{esc(r['date'])}<br>{esc(r['bill_id'])}{'<br>Overdue' if r['overdue'] else ''}</td><td>{money(r['principal_minor'],ccy)}</td><td>{money(r['interest_minor'],ccy)}</td><td>{money(r['paid_minor'],ccy)}</td><td>{money(r['remaining_minor'],ccy)}</td></tr>"
            body+='</tbody></table></div><p>'+esc(loan['note_boundary'])+'</p>'+web.evidence_link(ledger,loan['evidence_id'])+'</div>'
            if controls and not any(ledger.outflows[r['bill_id']].observed_payments or ledger.outflows[r['bill_id']].legacy_payment for r in loan['rows']):
                body+="<div class='card'><h3>Correct an erroneous registration</h3><p>Before any payment history, void an erroneous/duplicate registration with checked evidence, then register corrected terms under a new ID. This does not forgive real debt.</p>"+form(ledger,'loan_void')+f"<input type='hidden' name='loan_id' value='{esc(loan['id'])}'>"+source(ledger)+end('Preview erroneous-registration correction')+'</div>'
        return body

    @web.route('GET','/loans')
    def loans(handler,req):
        def build(ledger):
            body="<p>Register an existing source-backed loan's remaining principal and exact agreed instalments. No loan proceeds, approval, interest calculation or external acceptance is invented. Instalments become protected scheduled bills and enter forecasts once.</p><p><a href='/payments'>Record an observed instalment payment</a> · <a href='/payment-corrections'>Correct an observed payment</a> · <a href='/calendar'>Cash calendar</a></p>"
            body+=loan_view(ledger)+"<div class='card'><h2>Register existing debt schedule</h2>"+form(ledger,'loan')+"<label>Unique loan ID (up to 40 characters)<input name='id' maxlength='40' required></label><label>Lender<input name='lender' maxlength='200' required></label><label>Agreement reference<input name='reference' maxlength='200' required></label><label>Currency<select name='currency'>"+options((a.currency,a.currency) for a in ledger.accounts.values())+"</select></label><label>Outstanding principal at this snapshot<input name='opening_principal' inputmode='decimal' required></label><label>Agreed instalments: one line per date, principal, interest<textarea name='schedule' rows='7' placeholder='2027-01-15, 500.00, 20.00&#10;2027-02-15, 500.00, 10.00' required></textarea></label><p>Use ordinary currency amounts, a decimal point and no thousands separators. Dates must be distinct and increasing. Principal portions must equal outstanding principal. Only dates inside the current forecast horizon appear in its daily chart; the full schedule remains here.</p>"+source(ledger)+"<label><input type='checkbox' name='already_in_opening' value='yes' required> This debt's cash proceeds are already represented in the opening / existing observed cash; no new drawdown is requested.</label><label><input type='checkbox' name='not_recorded_elsewhere' value='yes' required> I checked these future instalments are not already entered as other bills or recurring costs.</label>"+end()+'</div>'
            return page('Loans / EMI schedule',body)
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/obligations/preview')
    def preview(handler,req):
        def build(ledger):
            work=copy.deepcopy(ledger);result,_=parse(work,req);op=req.field('operation')
            body='<p>No saved state changed. Review exact source-backed terms.</p><div class="card"><h2>Proposed inputs</h2>'
            for name,values in req.form.items():
                if name not in {'csrf','revision','confirmed'}:body+=f"<div class='kv stack'><span>{esc(name.replace('_',' '))}</span><b>{esc(values[0])}</b></div>"
            body+='</div>'
            if op=='statement':body+=statement_view(work,result['check_id'])
            if op in {'loan','loan_void'}:body+=loan_view(work,False)
            body+="<div class='card'><h2>Recorded cash before → after</h2>"
            for a in ledger.accounts.values():body+=f"<p>{esc(a.currency)} through {ledger.as_of}: {money(ledger.actual_cash(a.currency),a.currency)} → {money(work.actual_cash(a.currency),a.currency)}.</p>"
            if op in {'loan','loan_void'}:
                from .forecast import forecast
                before,after=forecast(ledger,ledger.as_of,ledger.horizon_days),forecast(work,work.as_of,work.horizon_days)
                for ccy,f in after.currencies.items():body+=f"<p>{esc(ccy)} forecast closing: {money(before.currencies[ccy].closing_minor,ccy)} → {money(f.closing_minor,ccy)}; shortfall after {money(f.worst_shortfall_minor,ccy)}.</p>"
            body+='</div><form method="post" action="/obligations/confirm">'+web.csrf_field()
            for name,values in req.form.items():
                if name not in {'csrf','confirmed'}:body+=f"<input type='hidden' name='{esc(name)}' value='{esc(values[0])}'>"
            body+="<label><input type='checkbox' name='confirmed' value='yes' required> I reviewed these exact terms and explicitly confirm this synthetic owner operation. No money or external message is sent.</label>"+end('Confirm exact operation')
            return page('Review statement / credit / debt operation',body)
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/obligations/confirm')
    def confirm(handler,req):
        if req.field('confirmed')!='yes':raise CashflowError('Review and explicitly confirm the owner operation.')
        _,dest=web.mutate_state(lambda ledger:parse(ledger,req))
        return handler._redirect(dest)
