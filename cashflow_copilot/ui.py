"""Fluxora presentation components. No financial calculation or authority here."""
from __future__ import annotations

from html import escape
from threading import local

REQUEST = local()

# One canonical, discoverable home for every existing workspace page.
GROUPS = (
    ('Workspace', (
        ('/', 'Overview', 'grid', ()),
        ('/today', 'Today', 'sun', (('/review', 'Daily review'), ('/decision-brief', 'Decision brief'))),
        ('/agent/tasks', 'Agent', 'spark', (('/autopilot', 'Autopilot'),('/agent', 'Advanced agent & history'))),
    )),
    ('Cash planning', (
        ('/forecast', 'Forecast', 'chart', (('/weekly-cashflow', 'Thirteen-week view'), ('/account-forecast', 'Account funding'), ('/variance', 'Forecast vs actual'))),
        ('/plans', 'Plans & scenarios', 'layers', (('/scenarios', 'Saved scenarios'), ('/stress', 'Stress test'), ('/plans/examples', 'Example plans'))),
        ('/affordability', 'Affordability', 'check', ()),
        ('/calendar', 'Calendar', 'calendar', ()),
    )),
    ('Records', (
        ('/ledger', 'Evidence & ledger', 'file', (('/facts', 'Resolve facts'), ('/attachments', 'Attachments'), ('/freshness', 'Data freshness'), ('/duplicates', 'Duplicate review'), ('/entries', 'Owner entries'), ('/records', 'Record register'), ('/search', 'Search records'))),
        ('/collections', 'Receivables', 'in', (('/promises', 'Payment promises'), ('/holds', 'Collection holds'), ('/parties', 'Party statements'), ('/party-register', 'Party register'), ('/split-receipts', 'Split receipts'), ('/unallocated-refunds', 'Unallocated refunds'), ('/invoice-adjustments', 'Invoice adjustments'), ('/refunds', 'Refunds'))),
        ('/payments', 'Bills & payroll', 'out', (('/recurring', 'Recurring bills'), ('/payment-warnings', 'Payment warnings'), ('/payment-corrections', 'Payment corrections'), ('/instalments', 'Instalments'), ('/bill-adjustments', 'Bill adjustments'), ('/payment-dates', 'Payment dates'))),
        ('/cash-accounts', 'Accounts & loans', 'wallet', (('/cash-availability', 'Cash availability'), ('/daily', 'Daily cash update'), ('/loans', 'Loan positions'), ('/credits', 'Credits & adjustments'), ('/cash-movements', 'Cash movements'))),
        ('/statements', 'Reconciliation', 'link', (('/statement-mapping', 'Statement mapping'), ('/statement-balances', 'Statement balances'), ('/batch-matching', 'Batch matching'), ('/receipts', 'Receipt review'), ('/receipts/inbox', 'Receipt inbox'), ('/corrections', 'Receipt corrections'), ('/split-payment-corrections', 'Split payment corrections'))),
    )),
    ('Review', (
        ('/agent/workflows', 'Approval queue', 'check', ()),
        ('/actions', 'Action history', 'clock', ()),
        ('/reports', 'Reports', 'file', ()),
    )),
    ('Settings & help', (
        ('/setup', 'Business setup', 'settings', ()),
        ('/backups', 'Backup & restore', 'save', ()),
        ('/telegram', 'Telegram companion', 'link', (('/telegram/demo', 'Interactive demo — no account needed'),)),
        ('/docs', 'Documentation', 'book', (('/demo', 'Demo workflow'), ('/usability', 'Walkthrough session'))),
    )),
)

_PATHS = {
    'grid': '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    'chart': '<path d="M4 4v16h17M7 15l4-5 4 3 5-7"/>',
    'sun': '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
    'spark': '<path d="m12 3 2.8 6.2L21 12l-6.2 2.8L12 21l-2.8-6.2L3 12l6.2-2.8Z"/>',
    'layers': '<path d="m12 3 10 5-10 5L2 8Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
    'check': '<rect x="3" y="3" width="18" height="18" rx="5"/><path d="m7 12 3 3 7-7"/>',
    'calendar': '<rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4m10-4v4M3 11h18"/>',
    'file': '<path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9Zm0 0v6h6M8 13h8m-8 4h5"/>',
    'in': '<path d="M4 16v4h16v-4M12 3v12m-5-5 5 5 5-5"/>',
    'out': '<path d="M4 16v4h16v-4M12 15V3M7 8l5-5 5 5"/>',
    'wallet': '<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 8V6l14-3v2m4 7h-6v5h6"/>',
    'link': '<path d="m10 14 4-4m-6 6-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 2 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/>',
    'clock': '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    'settings': '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    'save': '<path d="M4 3h13l4 4v14H3V3Zm3 0v7h10V3M7 21v-7h10v7"/>',
    'book': '<path d="M12 6C8 3 4 3 2 4v16c3-1 6-1 10 1 4-2 7-2 10-1V4c-2-1-6-1-10 2Zm0 0v15"/>',
    'arrow': '<path d="M4 12h16m-6-6 6 6-6 6"/>',
    'search': '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
}


