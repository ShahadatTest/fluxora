"""Server-authored Telegram cards. Dynamic values are always HTML-escaped.

No model prose, financial arithmetic, arbitrary links or truncation of approvals.
Only the supported Telegram <b>/<code> tags are emitted.
"""
from html import escape

from .money import format_amount


class Card(str):
    """HTML produced by these templates, not an untrusted model string."""


def text(value):return escape(str(value),quote=False)
def bold(value):return '<b>'+text(value)+'</b>'
def code(value):return '<code>'+text(value)+'</code>'


def card(title,*blocks):
    return Card(bold('Fluxora · '+title)+'\nSynthetic workspace\n\n'+'\n\n'.join(str(b) for b in blocks if b))


def plain(value):
    """Preserve existing plain queued messages; never interpret their markup."""
    first,sep,rest=value.partition('\n')
    return Card(bold(first)+(sep+text(rest) if sep else ''))


def fields(values,depth=0):
    """Show every exact parameter, including unfamiliar/nested operations."""
    lines=[];prefix='  '*depth
    for key,value in values.items():
        label=key.replace('_',' ').capitalize()
        if key.endswith('_minor'):label+=' (integer minor units)'
        if isinstance(value,dict):lines.extend([prefix+text(label)+':',fields(value,depth+1)])
        elif isinstance(value,list):
            lines.append(prefix+text(label)+':'+(' [empty]' if not value else ''))
            for index,item in enumerate(value,1):
                lines.append(prefix+str(index)+'. '+(fields(item,depth+1) if isinstance(item,dict) else code(_value(item))))
        else:lines.append(prefix+text(label)+': '+code(_value(value)))
    return '\n'.join(lines) if lines else prefix+'No additional parameters.'


def _value(value):
    if value is None:return 'null'
    if isinstance(value,bool):return str(value).lower()
    return str(value)


def source_refs(basis):
    refs=sorted({ref for row in basis.get('inputs',[])+basis.get('drivers',[]) for ref in row.get('evidence_ids',[]) if ref})
    shown=', '.join(code(r) for r in refs[:4]) or 'No evidence IDs recorded'
    if len(refs)>4:shown+='; '+str(len(refs)-4)+' more in Web'
    if basis.get('truncated'):shown+='; tool source list is partial'
    return 'Source references: '+shown


def forecast(result):
    blocks=['As of '+code(result['as_of'])+' · '+text(result['horizon_days'])+' days']
    if result.get('scenario'):blocks.append('What-if only: '+text(result.get('scenario_description','Reviewed scenario')))
    for ccy,row in result.get('currencies',{}).items():
        breach=row['first_breach_date']
        lines=[bold(ccy+' · '+('Cash-floor risk' if breach else 'No floor breach projected')),
            'Available opening: '+text(row['opening']),
            'Cash floor: '+text(row['floor']),
            'Lowest projected balance: '+text(row['min_balance'])+' on '+code(row['min_balance_date']),
            'Projected closing: '+text(row['closing'])]
        if breach:lines+=['First floor breach: '+code(breach)+' · gap '+text(row['first_breach_shortfall']),
            'Worst gap to floor: '+text(row['worst_shortfall'])+' on '+code(row['min_balance_date'])]
        lines.append(source_refs(row.get('source_basis',{})));blocks.append('\n'.join(lines))
    if result.get('clarifications'):blocks.append('Inputs need review: '+text(len(result['clarifications']))+' clarification(s). Check the full forecast in Web.')
    blocks+=['Deterministic report. Expected collections are conditional; currencies remain separate. Outflows are counted before same-day inflows.',
        'Next: /plans to compare options. Open Forecast and Evidence & Ledger in your linked browser for full assumptions and source details.']
    return card('Cash-flow forecast',*blocks)


def plans(result):
    blocks=[bold('No feasible plan within the allowed strategies.') if result.get('no_feasible_plan') else
        'Conditional options meet the financial test. They still need the recorded approvals and collections.']
    for plan in result.get('plans',[]):
        rows=[bold(plan['name']), 'Plan ID: '+code(plan['id']),
            'Financial test: '+('sufficient under assumptions' if plan['financially_sufficient'] else 'insufficient under assumptions')]
        for ccy,values in plan.get('currencies',{}).items():
            rows.append(text(ccy)+': minimum '+text(format_amount(values['minimum_minor'],ccy))+
                ' · worst gap '+text(format_amount(values['shortfall_minor'],ccy)))
        notes=plan.get('conditional_labels',[])+plan.get('reasons',[])
        if notes:
            rows+=['• '+text(n) for n in notes[:2]]
            if len(notes)>2:rows.append(str(len(notes)-2)+' additional condition(s) in Plan Comparison.')
        refs=plan.get('evidence_ids',[])
        if refs:rows.append('Source references: '+', '.join(code(v) for v in refs[:3])+('; more in Web' if len(refs)>3 else ''))
        blocks.append('\n'.join(rows))
    blocks+=['These are proposed options. Owner approval, counterparty acceptance, observed payment and reconciliation remain separate. Protected payroll cannot be deferred. Currencies remain separate; display FX creates no cash.',
        'Next: open Plan Comparison in your linked browser to inspect exact draft terms and all conditions. No action was created by this report.']
    return card('Plan comparison',*blocks)


def review(plan,title,as_of):
    action=plan['task']=='action_proposal';pending=plan['task']=='proposal'
    effect=('Create one DRAFT for owner review.' if action else 'Prepare a pending workflow; separate owner approval follows.' if pending else 'Run the exact read-only report below.')
    return card('Review request',bold(title),'As of '+code(as_of),fields(plan['parameters']),
        'Button effect: '+effect,
        'Nothing has been prepared by this card. It expires in 15 minutes and becomes invalid if recorded data changes.',
        'No owner approval, counterparty consent, receipt, outgoing business message or payment is created. Choose Reject to discard this request.')


