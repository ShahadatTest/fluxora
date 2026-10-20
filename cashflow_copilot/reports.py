"""Read-only deterministic snapshots, CSV and dependency-free XLSX serialization.

The workbook is a static export, not a second financial calculation engine.
Money remains integer minor units until exact decimal display serialization.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
import csv
from html import escape
from io import BytesIO, StringIO
import re
import textwrap
import math
from zipfile import ZipFile, ZIP_DEFLATED

from .authz import OP_READ, require_access
from .errors import CashflowError
from .forecast import forecast
from .money import minor_digits
from .settlement import cash_events, collection_held, bill_schedule, split_unallocated


@dataclass(frozen=True)
class Money:
    minor: int
    currency: str

    def decimal(self):
        digits=minor_digits(self.currency)
        raw=str(abs(self.minor)).zfill(digits+1)
        return Decimal(('-' if self.minor<0 else '')+(raw[:-digits]+'.'+raw[-digits:] if digits else raw))


def report_tables(ledger,*,ctx):
    require_access(ctx,ledger,OP_READ)
    if not 1<=ledger.horizon_days<=120 or len(ledger.outflows)+len(ledger.receivables)>600:
        raise CashflowError('Financial exports support a 1â€“120-day horizon and at most 600 invoices/bills.')
    currencies=list(dict.fromkeys(a.currency for a in ledger.accounts.values()))
    if any(key != a.id for key,a in ledger.accounts.items()):
        raise CashflowError('Account register keys must match their stable account IDs.')
    if not currencies:
        raise CashflowError('Reports need opening cash accounts.')
    if any(i.currency not in currencies for group in [ledger.receivables,ledger.outflows] for i in group.values()):
        raise CashflowError('Every invoice/bill currency needs a separate cash account before financial export.')
    result=forecast(ledger,ledger.as_of,ledger.horizon_days)
    events=list(cash_events(ledger))
    def m(value,ccy):return Money(value,ccy)
    tables=[]
    def add(name,headers,rows,note):
        if len(rows)>20000:raise CashflowError('A report table exceeds 20,000 rows. Use a smaller reviewed dataset; no history was silently dropped.')
        if any(len(str(v))>32767 for row in rows for v in row if isinstance(v,str)):
            raise CashflowError('A report source text exceeds the Excel cell limit; review that source before export.')
        tables.append(dict(name=name,headers=headers,rows=rows,note=note))
    summary=[]
    from .treasury import currency_opening, currency_floor, restricted_cash, treasury_report, party_summary
    for ccy in currencies:
        accounts = [a for a in ledger.accounts.values() if a.currency == ccy]
        from .models import Account
        account = Account('pooled-'+ccy, ccy, currency_opening(ledger,ccy), currency_floor(ledger,ccy), ', '.join(a.evidence_id or 'unknown' for a in accounts))
        ccy=account.currency;curve=result.currencies[ccy]
        summary.append([ccy,m(account.opening_minor,ccy),m(ledger.actual_cash(ccy),ccy),m(account.floor_minor,ccy),
            m(sum(r.remaining_minor for r in ledger.receivables.values() if r.currency==ccy),ccy),
            m(sum(r.remaining_minor for r in ledger.receivables.values() if r.currency==ccy and collection_held(r)),ccy),
            m(sum(o.remaining_minor for o in ledger.outflows.values() if o.currency==ccy),ccy),
            m(curve.min_balance_minor,ccy),curve.first_breach_date or 'none in window',account.evidence_id or 'unknown',
            m(sum(r.credit_balance_minor for r in ledger.receivables.values() if r.currency==ccy),ccy),
            m(sum(o.overpaid_minor for o in ledger.outflows.values() if o.currency==ccy),ccy),
            m(sum(split_unallocated(ledger,p) for p in ledger.split_receipts.values() if p['currency']==ccy),ccy)])
    add('Summary',['Currency','Opening snapshot','Recorded cash at snapshot','Cash floor','Outstanding receivables','Held receivables','Unpaid bills','Forecast minimum','First forecast breach','Opening evidence','Customer credit requiring review','Supplier overpayment requiring review','Unallocated customer receipts'],summary,
        'SYNTHETIC deterministic snapshot. Amounts are major units in each row currency. Recorded cash is opening plus dated records through the snapshot, not a bank-verified balance.')
    daily=[]
    for ccy,curve in result.currencies.items():
        for day in curve.days:
            recorded=[e for e in events if e['currency']==ccy and e['date']==day.date]
            recorded_in=sum(e['amount_minor'] for e in recorded if e['amount_minor']>0)
            recorded_out=-sum(e['amount_minor'] for e in recorded if e['amount_minor']<0)
            refs={e['evidence_id'] for e in recorded}
            refs.update(s for e in recorded for s in e.get('date_evidence_ids',[]))
            refs.update(r.evidence_id for r in ledger.receivables.values() if r.currency==ccy and not collection_held(r) and r.remaining_minor>0 and r.expected_date==day.date)
            refs.update(ledger.collection_date_source(r.id) for r in ledger.receivables.values() if r.currency==ccy and not collection_held(r) and r.remaining_minor>0 and r.expected_date==day.date)
            refs.update(o.evidence_id for o in ledger.outflows.values() if o.currency==ccy and any(max(d,ledger.as_of)==day.date for d,amt in bill_schedule(o) if amt>0))
            daily.append([ccy,day.date,m(day.opening_minor,ccy),m(recorded_in,ccy),m(day.inflow_minor-recorded_in,ccy),
                m(recorded_out,ccy),m(day.outflow_minor-recorded_out,ccy),m(day.intraday_low_minor,ccy),m(day.closing_minor,ccy),day.floor_breached,', '.join(sorted(refs))])
    add('Forecast',['Currency','Date','Forecast opening','Recorded cash increases','Expected collections','Recorded cash decreases','Scheduled unpaid outflow','Forecast intraday low','Forecast closing','Floor breach','Evidence IDs'],daily,
        'FORECAST under recorded terms. Holds exclude remaining expected inflow. Signed accounting corrections are recorded cash adjustments, not executed refunds/transfers. Same-day outflows precede inflows.')
    invoices=[]
    for r in ledger.receivables.values():
        days=(ledger.as_of-r.due_date).days if r.due_date else None
        bucket='unknown' if days is None else 'not overdue' if days<=0 else '1-30' if days<=30 else '31-60' if days<=60 else '61-90' if days<=90 else 'over 90'
        invoices.append([r.id,r.currency,r.counterparty,r.invoice_reference,m(r.original_minor,r.currency),m(r.remaining_minor,r.currency),
            r.due_date,r.expected_date,bucket,'held' if collection_held(r) else 'eligible',r.evidence_id,
            ledger.invoice_due_sources.get(r.id,r.evidence_id),r.collection_hold['evidence_id'] if r.collection_hold else '',ledger.collection_date_source(r.id),
            m(r.effective_total_minor,r.currency),m(r.credit_balance_minor,r.currency),', '.join(c['evidence_id'] for c in r.face_adjustments),
            m(sum(x['adjustments'][-1]['new_amount_minor'] if x['adjustments'] else x['amount_minor'] for x in r.refunds),r.currency),m(sum(c['delta_minor'] for c in r.credit_entries),r.currency),', '.join(c['evidence_id'] for c in r.credit_entries)])
    add('Receivables',['Invoice ID','Currency','Customer','External reference','Original amount','Outstanding amount','Contractual due','Expected collection','Overdue days bucket','Collection status','Invoice evidence','Due evidence','Hold evidence','Collection date evidence','Effective invoice total','Customer credit requiring review','Adjustment sources','Effective observed customer refunds','Net noncash credit applied','Credit transfer sources'],invoices,
        'OUTSTANDING claims, not cash. Contractual due is separate from expected collection. Unknown due stays unknown. Hold retains receivable/receipts and excludes only remaining planned collections.')
    bills=[[o.id,o.currency,o.counterparty,o.invoice_reference,m(o.amount_minor,o.currency),m(o.paid_minor,o.currency),m(o.remaining_minor,o.currency),
        o.date,o.protected,o.status,o.evidence_id,m(o.effective_total_minor,o.currency),m(o.overpaid_minor,o.currency),', '.join(c['evidence_id'] for c in o.face_adjustments),
        m(sum(x['adjustments'][-1]['new_amount_minor'] if x['adjustments'] else x['amount_minor'] for x in o.refunds),o.currency),
        '; '.join(str(d)+': '+str(m)+' minor '+o.currency for d,m in bill_schedule(o)),
        ', '.join(a['evidence_id'] for a in o.agreement_schedules),m(sum(c['delta_minor'] for c in o.credit_entries),o.currency),', '.join(c['evidence_id'] for c in o.credit_entries)] for o in ledger.outflows.values()]
    add('Bills',['Bill ID','Currency','Supplier','External reference','Bill amount','Net settled including noncash credit after refunds','Remaining payable','Contractual date','Protected','Status','Evidence ID','Effective bill total','Supplier overpayment requiring review','Adjustment sources','Effective observed supplier refunds','Current unpaid schedule','Accepted agreement sources','Net noncash credit applied','Credit transfer sources'],bills,
        'SCHEDULED obligations and net settlement including noncash credit after recorded supplier refunds. Carried credits are not observed payments; loan instalments are protected bills, with no drawdown from registration. Corrections preserve original observations in Cash events. Payroll dates and consent requirements are not waived.')
    event_rows=[[e['id'],e['kind'],e['currency'],e['date'],m(e['amount_minor'],e['currency']),
        'absorbed in current opening' if e['date']<ledger.as_of else 'recorded through snapshot' if e['date']==ledger.as_of else 'future-dated recorded in sandbox',
        e.get('invoice_id') or e.get('outflow_id') or e.get('receipt_id',''),e['evidence_id'],e.get('classification',''),e.get('original_date',e['date']),', '.join(e.get('date_evidence_ids',[]))] for e in events]
    add('Cash events',['Event ID','Record kind','Currency','Effective date','Signed cash amount','Snapshot treatment','Target ID','Evidence ID','Other cash classification','Original source date','Date correction sources'],event_rows,
        'RECORDED HISTORY. Cash increases positive, decreases negative. Events before opening are already absorbed and must not be added again. Correction entries are accounting adjustments, not observed new transfers.')
    evidence=[[e.id,e.kind,e.summary] for e in ledger.evidence.values()]
    add('Evidence',['Evidence ID','Kind','Source summary'],evidence,
        'Synthetic evidence register. Source assertions are not independent verification. Raw uploaded document files, provider configuration and saved agent prompts/prose are not exported.')
    from .adaptive import input_problems
    if ledger.account_details or ledger.cash_restrictions or ledger.internal_transfers:
        treasury = treasury_report(ledger, ctx=ctx)
        add('Cash accounts', ['Account ID','Account label','Currency','Opening snapshot','Book cash','Restricted cash','Available cash','Floor buffer','Default observation account','Opening evidence'],
            [[r['account_id'],r['name'],r['currency'],m(r['opening_minor'],r['currency']),m(r['book_minor'],r['currency']),m(r['restricted_minor'],r['currency']),m(r['available_minor'],r['currency']),m(r['floor_minor'],r['currency']),r['default'],r['evidence_id']] for r in treasury['accounts']], treasury['note'])
        add('Cash restrictions', ['Restriction ID','Account ID','Currency','Restricted amount','Purpose','Recorded date','Restriction evidence','Released date','Release evidence'],
            [[r['id'],r['account_id'],ledger.accounts[r['account_id']].currency,m(r['amount_minor'],ledger.accounts[r['account_id']].currency),r['purpose'],r['date'],r['evidence_id'],r['release']['date'] if r['release'] else '',r['release']['evidence_id'] if r['release'] else ''] for r in treasury['restrictions']],
            'Source-backed restrictions and releases are not cash movements. Forecast balances exclude active restrictions once; no release is assumed.')
        add('Own transfers', ['Transfer ID','From account','To account','Currency','Amount','Observed date','Evidence','Reversal date','Reversal evidence'],
            [[r['id'],r['source_id'],r['target_id'],r['currency'],m(r['amount_minor'],r['currency']),r['date'],r['evidence_id'],r['reversal']['date'] if r['reversal'] else '',r['reversal']['evidence_id'] if r['reversal'] else ''] for r in treasury['transfers']],
            'Own transfers have two linked account legs and zero business-currency cash effect. They are excluded from business cash inflows/outflows. No payment is executed.')
    if ledger.party_register:
        parties = [party_summary(ledger, ident, ctx=ctx) for ident in ledger.party_register]
        add('Party identities', ['Party ID','Role','Current name','Aliases','Record ID','Source name','Currency','Remaining claim','Separate credit','Link evidence'],
            [[p['id'],p['role'],p['name'],', '.join(p['aliases']),r['id'],r['source_name'],r['currency'],m(r['remaining_minor'],r['currency']),m(r['credit_minor'],r['currency']),ledger.party_links[('invoice:' if p['role']=='customer' else 'bill:')+r['id']]['evidence_id']] for p in parties for r in p['records']],
            'Explicit owner-reviewed record links; roles/currencies stay separate. Original source labels remain. No independent legal identity verification or automatic credit transfer.')
    warnings=input_problems(ledger)
    if ledger.cash_restrictions: warnings.append('Forecast minimum/closing values are available decision cash after active source-backed restrictions; recorded cash remains book cash. See Cash accounts and Cash restrictions.')
    if any(collection_held(r) for r in ledger.receivables.values()):warnings.append('Held/disputed invoices remain outstanding and are excluded from planned collection.')
    if ledger.split_receipts:
        warnings.append('Split payments supply cash once at the parent observation. Invoice allocations/corrections supply no additional cash. Unallocated customer receipts are not revenue; balances include any explicitly labelled future-dated sandbox observations.')
    return dict(business_id=ledger.business_id,business_name=ledger.business_name,as_of=ledger.as_of,tables=tables,warnings=warnings,
        note='Static export from the deterministic engine. Editing a workbook does not recalculate the application. Amounts over Excel 15-digit precision remain exact text. No currency conversion or combined-currency total.')


def csv_text(value):
    if value is None:return ''
    if isinstance(value,Money):return ("'" if len(str(abs(value.minor)))>15 else '')+format(value.decimal(),'f')
    if type(value) is date:return value.isoformat()
    if type(value) is bool:return 'yes' if value else 'no'
    if type(value) is int:return str(value)
    text=str(value)
    # Quotes alone do not stop CSV formula injection. Prefix hostile text,
    # while genuine numeric negatives from typed Money stay numeric.
    candidate=re.sub(r'^[\x00-\x20\s\uFEFF]+','',text)
    if text and (ord(text[0])<32 or candidate.startswith(('=','+','-','@'))):return "'"+text
    return text


def csv_report(report,name):
    table=next((t for t in report['tables'] if t['name']==name),None)
    if table is None:raise CashflowError('Choose a named report table.')
    output=StringIO(newline='');writer=csv.writer(output)
    writer.writerow(['Business ID','Snapshot date','Record basis']+table['headers'])
    for row in table['rows']:
        writer.writerow([csv_text(report['business_id']),report['as_of'].isoformat(),'synthetic static '+name]+[csv_text(v) for v in row])
    return ('\ufeff'+output.getvalue()).encode('utf-8')


def csv_archive(report):
    output=BytesIO()
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        for table in report['tables']:archive.writestr(table['name'].lower().replace(' ','-')+'.csv',csv_report(report,table['name']))
        archive.writestr('README.txt',report['note']+'\n'+'\n'.join(t['name']+': '+t['note'] for t in report['tables'])+'\nWarnings:\n'+'\n'.join(report['warnings']))
    return output.getvalue()


def _xml(text):
    return escape(re.sub(r'[^\x09\x0A\x0D\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]','',str(text)),quote=True)


def _col(index):
    label=''
    while index:
        index,rem=divmod(index-1,26);label=chr(65+rem)+label
    return label


def _cell(ref,value,style=None):
    if value is None:return ''
    if isinstance(value,Money):
        decimal=value.decimal()
        if len(str(abs(value.minor)))<=15:return f'<c r="{ref}" s="{3 if minor_digits(value.currency)==0 else 4}"><v>{format(decimal,"f")}</v></c>'
        value=format(decimal,'f')
    if type(value) is date:
        if value>=date(1900,1,1):
            serial=(value-date(1899,12,30)).days
            if 1<=serial<=60:serial-=1
            return f'<c r="{ref}" s="5"><v>{serial}</v></c>'
        value=value.isoformat()
    if type(value) is bool:return f'<c r="{ref}" t="b"><v>{int(value)}</v></c>'
    if type(value) is int and len(str(abs(value)))<=15:return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr" s="{style or 0}"><is><t xml:space="preserve">{_xml(value)}</t></is></c>'


def xlsx_report(report):
    """Minimal OOXML with typed dates/money and literal strings. No formulas/macros/links."""
    output=BytesIO();ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    styles=f'''<styleSheet xmlns="{ns}"><numFmts count="3"><numFmt numFmtId="164" formatCode="#,##0;(#,##0);&quot;-&quot;"/><numFmt numFmtId="165" formatCode="#,##0.00;(#,##0.00);&quot;-&quot;"/><numFmt numFmtId="166" formatCode="yyyy-mm-dd"/></numFmts><fonts count="4"><font><sz val="10"/><name val="Arial"/><color rgb="FF202A35"/></font><font><sz val="10"/><name val="Arial"/><b/><color rgb="FFFFFFFF"/></font><font><sz val="14"/><name val="Arial"/><b/><color rgb="FF22384D"/></font><font><sz val="10"/><name val="Arial"/><i/><color rgb="FF5D6570"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF22384D"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="8"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"><alignment vertical="center"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="0" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="2" fillId="0" borderId="0"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="166" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="3" fillId="0" borderId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''
    with ZipFile(output,'w',ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(report['tables'])+1))+'</Types>')
        archive.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{t["name"]}" sheetId="{i}" r:id="rId{i}"/>' for i,t in enumerate(report['tables'],1))+'</sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(report['tables'])+1))+f'<Relationship Id="rId{len(report["tables"])+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml',styles)
        for i,table in enumerate(report['tables'],1):
            headers=table['headers'];last=_col(len(headers));last_row=len(table['rows'])+5
            context=f'{report["business_name"]} ({report["business_id"]}) | Snapshot {report["as_of"].isoformat()} | SYNTHETIC static export'
            rows=[f'<row r="2" ht="24" customHeight="1">{_cell("A2",table["name"],2)}</row>',
                f'<row r="3" ht="18" customHeight="1">{_cell("A3",context,6)}</row>',
                '<row r="5" ht="36" customHeight="1">'+''.join(_cell(f'{_col(c)}5',h,1) for c,h in enumerate(headers,1))+'</row>']
            widths=[min(64,max(18,len(h)+2,max((len(csv_text(row[c]))+3 for row in table['rows']),default=0))) for c,h in enumerate(headers)]
            for rownum,row in enumerate(table['rows'],6):
                height=max([20]+[min(400,16*sum(max(1,math.ceil(len(line)/max(1,widths[c]-3))) for line in v.splitlines())) for c,v in enumerate(row) if isinstance(v,str)])
                rows.append(f'<row r="{rownum}" ht="{height}" customHeight="1">'+''.join(_cell(f'{_col(c)}{rownum}',v,7 if isinstance(v,str) and len(v)>widths[c-1]-3 else None) for c,v in enumerate(row,1))+'</row>')
            paragraphs=[table['note'],report['note']]+(report['warnings'] if i==1 else [])
            notes=[line for paragraph in paragraphs for line in textwrap.wrap(paragraph,width=95)]
            for offset,note in enumerate(notes,last_row+3):
                rows.append(f'<row r="{offset}" ht="20" customHeight="1">{_cell(f"A{offset}",note,6)}</row>')
            cols=''.join(f'<col min="{c}" max="{c}" width="{widths[c-1]}" customWidth="1"/>' for c,h in enumerate(headers,1))
            sheet=f'<worksheet xmlns="{ns}"><dimension ref="A1:{last}{last_row+3+len(notes)}"/><sheetViews><sheetView workbookViewId="0" showGridLines="0"><pane xSplit="1" ySplit="5" topLeftCell="B6" activePane="bottomRight" state="frozen"/></sheetView></sheetViews><sheetFormatPr defaultRowHeight="20"/><cols>{cols}</cols><sheetData>'+''.join(rows)+f'</sheetData><autoFilter ref="A5:{last}{max(5,last_row)}"/><pageMargins left="0.3" right="0.3" top="0.5" bottom="0.5" header="0.2" footer="0.2"/><pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/></worksheet>'
            archive.writestr(f'xl/worksheets/sheet{i}.xml',sheet)
    return output.getvalue()