def icon(name):
    return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'+_PATHS.get(name, _PATHS['file'])+'</svg>'


def wordmark():
    return '<span class="brand-mark generated-mark" aria-hidden="true"><img src="/assets/fluxora-mark-v1.png" width="1254" height="1254" alt="" decoding="async"></span><span>Fluxora<span class="brand-dot">.</span></span>'


def nav_match(path):
    if path.startswith('/settings/connections') and (getattr(REQUEST,'local_connections',None) is not None or getattr(REQUEST,'hosted',False)):
        return ('Settings & help','/settings/connections','API & Telegram','settings',(),'/settings/connections')
    # Query is never part of path. Prefer the deepest existing navigation root.
    found = None
    for group, rows in GROUPS:
        for url, label, glyph, children in rows:
            for target, childlabel in ((url, label),)+children:
                if path == target or (target != '/' and path.startswith(target+'/')):
                    if found is None or len(target)>len(found[-1]):
                        found=(group, url, label, glyph, children, target)
    return found


def nav_html(path):
    current=nav_match(path)
    chunks=[]
    for group,rows in GROUPS:
        if group=='Settings & help' and (getattr(REQUEST,'local_connections',None) is not None or getattr(REQUEST,'hosted',False)):
            rows=(('/settings/connections','API & Telegram','settings',()),)+rows
        links=[]
        for url,label,glyph,children in rows:
            selected=current is not None and current[1]==url
            links.append(f'<a href="{url}" class="side-link{" selected" if selected else ""}"'+(' aria-current="page"' if path==url else '')+f'>{icon(glyph)}<span>{label}</span></a>')
        if group=='Settings & help':
            opened=' open' if current and current[0]==group else ''
            chunks.append(f'<details class="nav-settings"{opened}><summary>{icon("settings")}<span>Settings & help</span></summary><div>{"".join(links)}</div></details>')
        else:chunks.append(f'<div class="nav-group"><div class="nav-label">{group}</div>{"".join(links)}</div>')
    return ''.join(chunks)


HELP = {'/agent':'guided-agent','/agent/tasks':'guided-agent','/agent/workflows':'approvals','/actions':'approvals',
        '/plans':'scenarios','/scenarios':'scenarios','/forecast':'forecasts','/ledger':'evidence',
        '/backups':'recovery','/setup':'local-setup','/statements':'reconciliation','/telegram':'telegram'}


def document(title, content, *, kind='app', extra=''):
    from .floating_assistant import render
    assistant=render(getattr(REQUEST,'path','/'),bool(getattr(REQUEST,'public_demo',True)))
    assets='<link rel="stylesheet" href="/assets/fluxora-assistant.css"><script src="/assets/fluxora-assistant.js" defer></script>' if assistant else ''
    if kind=='site':assets+='<link rel="stylesheet" href="/assets/fluxora-motion.css"><script src="/assets/fluxora-motion.js" defer></script>'
    if not getattr(REQUEST,'path','/').startswith('/settings/connections'):
        assets+='<link rel="stylesheet" href="/assets/fluxora-guide.css">'
        if kind=='app':assets+='<script src="/assets/fluxora-guide.js" defer></script>'
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#133c36"><title>'+escape(title)+' — Fluxora</title>'+extra+'<link rel="icon" href="/assets/fluxora-mark-v1.png" type="image/png"><link rel="apple-touch-icon" href="/assets/fluxora-mark-v1.png"><link rel="stylesheet" href="/assets/fluxora.css"><link rel="stylesheet" href="/assets/fluxora-polish.css">'+('<link rel="stylesheet" href="/assets/fluxora-workspace.css">' if kind=='app' else '')+assets+'<script src="/assets/fluxora.js" defer></script></head><body class="'+kind+'"><a class="skip-link" href="#main">Skip to main content</a>'+content+assistant+'</body></html>'


