"""Reviewed statement column mapping; no bank I/O or automatic cash entries."""
import copy
import json
from urllib.parse import quote

from .errors import CashflowError
from .owner_workflows import review_revision
from .statement_mapping import DEFAULT_MAPPING, normalize_statement, import_mapped_statement


def mount(web):
    esc=web.esc
    def fields(req,allowed):
        if set(req.form)-set(allowed)-{'csrf'} or any(len(v)!=1 for v in req.form.values()):
            raise CashflowError('Unexpected/repeated statement mapping field. Reload the form.')

    @web.route('GET','/statement-mapping')
    def page(handler,req):
        def build(ledger):
            selected=req.query.get('profile',[''])[0];mapping=DEFAULT_MAPPING
            if selected:
                profile=ledger.statement_profiles.get(selected)
                if profile is None or profile['business_id']!=ledger.business_id:raise CashflowError('Choose a profile for this business.')
                mapping=profile['mapping']
            body="<h1>Reviewed statement mapping</h1><p><a href='/statements'>Statement reconciliation</a> · <a href='/statement-balances'>Boundary checks</a></p><p>Local synthetic CSV evidence only. Import never creates cash or receipts, verifies a bank, grants consent or reconciles an action.</p>"
            body+="<div class='card'><h2>Reusable parsing profiles</h2><p>Profiles remember column/date/number rules only. Choose the account and period for every upload.</p><form method='get'><label>Profile<select name='profile'><option value=''>Standard column mapping</option>"
            for p in ledger.statement_profiles.values():body+=f"<option value='{esc(p['id'])}' {'selected' if p['id']==selected else ''}>{esc(p['name'])}</option>"
            body+="</select></label><button>Load profile</button></form></div>"
            body+=f"<div class='card'><h2>Map and preview a file</h2><p>UTF-8 CSV, up to 1 MiB and 100 nonzero transactions. No grouping separators, currency symbols or inferred dates. A header-only CSV declares zero activity; this assertion still needs boundary/source review.</p><form method='post' action='/statement-mapping/preview' enctype='multipart/form-data'>{web.csrf_field()}<input type='hidden' name='business_id' value='{esc(ledger.business_id)}'><label>Synthetic CSV<input type='file' name='file' accept='.csv' required></label><label>Own account<select name='account_id' required>"
            for a in ledger.accounts.values():body+=f"<option value='{esc(a.id)}'>{esc(a.id+' · '+a.currency)}</option>"
            body+=f"</select></label><label>Statement start (inclusive)<input type='date' name='start' value='{ledger.as_of}' required></label><label>Statement end (inclusive)<input type='date' name='end' value='{ledger.as_of}' required></label>"
            for key,label,options in [
                ('delimiter','CSV delimiter', [(',', 'Comma'),(';','Semicolon'),('\t','Tab')]),
                ('date_format','Exact date format',[('iso','YYYY-MM-DD'),('dmy','DD/MM/YYYY'),('mdy','MM/DD/YYYY')]),
                ('mode','Amount mode',[('signed','Signed amount: positive in, negative out'),('debit_credit','Separate debit (out) and credit (in)')]),
                ('decimal','Decimal separator',[('.','Dot'),(',','Comma')])]:
                body+=f"<label>{label}<select name='{key}'>"+''.join(f"<option value='{esc(v)}' {'selected' if mapping[key]==v else ''}>{esc(t)}</option>" for v,t in options)+'</select></label>'
            for key,label in [('id_column','Row ID column (blank generates stable file row IDs)'),('date_column','Date column'),('reference_column','Reference column (optional)'),('currency_column','Currency column (blank uses the explicitly selected account)'),('amount_column','Signed amount column (signed mode only)'),('debit_column','Debit column (separate mode only)'),('credit_column','Credit column (separate mode only)')]:
                body+=f"<label>{label}<input name='{key}' value='{esc(mapping[key])}' maxlength='120'></label>"
            body+="<label>Save these parsing rules under a new profile name (optional)<input name='profile_name' maxlength='80'></label><label>Statement source / scope check<textarea name='note' required maxlength='1000'></textarea></label><button type='submit'>Preview exact normalized rows</button></form></div>"
            return web.page('Statement mapping',body,active='/statements')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/statement-mapping/preview')
    def preview(handler,req):
        fields(req,set(DEFAULT_MAPPING)|{'business_id','account_id','start','end','note','profile_name'})
        up=req.files.get('file')
        if set(req.files)!={'file'} or not up or not up['filename'].lower().endswith('.csv'):raise CashflowError('Upload one synthetic CSV.')
        def build(ledger):
            if req.field('business_id')!=ledger.business_id:raise CashflowError('The active business changed. Reload the mapping form.')
            normalized=normalize_statement(ledger,up['data'],{k:req.field(k) for k in DEFAULT_MAPPING},req.field('account_id'),req.field('start'),req.field('end'),ctx=web._owner_ctx(ledger))
            stage=dict(kind='statement_mapping',normalized=normalized,note=req.field('note'),profile_name=req.field('profile_name'),revision=review_revision(ledger))
            import_mapped_statement(copy.deepcopy(ledger),normalized,stage['note'],stage['profile_name'],stage['revision'],ctx=web._owner_ctx(ledger))
            token=web._stage(stage);context=normalized['context']
            body=f"<h1>Review mapped statement</h1><p style='overflow-wrap:anywhere'>Account {esc(context['account_id'])}; inclusive period {esc(context['start'])} to {esc(context['end'])}. {len(normalized['rows'])} transactions. File SHA-256: {esc(normalized['digest'])}.</p><p>Ignored columns (not imported): {esc(', '.join(normalized['ignored_columns']) or 'none')}. Generated row IDs identify positions inside this file only; overlapping files can repeat activity and require source review.</p><div class='card'><h2>Exact parsing rules</h2><pre class='dump'>{esc(json.dumps(context['mapping'],indent=2))}</pre><p>Optional new profile: {esc(stage['profile_name'] or 'none')}. Source check: {esc(stage['note'])}.</p></div>"
            if not normalized['rows']:body+="<p><b>Declared zero-activity period.</b> No transactions imported. This does not prove no bank activity; compare source boundaries and ledger observations.</p>"
            for r in normalized['rows']:body+=f"<div class='card'><h2>{esc(r['id'])}</h2><p>{web.money(r['signed_minor'],r['currency'])} on {esc(r['date'])}; {esc(r['reference'] or 'no reference')}.</p></div>"
            body+=f"<form method='post' action='/statement-mapping/confirm'>{web.csrf_field()}<input type='hidden' name='token' value='{token}'><label><input type='checkbox' name='confirmed' value='yes' required> I checked account, period, parsing, ignored columns and all rows. Import evidence only; create no cash.</label><button>Confirm mapped statement</button></form>"
            return web.page('Mapped statement preview',body,active='/statements')
        return handler._send_html(200,web.read_state(build))

    @web.route('POST','/statement-mapping/confirm')
    def confirm(handler,req):
        fields(req,{'token','confirmed'})
        if req.field('confirmed')!='yes':raise CashflowError('Confirm the reviewed statement explicitly.')
        stage=web.staged().get(req.field('token'))
        if not stage or stage.get('kind')!='statement_mapping':raise CashflowError('Preview expired; upload/review again. Saved source identities remain deduplicated.')
        result=web.mutate_state(lambda ledger:import_mapped_statement(ledger,stage['normalized'],stage['note'],stage['profile_name'],stage['revision'],ctx=web._owner_ctx(ledger)))
        return handler._redirect('/statements?batch_id='+quote(result['batch_id'],safe=''))