def snapshot_review(ledger,job):
    preview=job['preview']
    blocks=['Workflow: '+code(job['id']),fields({'steps':job['steps']}),
        'As of '+code(ledger.as_of)+'\nSource revision: '+code(job['basis'])]
    for ccy,before in preview['cash_before_minor'].items():
        after=preview['cash_after_minor'][ccy];f=preview['forecast_after'][ccy]
        blocks.append(bold(ccy)+'\nRecorded cash: '+text(format_amount(before,ccy))+' → '+text(format_amount(after,ccy))+
            '\nSnapshot minimum: '+text(format_amount(f['minimum_minor'],ccy))+' on '+code(f['minimum_date'])+
            '\nSnapshot closing: '+text(format_amount(f['closing_minor'],ccy))+
            '\nCash floor: '+text(format_amount(f['floor_minor'],ccy))+
            '\nFirst floor breach: '+code(f['first_breach_date'] or 'none under assumptions'))
    return card('Owner approval required',*blocks,
        'Approve & save snapshot records only the exact forecast snapshot(s) listed above. Current recorded cash is unchanged. Expected collections remain conditional.',
        'No payment, receipt or counterparty consent is recorded. Review expires in 15 minutes. All amounts are displayed from integer minor units, separately by currency.')


def completion(plan,outcome):
    if outcome.status!='completed':
        return card('Task needs attention','Status: '+text(outcome.status),'Check the request in Web history before making a fresh request. This message does not confirm a completed operation.')
    calls=getattr(outcome.result,'tool_calls',[]) if outcome.result else []
    good=[c for c in calls if c.get('ok')]
    for call in good:
        result=call['result']
        if call['name']=='compute_forecast':return forecast(result)
        if call['name']=='compare_plans':return plans(result)
        if call['name']=='draft_action':
            return card('DRAFT ready', 'Action: '+code(result['action_id']),fields(plan['parameters']),
                'Status: DRAFT · awaiting owner review.',
                'Next: open Action History in your linked browser to inspect and decide. No approval, counterparty consent, receipt, business message or payment was created.')
        if call['name']=='prepare_workflow':
            return card('Workflow prepared','Workflow: '+code(result['id']),'Status: '+text(result['status']),
                'The exact steps await separate owner approval. No financial effect has been applied. Review the next approval card, or open the workflow in Web.')
    return card('Report ready','The reviewed deterministic task completed. Its full structured results are available in Web request history. No model-written financial summary is shown here.')


def help_card():
    return card('Your cash-flow companion',
        bold('Read your position')+'\n/forecast — cash-floor risks and dates\n/plans — compare conditional options\n/status — connection and workspace status',
        bold('Prepare for review')+'\n'+code('/draft INVOICE_ID USD 100 YYYY-MM-DD')+'\nPrepare an early-payment DRAFT with exact terms.\n'+code('/snapshot LABEL')+'\nPrepare a forecast snapshot; separate approval follows.',
        bold('Settings')+'\n/link CODE — link your own browser workspace\n/alerts on · /alerts off — opt-in updates\n/unlink — disconnect and revoke pending buttons',
        bold('Optional private model')+'\n/verify · /qualify — confirmed checks that may incur charges.\nOther text uses one model translation only when configured and qualified. Your question and static task catalog go to that provider; ledger/attachments are not sent. You review its exact interpretation.',
        'Use synthetic data only. Reports and DRAFTs do not approve work, prove payment or send business messages.')


def model_state(state):
    return {'configured-but-unverified':'Configured; connection check needed',
        'verified':'Connection verified (not a guarantee of answer accuracy)',
        'disconnected':'Not connected','not configured':'Not configured',
        'error':'Unavailable; direct commands still work','mock':'Deterministic mock; no live model'}.get(state,'Check required')


NOTICES={
    'not_linked':('Link your workspace first','Open Telegram settings in your own browser workspace, create a one-time code, then send /link CODE here. Never use a code from another person.'),
    'workspace_expired':('Workspace expired','The 24-hour browser workspace has expired and was disconnected. Open the website and link a fresh synthetic workspace; Telegram does not extend its lifetime.'),
    'pair_expired':('Pairing code unavailable','The code expired, was already used or is invalid. Create a fresh code in your own Telegram settings page. Your current link was not changed.'),
    'expired_review':('Review expired','Prepare the request again and inspect the fresh terms. This expired button applied no new operation.'),
    'changed_review':('Recorded data changed','The old card no longer matches the workspace. Prepare and review a fresh request; this button applied no operation.'),
    'old_link':('Review unavailable','This button belongs to an old link or is no longer available. Use /status, then prepare a fresh request in the linked workspace.'),
    'unknown_command':('Command not recognised','Use /help for supported commands. Nothing was applied.'),
    'draft_format':('Add the exact draft terms','Use /draft INVOICE_ID USD 100 YYYY-MM-DD. Include an existing record ID, currency, positive amount and ISO date. No DRAFT was created.'),
    'model_missing':('Private model is not configured','Direct /forecast, /plans, /draft and /snapshot commands still work without a model.'),
    'qualification':('Private model needs a check','Use /verify, then /qualify and review the cost confirmation. Direct commands still work; no model translation was run.'),
    'oversize':('Review needs the Web workspace','The complete terms do not fit a Telegram card. Open the detailed Web review; no terms were shortened for approval.'),
}


def notice(reason):
    title,detail=NOTICES[reason]
    return card(title,text(detail))