def app_page(title, body, active, status, legacy_css=''):
    path=getattr(REQUEST,'path','') or active or '/'
    hosted=getattr(REQUEST,'hosted',False)
    public_demo=hosted and getattr(REQUEST,'public_demo',False)
    workspace_context='Your visitor session · synthetic data' if public_demo else ('Protected · synthetic data' if hosted else 'Local · synthetic data')
    sandbox_context='Visitor demo' if public_demo else ('Hosted owner demo' if hosted else 'Local sandbox')
    sandbox_notice='Your synthetic workspace is separate from other visitor sessions. Expires after 24 hours; clearing browser cookies loses access. Use synthetic data only.' if public_demo else 'Synthetic records only. No real money or bank connection. Counterparty messages are simulated; private Telegram delivery is separate.'
    if path=='/app':path='/'
    match=nav_match(path) or nav_match(active)
    group=match[0] if match else 'Workspace'
    label=match[2] if match else title
    children=match[4] if match else ()
    url=match[1] if match else '/'
    page_label=next((t for p,t in children if match and p==match[-1]),label)
    localnav=''
    if children:
        localnav='<nav class="local-tabs" aria-label="'+escape(label)+' pages">'+''.join(
            '<a href="'+p+'"'+(' aria-current="page"' if p==path else '')+'>'+t+'</a>' for p,t in ((url,label),)+children)+'</nav>'
        if len(children)>5:
            localnav='<details class="section-navigation"><summary>'+escape(page_label)+'<span>'+str(1+len(children))+' related views</span></summary>'+localnav+'</details>'
    help_slug=HELP.get(url,'getting-started')
    status_class='connected' if 'provider: verified' in status else 'offline'
    from .workspace_ui import render
    body=render(title,body,path,active,group)
    from .getting_started import panel
    guidance=panel() if not path.startswith('/settings/connections') else ''
    shell=f'''<div class="app-frame">
<details class="mobile-menu"><summary aria-label="Open navigation">☰ <span>Menu</span></summary><nav aria-label="Mobile navigation">{nav_html(path)}<a class="side-link" href="/workspace/features">All workspace features</a><a class="side-link" href="/welcome">Product website</a></nav></details>
<aside class="sidebar"><a class="brand" href="/welcome" aria-label="Fluxora product home">{wordmark()}</a><div class="workspace-label"><span class="workspace-avatar">F</span><span>Demo workspace<small>{workspace_context}</small></span></div><nav aria-label="Main navigation">{nav_html(path)}</nav><div class="sidebar-utilities"><a class="side-link" href="/start">{icon('book')} Get started</a><a class="side-link" href="/workspace/features">{icon('grid')} All workspace features</a><a class="sidebar-bottom" href="/welcome">Product website {icon('arrow')}</a></div></aside>
<div class="app-main"><header class="workspace-top"><a class="brand mobile-brand" href="/welcome" aria-label="Fluxora product home">{wordmark()}</a><nav class="breadcrumb" aria-label="Breadcrumb"><a href="/workspace/features">{escape(group)}</a><span aria-hidden="true">/</span><b aria-current="page">{escape(page_label)}</b></nav><div class="top-tools"><a href="/search" class="workspace-search" aria-label="Search records">{icon('search')}<span>Search records</span></a><a class="help-link" href="/docs/{help_slug}">{icon('book')}<span>Help</span></a><span class="demo-tag">SYNTHETIC DEMO</span><a class="today-link" href="/today">{icon('sun')}<span>Today</span></a></div></header>
<div class="workspace-notice"><span class="sandbox-label"><span class="sandbox-dot" aria-hidden="true"></span>{sandbox_notice}</span><details class="connection-status"><summary><span class="status-dot {status_class}" aria-hidden="true"></span>{'Model verified' if status_class=='connected' else 'Model status'}<span aria-hidden="true">⌄</span></summary><div><b>Current connection</b><p>{escape(status)}</p><a href="/agent">Connection details →</a></div></details></div>
<div class="content-area">{guidance}{localnav}<main id="main" class="feature-workspace" tabindex="-1">{body}</main></div>
<footer class="workspace-footer"><span>Fluxora · Cashflow Action Copilot</span><span>{sandbox_context} · financial results from deterministic tools · model prose is unverified</span><a href="/docs/security">Boundaries & limitations</a></footer></div></div>'''
    return document(title,shell,extra='<style>'+legacy_css+'</style>')


def task_review(plan):
    """Display the exact server-validated plan; never infer or change its meaning."""
    from .agent_tasks import title
    p=plan['parameters']; task=plan['task']; fields=[]
    if task=='action_proposal':
        fields=[('Action',p['kind'].replace('_',' ').capitalize()),('Record',p['target_id']),
                ('Amount',p['currency']+' '+p['amount']),('Requested date',p['date']),('Result','DRAFT only — no approval or cash movement')]
    elif task=='proposal':
        for i,step in enumerate(p['steps'],1):
            fields.append((f'Step {i}',step['operation'].replace('_',' ').capitalize()))
            for key,value in step['arguments'].items():
                # Workflow argument money fields are explicitly named minor units.
                shown=str(value)
                if key.endswith('_minor'):shown+=' (minor units)'
                fields.append((key.replace('_',' ').capitalize(),shown))
        fields.append(('Result','Pending owner review — no operation applied'))
    else:
        fields=[('Report',title(task)),('Effect','Read only — saved financial records stay unchanged')]
        scenario=p.get('scenario')
        if scenario:
            for key,value in scenario.items():fields.append((key.replace('_',' ').capitalize(),str(value)))
        for key,value in p.items():
            if key!='scenario':fields.append((key.replace('_',' ').capitalize(),str(value)))
        if not p:fields.append(('Inputs','Current recorded snapshot and assumptions'))
    return '<section class="card interpretation"><div class="eyebrow">Your reviewed instruction</div><h2>'+escape(title(task))+'</h2><dl class="review-fields">'+''.join('<div><dt>'+escape(k)+'</dt><dd>'+escape(v)+'</dd></div>' for k,v in fields)+'</dl></section>'
