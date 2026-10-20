"""Multipage product site, local documentation and deterministic dashboard."""
from __future__ import annotations

from html import escape as esc
from pathlib import Path

from . import ui

SITE_NAV=(('/product','Product'),('/how-it-works','How it works'),('/trust','Trust'),('/docs','Docs'))
assets={'fluxora.css':'text/css; charset=utf-8','fluxora-polish.css':'text/css; charset=utf-8','fluxora-workspace.css':'text/css; charset=utf-8','fluxora.js':'text/javascript; charset=utf-8','fluxora.svg':'image/svg+xml','fluxora-mark-v1.png':'image/png','fluxora-planning-v1.png':'image/png','fluxora-evidence-v1.png':'image/png'}
assets.update({'fluxora-assistant.css':'text/css; charset=utf-8','fluxora-assistant.js':'text/javascript; charset=utf-8'})
assets.update({'fluxora-motion.css':'text/css; charset=utf-8','fluxora-motion.js':'text/javascript; charset=utf-8'})
assets.update({'fluxora-guide.css':'text/css; charset=utf-8','fluxora-guide.js':'text/javascript; charset=utf-8'})


def public_page(title,body,path):
    links=''.join('<a href="'+url+'"'+(' aria-current="page"' if path==url or (url=='/docs' and path.startswith('/docs/')) else '')+'>'+label+'</a>' for url,label in SITE_NAV)
    links+='<a href="/demo/examples">Demo examples</a>'
    cta='<a class="btn primary" href="/app">Open workspace '+ui.icon('arrow')+'</a>'
    header='<header class="site-header"><div class="site-container site-header-inner"><a class="brand" href="/welcome" aria-label="Fluxora home">'+ui.wordmark()+'</a><nav class="site-nav" aria-label="Product navigation">'+links+cta+'</nav><details class="site-mobile-nav"><summary>Menu</summary><nav aria-label="Product mobile navigation">'+links+'<a href="/about">About</a>'+cta+'</nav></details></div></header>'
    footer='<footer class="site-footer"><div class="site-container"><div class="footer-grid"><div class="footer-brand"><a class="brand" href="/welcome">'+ui.wordmark()+'</a><p>See ahead.<br>Act with clarity.</p><span class="demo-tag">SYNTHETIC DEMONSTRATION</span></div><nav aria-label="Product footer links"><h2>Product</h2><a href="/product">Overview</a><a href="/how-it-works">How it works</a><a href="/app">Demo workspace</a><a href="/trust">Trust &amp; limitations</a></nav><nav aria-label="Resource footer links"><h2>Resources</h2><a href="/start">Get started</a><a href="/docs">Documentation</a><a href="/docs/local-setup">Local setup</a><a href="/docs/guided-agent">Agent guide</a><a href="/docs/troubleshooting">Troubleshooting</a></nav><nav aria-label="Project footer links"><h2>Project</h2><a href="/about">About Fluxora</a><a href="/docs/security">Security boundaries</a><a href="/workspace/features">All features</a><a href="/docs/recovery">Backup &amp; recovery</a></nav></div><div class="footer-bottom"><span>Fluxora · Cashflow Action Copilot</span><span>No real bank connection, counterparty messages or payments.</span><button type="button" class="motion-toggle" data-motion-toggle aria-pressed="false" hidden>Motion off</button></div><p class="footer-disclosure">Forecasts depend on recorded inputs; they are not guarantees. Fluxora is a working project name; brand availability has not been cleared.</p></div></footer>'
    return ui.document(title,header+'<main id="main" class="site-container" tabindex="-1">'+body+'</main>'+footer,kind='site')


def cta_panel():
    return '<section class="site-cta"><div><div class="eyebrow">A clearer next step</div><h2>Start with the cash.<br>See where it takes you.</h2><p>Explore the synthetic workspace. Bring the position, the assumptions and the next decision into focus.</p></div><div class="cta-actions"><a class="btn" href="/app">Explore the workspace '+ui.icon('arrow')+'</a><a href="/start">Take a guided tour →</a></div></section>'


def feature(glyph,title,copy,href,label):
    return '<article class="site-feature">'+ui.icon(glyph)+'<h3>'+title+'</h3><p>'+copy+'</p><a href="'+href+'">'+label+' →</a></article>'


def brand_story(kind='planning'):
    """Conceptual editorial artwork, separate from all financial result cards."""
    if kind=='evidence':
        image='fluxora-evidence-v1.png'
        title='A clear thread from evidence to decision.'
        copy='Keep the source within reach. Inspect the proposal. Make the owner decision separately. Every step has a clear place in the workflow.'
        link='/docs/approvals';label='Explore the review workflow'
    else:
        image='fluxora-planning-v1.png'
        title='Turn a cash question into a considered next step.'
        copy='Connect the recorded position, the possible scenarios and the decision ahead. Fluxora helps you see the path while keeping you in control.'
        link='/how-it-works';label='Follow the process'
    return '<section class="brand-story"><figure class="brand-art"><img src="/assets/'+image+'" width="1536" height="1024" alt="" loading="lazy" decoding="async"><figcaption>Concept illustration · not financial data or evidence</figcaption></figure><div class="brand-story-copy"><div class="eyebrow">Clarity, connected</div><h2>'+title+'</h2><p>'+copy+'</p><a class="btn soft" href="'+link+'">'+label+' '+ui.icon('arrow')+'</a></div></section>'


def demo_preview(web,example='baseline'):
    # Public examples always use isolated seed data, never the active business.
    from .ledger import Ledger
    from .forecast import forecast
    from .planning import stress_test
    from .authz import owner_context_for
    ledger=Ledger.seed()
    fc=(stress_test(ledger,'inv-client-001',7,ctx=owner_context_for(ledger.business_id)).scenario
        if example=='delay' else forecast(ledger,ledger.as_of,ledger.horizon_days))
    cf=fc.currencies[ledger.base_currency]
    selected=lambda name:' aria-current="page"' if name==example else ''
    tabs='<nav class="sample-switch" aria-label="Synthetic forecast example"><a href="/welcome?example=baseline#product-preview"'+selected('baseline')+'>Recorded timing</a><a href="/welcome?example=delay#product-preview"'+selected('delay')+'>Customer pays 7 days late</a></nav>'
    condition='Seven-day collection delay · hypothetical only' if example=='delay' else 'Current recorded timing · baseline example'
    risk='<span class="pill warn">Floor breach '+str(cf.first_breach_date)+'</span>' if cf.first_breach_date else '<span class="pill ok">No breach under assumptions</span>'
    cards=''.join('<div class="sample-metric"><span>'+label+'</span><b>'+value+'</b><small>'+note+'</small></div>' for label,value,note in [
        ('Recorded cash · USD',web.money(ledger.actual_cash('USD'),'USD'),'Snapshot '+str(ledger.as_of)),
        ('Lowest projected · USD',web.money(cf.min_balance_minor,'USD'),str(cf.min_balance_date)),
        ('Cash-floor shortfall',web.money(cf.worst_shortfall_minor,'USD'),'Conditional · USD floor '+web.money(cf.floor_minor,'USD')),
        ('Recorded cash · EUR',web.money(ledger.actual_cash('EUR'),'EUR'),'Separate currency · no FX pooling')])
    rail='<aside class="sample-rail"><span class="sample-rail-brand">'+ui.wordmark()+'</span><span class="sample-rail-label">WORKSPACE</span><a href="/app">'+ui.icon('grid')+' Overview</a><a class="current" href="/forecast">'+ui.icon('chart')+' Forecast</a><a href="/scenarios">'+ui.icon('layers')+' Scenarios</a><a href="/ledger">'+ui.icon('file')+' Evidence</a><a href="/agent/tasks">'+ui.icon('spark')+' Agent</a><span class="sample-rail-foot">Synthetic product example</span></aside>'
    drivers=''.join('<div class="sample-driver"><span>'+esc(o.id)+'<small>'+str(o.date)+(' · Protected' if o.protected else ' · Scheduled')+'</small></span><b>'+web.money(o.remaining_minor,o.currency)+'</b></div>' for o in sorted(ledger.outflows.values(),key=lambda o:o.date))
    b='<section id="product-preview" class="product-stage" aria-labelledby="sample-title"><div class="stage-toolbar"><div><span class="stage-dot"></span><span>Product preview</span></div><span>Local calculations. Synthetic data.</span></div><div class="sample-app">'+rail+'<div class="sample-body"><div class="sample-header"><div><div class="eyebrow">Demo Atlas Agency · '+str(ledger.as_of)+'</div><h2 id="sample-title">A view of what is ahead.</h2></div><span class="demo-tag">SYNTHETIC EXAMPLE</span></div>'+tabs+'<div class="sample-metrics">'+cards+'</div><div class="sample-panels"><section class="sample-chart"><div class="section-head"><h3>Cash outlook · USD</h3>'+risk+'</div><p>'+condition+'</p>'+web.forecast_svg(cf,width=720,height=250)+'<div class="chart-legend"><span class="legend-line">Closing balance</span><span class="legend-line low">Intraday low</span><span class="legend-line floor">Cash floor</span></div></section><aside class="sample-obligations"><h3>Upcoming obligations</h3>'+drivers+'<a href="/account-forecast">Check account funding →</a></aside></div><details class="sample-accessible"><summary>Inspect the computed daily data</summary>'+web.forecast_table(cf)+'</details><p class="sample-caption">Expected collections remain conditional. Example source: inv-client-001 / ev-client-invoice. Switching examples changes no saved record. Workspace links open the active local app.</p></div></div></section>'
    return b


def home_body(web,example='baseline'):
    from .site_motion import artwork
    points=''.join('<span>'+ui.icon(glyph)+label+'</span>' for glyph,label in [('chart','Calculated locally'),('check','Owner-reviewed actions'),('save','No model needed for reports')])
    hero='<section class="pro-hero motion-hero"><div class="hero-copy"><a class="release-chip" href="/how-it-works"><span class="chip-dot"></span>Cashflow Action Copilot '+ui.icon('arrow')+'</a><h1>See your cash.<br><em>Find your next move.</em></h1><p>A focused workspace for the cash decisions ahead. Trace the numbers, compare the options and prepare the next step for review.</p><div class="hero-ctas"><a class="btn primary" href="/app">Explore the workspace '+ui.icon('arrow')+'</a><a class="btn" href="/start">Take the guided tour</a></div><div class="pro-points">'+points+'</div><p class="hero-scope">Synthetic demo · no bank connection or real payments.</p></div>'+artwork()+'</section>'
    index='<nav class="product-index" aria-label="Explore the product"><span>ONE WORKSPACE, A CLEARER PROCESS</span><a href="#product-preview">01 · See the position</a><a href="#plan-decisions">02 · Compare the options</a><a href="#agent-workflow">03 · Prepare the next step</a></nav>'
    preview_intro='<header class="section-heading"><div><div class="eyebrow">01 / Visibility</div><h2>Start with the numbers.</h2></div><p>Switch between two computed examples. Inspect the inputs and daily data behind each result.</p></header>'
    intro='<header class="section-heading" id="plan-decisions"><div><div class="eyebrow">02 / Planning</div><h2>Make room for a better decision.</h2></div><p>Follow the timing, test a change and keep the evidence close. Every part of the workflow has its place.</p></header>'
    features='<div class="site-feature-grid numbered-features">'+feature('chart','Know when cash gets tight','Follow daily balances, the recorded cash floor and the dates that need attention.','/forecast','Open forecast')+feature('layers','Compare before committing','Model a delayed collection or compare allowed plans. A scenario changes no observed payment.','/scenarios','Explore scenarios')+feature('check','Review the exact next step','Check proposed terms before a separate owner decision. Approval is never evidence of payment.','/agent/workflows','Open approval queue')+'</div>'
    agent='<section class="agent-feature" id="agent-workflow"><div class="agent-feature-copy"><div class="eyebrow">03 / Preparation</div><h2>Your question.<br>A reviewable next step.</h2><p>Use direct reports without a model. With a privately configured provider, translate a supported question into an exact task, review it and let local tools compute the answer.</p><a class="btn primary" href="/agent/tasks">Explore the guided agent '+ui.icon('arrow')+'</a><a class="data-link" href="/docs/guided-agent">How interpretation and review work →</a></div><div class="agent-feature-panel"><span class="agent-panel-label">EXAMPLE WORKFLOW · NO TASK HAS RUN</span><blockquote>What happens if inv-client-001 pays 7 days late?</blockquote><ol><li><b>Interpret</b><span>Forecast scenario · invoice delay · 7 days</span></li><li><b>Review</b><span>Check the task, record identity and exact parameters.</span></li><li><b>Compute</b><span>Deterministic tools return the result and source references.</span></li></ol><p>The model can prepare DRAFT or pending work. Owner decisions, counterparty acceptance and observed cash remain separate.</p></div></section>'
    destinations='<section class="workspace-map" aria-labelledby="workspace-map-title"><header class="section-heading"><div><div class="eyebrow">Find your way</div><h2 id="workspace-map-title">Everything has a place.</h2></div><a href="/workspace/features">Browse all workspace pages '+ui.icon('arrow')+'</a></header><div class="workspace-map-grid">'+''.join('<a href="'+href+'"><span class="map-glyph">'+ui.icon(glyph)+'</span><span><b>'+title+'</b><small>'+copy+'</small></span>'+ui.icon('arrow')+'</a>' for glyph,title,copy,href in [('file','Evidence & records','Inspect sources, imports and missing facts.','/ledger'),('wallet','Accounts & obligations','Review recorded cash, bills and payroll.','/cash-accounts'),('clock','Action history','Follow proposals and recorded lifecycle steps.','/actions'),('link','Telegram practice','Try the simulated companion without an account.','/telegram/demo'),('book','Documentation','Setup, workflows and honest limitations.','/docs'),('settings','API & Telegram setup','Private local configuration and a locked hosted preview.','/docs/model-configuration')])+'</div></section>'
    faq='<section class="home-faq"><div><div class="eyebrow">Before you begin</div><h2>A clear view<br>of the boundaries.</h2><a href="/trust">Trust & limitations →</a></div><div>'+''.join('<details><summary>'+q+'</summary><p>'+answer+'</p></details>' for q,answer in [
        ('Does this work without a model?','Yes. Financial reports, evidence review, scenarios and owner forms work without a model. Natural-language task translation needs a privately configured provider and internet. The hosted operator can explicitly enable bounded public guided AI. Direct reports always work without a model.'),
        ('What can the agent do?','Read supported reports and prepare DRAFT or pending work. You review the interpretation and separately decide on exact operations. The model cannot approve, invent receipts or move money.'),
        ('Is the demonstration connected to my bank?','No. All demonstration records are synthetic. The app has no real bank integration, payment rail or counterparty messaging. The optional private Telegram companion is a separate opt-in connection.'),
        ('Can I check where a result came from?','Inspect the recorded inputs, assumptions, currencies, snapshot date and source references. Deterministic calculation does not certify that an input source is authentic or complete.')])+'</div></section>'
    return hero+index+preview_intro+demo_preview(web,example)+intro+features+agent+destinations+brand_story()+faq+cta_panel()


PUBLIC = {
    '/product':('Product','Cash-flow planning, connected.','One workspace to understand the position, explore alternatives, and review the next action.',[
        ('chart','Forecast from recorded facts','Daily and thirteen-week views show currency-separated balances, floor breaches and conditional collections. Inspect the dates, assumptions and source references behind the result.','/forecast','Open forecast'),
        ('layers','Compare the practical options','Try invoice delays, partial collections and permitted plan alternatives. Preserve payroll protection, consent requirements and honest no-feasible-plan outcomes.','/scenarios','Explore scenarios'),
        ('spark','An agent with a clear role','The guided model translates a question into a supported task. Local code validates it and computes the financial answer. The owner reviews the exact interpretation.','/agent/tasks','Open guided tasks'),
        ('file','Keep evidence within reach','Review staged imports, source identities, attachments, duplicate flags and missing facts. A document does not grant permission or prove that money arrived.','/ledger','Explore the ledger'),
        ('check','Make review part of the workflow','DRAFTs, approvals, acceptance, observed payments and reconciliation have different meanings. Every step keeps its own boundary.','/agent/workflows','Open approval queue'),
        ('save','Keep the workspace local','Python and SQLite hold local records. Direct reports and reviewed forms work without a model. Optional model tasks need a configured external provider.','/docs/local-setup','Read setup instructions'),
    ]),
}

DOCS = {
 'getting-started':('Getting started','Start with the snapshot, then follow the cash.', '''
<p>Fluxora is an English SME cash-flow planning workspace. This release is a <strong>local synthetic sandbox</strong>, with optional model-assisted task interpretation. It has no production login, bank integration, or real payment/message execution.</p>
<h2>Your first five minutes</h2><ol><li>Open <a href="/app">Overview</a>. Read the business snapshot date, cash currencies, and forecast minimum.</li><li>Open <a href="/today">Today</a> for missing facts and decisions needing review.</li><li>Inspect a forecast source in <a href="/ledger">Evidence & ledger</a>.</li><li>Try a <a href="/scenarios">scenario</a>. Its forecast does not rewrite recorded payments.</li><li>Use a <a href="/agent/tasks">direct agent report</a> without any model, or review a qualified model's task interpretation.</li></ol>
<div class="article-note">Proposed cash is not observed cash. Owner approval is not counterparty acceptance or evidence of payment.</div>
<h2>Find the rest of the workspace</h2><p>The sidebar groups planning, records, review and help. A selected section exposes its related pages as tabs. <a href="/workspace/features">All workspace features</a> lists every destination.</p>'''),
 'local-setup':('Running locally','A small local server, a persistent SQLite ledger.', '''
<p>Use Python 3.10 or newer. Extract the complete source folder and double-click <strong>START_FLUXORA.cmd</strong> on Windows. Runtime uses the standard library: no pip packages or network download. Alternatively, from Windows PowerShell:</p>
<pre><code>.\\tools\\setup_local.ps1 -Python python
.\\tools\\setup_local.ps1 -Python python -CheckOnly</code></pre>
<p>On macOS/Linux: <code>python3 -m tools.local_setup --start --open-browser</code>. The default persistent database is <code>.runtime/local/state.sqlite</code>. This single-owner local workspace has no 24-hour visitor expiry. Use the exact displayed 127.0.0.1 address; Host, Origin and CSRF checks protect local forms.</p>
<p>Open <a href="/welcome">the product website</a>, <a href="/app">the app</a>, or <a href="/docs">documentation</a> at the same local port. The default local CLI binds to 127.0.0.1. A new database starts with the synthetic fixture; an existing database is preserved.</p>
<h2>Restart without losing work</h2><p>Stop the server with Ctrl+C and run the same command with the <strong>same database path</strong>. Saved requests and approved local work remain. Connection verification starts unverified after restart. In-progress staged reviews may expire and must be reviewed again.</p>
<h2>Model-free use</h2><p>The ledger, deterministic reports, scenarios and reviewed owner forms work without a model key. Natural-language task translation needs internet and an external compatible model. See <a href="/docs/model-configuration">model configuration</a>.</p>
<h2>API and Telegram Web settings</h2><p>In the persistent local launcher, open <strong>Settings &amp; help → API &amp; Telegram</strong> at <code>/settings/connections</code>. Add multiple compatible API/model profiles and choose one active connection. Configure, test and explicitly start/stop a private Telegram bot on this same ledger. Default credentials are session memory; optional Windows Remember uses current-account DPAPI encryption. Restart loads remembered settings unverified and never starts the bot automatically. Hosted/legacy modes do not expose these credential forms.</p><p>Do not run a second poller for the active VPS bot; use a separate local bot. Computer and app must stay on for replies. Read <code>docs/LOCAL-SETUP.md</code> for Web settings, alternative hidden terminal prompts, existing data paths, backup/move and troubleshooting. No model/bot call occurs during readiness checks. The legacy serve command does not enable this local adapter or run lock.</p>
<p>The default service runs locally. Optional hosted modes are synthetic demonstrations: authenticated mode uses one shared owner credential; public mode gives each browser session a separate expiring synthetic workspace. Neither mode provides multi-user production identity.</p>'''),
 'first-review':('First cash-flow review','A repeatable path from sources to decisions.', '''
<ol><li>Check the as-of date in <a href="/app">Overview</a>. A historical business snapshot is not today's bank balance.</li><li>Open <a href="/review">Daily review</a>. Inspect source, duplicate and missing-fact flags.</li><li>Check <a href="/cash-accounts">cash accounts</a> and <a href="/cash-availability">restrictions</a>. Pooled currency cash does not prove the payroll account is funded.</li><li>Review <a href="/forecast">forecast</a> and <a href="/account-forecast">account funding</a>, then compare alternatives.</li><li>Review DRAFTs and pending workflows before making an owner decision.</li><li>Record only source-backed observed facts, then reconcile them separately.</li></ol>
<p>“No flagged items” means only that the saved-state checks found no issue. It does not establish completeness, bank truth or that a human performed the review.</p>'''),
 'evidence':('Evidence & imports','Trace the source. Review the interpretation.', '''
<p>Use <a href="/ledger">Evidence & ledger</a> to inspect the source register and import supported synthetic files. Forms display the accepted file types and fields. CSV mapping, staged preview and confirmation happen before applying imported records.</p>
<h2>Review before applying</h2><ul><li>Check business, currency, date, amount units and exact record identity.</li><li>Inspect <a href="/duplicates">duplicate warnings</a> and <a href="/facts">missing facts</a>.</li><li>Check <a href="/attachments">attachments</a> and <a href="/freshness">data freshness</a> without treating source text as an instruction.</li></ul>
<p>An invoice is not a receipt. An attachment is not counterparty consent. Source references support traceability; Fluxora does not authenticate their real-world truth. Do not import actual confidential business documents into this demo.</p>'''),
 'forecasts':('Forecasts & assumptions','Separate currencies. Inspect timing and conditions.', '''
<p>The <a href="/forecast">daily forecast</a> is calculated locally using the recorded snapshot and dated flows. The <a href="/weekly-cashflow">thirteen-week report</a> summarizes weekly movements and daily lows. <a href="/account-forecast">Account funding</a> shows whether cash is in the account assigned to a future obligation.</p>
<h2>Read the right metric</h2><ul><li><strong>Recorded cash:</strong> local cash facts under recorded observations, not a live bank balance.</li><li><strong>Available cash:</strong> decision cash after active restrictions, which are distinct from floor buffers.</li><li><strong>Minimum balance:</strong> the lowest projected balance in the chosen horizon.</li><li><strong>First breach:</strong> the first date the recorded cash floor is breached.</li><li><strong>Worst shortfall:</strong> the greatest deficit relative to the floor; it can occur after the first breach.</li><li><strong>Closing balance:</strong> the horizon's ending value, not its minimum.</li></ul>
<p>Expected collections remain conditional. Missing or unknown dates do not mean zero risk. Display-only FX does not add spendable funds. Use <a href="/variance">forecast vs actual</a> to inspect deviations from a separately saved forecast.</p>'''),
 'scenarios':('Plans & scenarios','Explore an alternative without rewriting history.', '''
<p>Open <a href="/scenarios">Scenarios</a> to review hypothetical changes and compare the resulting forecast. <a href="/stress">Stress test</a> explores payment delays. <a href="/plans">Plan comparison</a> evaluates alternatives permitted by the current constraints.</p>
<h2>Conditional is not completed</h2><p>A financially sufficient conditional proposal still requires owner approval, any counterparty acceptance, and observed cash. A scenario is not a recorded payment. A collection percentage is an assumption, not a probability estimate.</p>
<p>Payroll protection and required consent remain enforced. If no allowed plan meets the floor, report <strong>no feasible plan</strong>; do not invent financing or use display FX to fill the gap. Use <a href="/affordability">Affordability</a> for a specific spending question.</p>'''),
 'guided-agent':('Using the guided agent','Describe. Run. Inspect the result.', '''
<p>Open <a href="/agent/tasks">Agent</a>. Direct reports make no model call. Describe a task only when the configured model has passed the current synthetic qualification.</p>
<ol><li>Write the task. Specify exact record IDs, currency, amounts and ISO dates for changes.</li><li>Read the provider destination and data notice beside the submit buttons. Choose Send to send the question for interpretation and run a supported report or prepare DRAFT/pending work in one submission. No ledger or attachments are sent in model context.</li><li>Alternatively, choose Options → Preview before sending to inspect the outbound messages and then the exact interpretation before running anything.</li><li>Inspect the selected task, exact terms, code-generated answer and source references. A schema-valid interpretation can still misunderstand intent; the one-step path does not certify it.</li><li>Approve any pending change separately after reviewing its terms and evidence. Missing facts still require clarification; no consent, receipt or payment is invented.</li></ol><p>One-step tasks share a 30-second deadline across interpretation and local execution. There is no background worker or automatic retry. Reposting the same completed request retrieves its saved historical result, including after restart. Local report links skip the provider entirely.</p>
<h2>Examples</h2><blockquote>Show the baseline cash forecast for every currency. Read only.</blockquote><blockquote>Compute a what-if forecast with inv-client-001 delayed 7 days. Do not change any record.</blockquote><blockquote>Prepare an early-payment request draft for inv-client-001, USD 100 on 2026-10-22. Owner review only.</blockquote>
<p>Examples refer to the synthetic fixture; use records that actually exist in your active session. Missing or invalid fields require clarification. A model cannot approve itself or create proof of consent/payment. The <a href="/agent">advanced agent</a> remains separate; its optional prose is explicitly unverified.</p>'''),
 'approvals':('DRAFTs & approvals','Each decision has its own boundary.', '''
<table aria-label="Workflow status meanings"><thead><tr><th>Status</th><th>Meaning</th></tr></thead><tbody><tr><td>DRAFT</td><td>A proposed action. No approval, message or cash movement.</td></tr><tr><td>Pending approval</td><td>Exact local workflow terms await a separate owner decision.</td></tr><tr><td>Approved</td><td>The owner approved reviewed terms. It does not prove external acceptance or payment.</td></tr><tr><td>Accepted</td><td>A separate source-backed counterparty acceptance was recorded or explicitly simulated.</td></tr><tr><td>Observed</td><td>A source-backed synthetic payment observation was recorded once.</td></tr><tr><td>Reconciled</td><td>A separate matching/reconciliation step was recorded.</td></tr></tbody></table>
<p>Use <a href="/agent/workflows">Approval queue</a> for pending operations and <a href="/actions">Action history</a> for action lifecycle review. Check exact terms, source facts and current data before approval. Changed data or stale terms can require fresh review.</p>
<p>The model's role remains read/DRAFT-only. Owner confirmation of an observed fact is not independent authentication. The outbox and counterparty simulator are sandbox demonstrations; they do not send actual messages or move funds.</p>'''),
 'reconciliation':('Payments & reconciliation','Match observations; never invent a balancing payment.', '''
<p>Use <a href="/receipts">Receipt review</a> and <a href="/payments">Bill payments</a> to review source-backed synthetic observations. <a href="/statements">Statement reconciliation</a> matches saved records with imported statement rows. <a href="/statement-mapping">Statement mapping</a> reviews parsing, own account, period and column meanings first.</p>
<ul><li>Keep observed payment dates, amounts, source IDs and idempotency identities explicit.</li><li>Match an existing payment instead of recording it twice.</li><li>Use correction workflows rather than silently changing history.</li><li>Review statement opening/closing boundaries separately.</li><li>Do not assume a matching row establishes account completeness or bank authenticity.</li></ul>
<p>A header-only statement is a zero-activity source assertion, not verified proof of a bank balance. Reconciliation does not create new cash or imply counterparty consent.</p>'''),
 'model-configuration':('Model configuration','Optional external interpretation, private local connection profiles.', '''
<p>The persistent local launcher provides <strong>Settings &amp; help → API &amp; Telegram</strong> at <code>/settings/connections</code>. Save up to 12 named profiles for OpenRouter, Gemini compatible, DeepSeek, Groq, Mistral or a custom OpenAI-compatible endpoint. One connection is active; there is no automatic provider fallback. Enter the exact model ID and key. Saving makes no call; explicitly test the displayed endpoint/model before qualification.</p><p>Default keys live in server memory until exit. Windows Remember optionally encrypts a separate local vault for the current OS account, outside financial SQLite/exports. Other systems use session-only Web settings. Keys are never prefilled or returned in HTML. OS account software/admins remain trusted. Do not package the private vault. These credential forms are unavailable on hosted/public instances.</p>
<p>Environment remains an alternative and is required by the legacy/hosted entrypoints:</p><pre><code>CASHFLOW_MODEL_API_KEY
CASHFLOW_MODEL_BASE_URL
CASHFLOW_MODEL_NAME</code></pre>
<p>The API key is a secret. Enter it only in the private local password field or terminal environment/hidden prompt. Do not paste it into chat, an agent question, source code or a shared screenshot. Do not commit it or include it in a submission archive. The app reads configuration server-side; the model name is required for chat.</p>
<h2>OpenRouter helper</h2><p>From the source folder, use the included PowerShell helper with the Python executable you installed:</p><pre><code>.\\tools\\start_openrouter.ps1 -Python .\\.venv\\Scripts\\python.exe -PromptForKey</code></pre>
<p>The helper prompts locally for the key. Existing explicit environment model selections are preserved; the guided preset is <code>qwen/qwen3.7-flash</code>. This is compatibility evidence from development, not a permanent model recommendation.</p>
<h2>Verify and qualify</h2><p>Verify the connection in <a href="/agent">connection details</a>. Then use <a href="/agent/tasks">the explicit synthetic qualification</a>. It can incur charges: 13 selected application cases, with 8 model translations and 5 local guard cases in the current contract.</p>
<p>Qualification lasts at most seven days and is tied to model, endpoint, request profile and relevant code. Restart begins connection status unverified. Configuration changes invalidate the relevant checks. A passing check does not establish general language accuracy, source truth, quota or future uptime. Model-free reports stay available.</p>'''),
 'recovery':('Backup & recovery','Keep the ledger path and recovery copies deliberate.', '''
<p>Open <a href="/backups">Backup & restore</a>. Create a named recovery copy before experimenting with imports, reset or destructive test data changes. Review the restore preview before confirming; it replaces state in the current local ledger.</p>
<p>Keep private backups outside public source/submission packages. Protect them as business records. A backup is useful only if you can restore and inspect it in an isolated local test session.</p>
<h2>Request replay</h2><p>Saved request links reopen historical results without rerunning them. Reloading a completed request does not create another DRAFT. After a restart, interrupted requests do not silently resume. New tasks require fresh review and can incur new provider charges.</p>
<p>Never restore or reset another user's active session. Keep an unchanged original before a recovery exercise.</p>'''),
 'troubleshooting':('Troubleshooting','Understand the state before retrying.', '''
<h2>Model disconnected</h2><p>Use direct reports and owner forms. In the persistent local launcher, check the selected profile in API &amp; Telegram settings. For environment/legacy mode, check the three model environment variables in the terminal that starts the server. Do not disclose the key.</p>
<h2>Configured but unverified</h2><p>Configuration exists, but this running server has not verified it. An explicit connection check is available in <a href="/agent">connection details</a>. Restart intentionally clears verification.</p>
<h2>HTTP 429, cooldown or timeout</h2><p>Wait for the displayed cooldown before explicitly retrying. A verified connection does not promise capacity. There is no silent model switch or background retry. A late/incomplete task is not a successful result and cannot save candidate DRAFTs.</p>
<h2>Qualification missing</h2><p>Run the potentially billed synthetic check explicitly, or use a direct report. Never relabel an old check as passing for changed model/code.</p>
<h2>Review expired or data changed</h2><p>Open a fresh task and review the current snapshot. Do not try to reuse changed hidden parameters or an old approval token.</p>
<h2>Unexpected forecast</h2><p>Inspect dates, currencies, restricted funds, unknown account routing and expected collections. Missing evidence is not a zero balance. See <a href="/facts">Resolve facts</a>.</p>
<p>For an independent walkthrough, use <a href="/usability">the task session</a>. An agent-driven browser check does not count as a human participant.</p>'''),
 'security':('Security & limitations','Explicit boundaries, without absolute guarantees.', '''
<h2>Bundled task skills</h2><p>Six reviewed skills reuse existing deterministic tools. Explicit selection narrows the allowed tasks; automatic suggestions can be wrong and require interpretation review. Uploaded documents cannot install skills or grant authorization. Public guided AI is an explicit operator option with separate qualification and a durable daily allowance. Private owner Telegram commands remain separate. <a href='/agent/skills'>Explore skills and local reports</a>.</p><h2>Deployment boundary</h2><p>The default CLI binds to localhost. An explicitly configured hosted demonstration runs behind HTTPS. Authenticated mode uses one shared owner credential. Public mode removes login and gives each browser session a separate synthetic ledger, upload staging, history and backups using an opaque secure cookie. Workspaces expire after 24 hours; clearing cookies loses access. Cookie holders can access that session, and host administrators remain trusted. The operator can enable public guided task translation after synthetic qualification. Only a confirmed question and static task definitions reach the model. Shared daily limits, exact review and separate owner approval still apply. An optional allowlisted private Telegram companion can use a separately enabled model in the same server process. This is anonymous session separation, not a production identity or account system. Hosted records live on that server, not in your browser. There is no bank integration, real payment rail or outbound messaging service. Host administrators remain trusted; credentials and data require separate operational protection.</p>
<h2>Financial and model boundary</h2><p>Integer minor units and deterministic tools supply financial results. The guided model selects a task; it never receives financial results for a final rewrite. Invalid, guessed, stale or late instructions are rejected. Exact owner review is still necessary: a supported task can misunderstand the question.</p>
<p>CSRF checks, business-scoped permissions, transactional saves, durable request identity, bounded provider responses and error redaction are implemented controls, not claims of universal attack resistance. Model prose in the advanced mode remains unverified.</p>
<h2>Evidence and availability</h2><p>Source references do not authenticate the source. Finite local guards do not cover every paraphrase. Provider billing, remote work after local timeout, outages and account-wide quotas are outside the local app's control. Incorrect input data and software bugs can produce incorrect results.</p>
<h2>Release use</h2><p>This is a synthetic hackathon demonstration with a redesigned interface. Independent human usability testing, production hardening and any real-business pilot remain separate work. No regulatory compliance, financial certification or global uniqueness is claimed.</p>'''),
 'glossary':('Glossary','The terms behind the workflow.', '''
<dl class="review-fields"><div><dt>As-of date</dt><dd>The recorded business snapshot date.</dd></div><div><dt>Minor unit</dt><dd>The integer unit used for money, such as a cent for USD.</dd></div><div><dt>Cash floor</dt><dd>A recorded owner buffer below which projected cash is flagged.</dd></div><div><dt>Expected collection</dt><dd>A conditional future inflow, not an observed receipt.</dd></div><div><dt>Restricted cash</dt><dd>Recorded funds excluded from decision availability while restricted.</dd></div><div><dt>DRAFT</dt><dd>A proposal with no approval or payment effect.</dd></div><div><dt>Idempotency</dt><dd>Replaying the same accepted identity does not duplicate its effect.</dd></div><div><dt>Qualification</dt><dd>A bounded synthetic application check for a specific model configuration and code contract.</dd></div><div><dt>Reconciliation</dt><dd>A separate review/match of recorded observations and source rows.</dd></div></dl>'''),
}


DOCS['telegram']=('Telegram companion','One private owner chat connected to your synthetic browser workspace.', '''
<h2>Judge access without a Telegram account</h2><p><a href="/telegram/demo">Open the Telegram interactive demo</a>. It uses a fresh disposable Atlas practice ledger and the existing deterministic tools. Try forecast, an exact DRAFT review, separate snapshot approval and on-demand alert examples. Delivery is simulated and no live AI is connected. Practice changes never enter your main workspace or the real private bot. The lesson expires after 30 minutes, server restart or loss of its visitor session. Unknown text is not translated by a model. Live delivery and unrestricted language understanding are not established by this practice.</p>
<p>Telegram is optional and disabled until the server operator privately configures the bot token and one allowed numeric Telegram user ID. <a href='/telegram'>Open Telegram settings</a> in your own browser session, acknowledge the destination, generate a five-minute code and send <code>/link CODE</code> to your configured bot. Do not use someone else's code. No public form can change the allowed Telegram account.</p>
<h2>Reports and reviews</h2><p><code>/forecast</code> and <code>/plans</code> run local deterministic tools. <code>/draft INVOICE_ID USD 100 YYYY-MM-DD</code> asks you to review an early-payment DRAFT; confirmation creates only that DRAFT. <code>/snapshot LABEL</code> first prepares a pending snapshot workflow. Its separate Approve button saves the exact reviewed forecast snapshot. Receipts, observed payments and consent are never inferred from generic approval; other workflow decisions require Web review.</p>
<h2>Optional private model</h2><p>Ordinary text uses a live translation only when the operator enables a private model and you complete <code>/verify</code> and <code>/qualify</code> checks using their potentially billed confirmation buttons. Your question and static task schema go to that provider; the ledger and attachments do not. Exact interpretations still need review and financial answers come from deterministic tools. Public guided task translation is an independent operator option. Browser Telegram practice remains simulated and makes no live AI calls. Model/provider availability is not guaranteed.</p>
<h2>Notifications and limits</h2><p>Use <code>/alerts on</code> to opt in to real Telegram messages about synthetic cash-gap and pending/action updates; <code>/alerts off</code> stops alerts. <code>/status</code> checks the link; <code>/unlink</code> or Web Disconnect revokes pending buttons. Only one browser workspace can be linked at a time. Pairing does not extend its 24-hour lifetime. Messages may be delayed or lost; an uncertain outbound send is not automatically retried. Check Web history before trying again. Telegram account/cookie compromise and host administrators remain outside this demo boundary.</p>''')


DOC_GROUPS = (
    ('Start here',('getting-started','local-setup')),
    ('Plan & review',('evidence','forecasts','scenarios','guided-agent','approvals','reconciliation')),
    ('Connect & operate',('model-configuration','telegram','recovery')),
    ('Reference',('troubleshooting','security','glossary')),
)


def docs_page(slug):
    import re
    title,subtitle,body=DOCS[slug]
    seen=set();groups=[]
    for label,keys in DOC_GROUPS:
        keys=[key for key in keys if key in DOCS];seen.update(keys)
        groups.append((label,keys))
    remaining=[key for key in DOCS if key not in seen]
    if remaining:groups.append(('More guides',remaining))
    nav=''.join('<div class="docs-nav-group"><h2>'+label+'</h2>'+''.join('<a href="/docs/'+key+'"'+(' aria-current="page"' if key==slug else '')+'>'+DOCS[key][0]+'</a>' for key in keys)+'</div>' for label,keys in groups if keys)
    headings=[]
    def heading(match):
        ident='guide-section-'+str(len(headings)+1)
        headings.append((ident,re.sub('<[^>]+>','',match.group(1))))
        return '<h2 id="'+ident+'">'+match.group(1)+'</h2>'
    body=re.sub(r'<h2>(.*?)</h2>',heading,body,flags=re.S)
    contents='<nav class="docs-contents" aria-label="On this guide"><b>On this guide</b>'+''.join('<a href="#'+ident+'">'+text+'</a>' for ident,text in headings)+'</nav>' if len(headings)>1 else ''
    keys=list(DOCS);index=keys.index(slug);prev=keys[index-1] if index else None;nextkey=keys[index+1] if index+1<len(keys) else None
    footer='<nav class="docs-end" aria-label="Documentation pagination">'+('<a href="/docs/'+prev+'">← '+DOCS[prev][0]+'</a>' if prev else '<a href="/welcome">← Product home</a>')+('<a href="/docs/'+nextkey+'">'+DOCS[nextkey][0]+' →</a>' if nextkey else '<a href="/app">Open workspace →</a>')+'</nav>'
    return '<div class="docs-layout"><aside class="docs-nav"><div class="nav-label">Fluxora documentation</div><nav aria-label="Documentation">'+nav+'</nav></aside><div><details class="docs-mobile"><summary>Documentation · '+title+'</summary><nav aria-label="Mobile documentation">'+nav+'</nav></details><article class="docs-article"><div class="eyebrow">Fluxora documentation</div><h1>'+title+'</h1><p class="section-lead">'+subtitle+'</p>'+contents+body+footer+'</article></div></div>'


def dashboard(web,ledger):
    from .forecast import forecast
    from .attention import attention_items
    from .treasury import treasury_report
    fc=forecast(ledger,ledger.as_of,ledger.horizon_days)
    attention=attention_items(ledger,ctx=web._owner_ctx(ledger))
    treasury=treasury_report(ledger,ctx=web._owner_ctx(ledger))
    pending=sum(1 for job in ledger.agent_workflows.values() if job['status']=='pending_approval')
    drafts=sum(1 for a in ledger.actions.values() if a.state=='draft')
    b='<div class="dashboard-intro"><div><div class="eyebrow">Your cash, in perspective</div><h1>Business overview</h1><p>'+esc(ledger.business_name)+' · Snapshot '+str(ledger.as_of)+' · '+str(ledger.horizon_days)+'-day horizon</p></div><div class="dashboard-actions"><a class="btn" href="/scenarios">'+ui.icon('layers')+' Test a scenario</a><a class="btn primary" href="/agent/tasks">'+ui.icon('spark')+' Ask Fluxora</a></div></div>'
    from .getting_started import entry
    b+=entry()
    b+='<div class="metric-grid">'
    for ccy,values in treasury['currencies'].items():
        b+='<article class="metric"><div class="metric-title">Recorded cash · '+esc(ccy)+ui.icon('wallet')+'</div><div class="metric-number">'+web.money(ledger.actual_cash(ccy),ccy)+'</div><div class="metric-foot">Available for decisions '+web.money(values['available_minor'],ccy)+'</div></article>'
    base=fc.currencies.get(ledger.base_currency)
    if base:
        b+='<article class="metric'+(' metric--alert' if base.min_balance_minor<base.floor_minor else '')+'"><div class="metric-title">Lowest forecast · '+esc(base.currency)+ui.icon('chart')+'</div><div class="metric-number">'+web.money(base.min_balance_minor,base.currency)+'</div><div class="metric-foot">'+str(base.min_balance_date)+' · conditional projection</div></article>'
    b+='<article class="metric metric--review"><div class="metric-title">Awaiting owner review'+ui.icon('check')+'</div><div class="metric-number">'+str(drafts+pending)+'</div><div class="metric-foot">'+str(drafts)+' DRAFT actions · '+str(pending)+' pending workflows</div></article></div>'
    b+='<div class="dashboard-columns"><div>'
    for ccy,cf in fc.currencies.items():
        breach='<span class="pill warn">Floor breach '+str(cf.first_breach_date)+'</span>' if cf.first_breach_date else '<span class="pill ok">No floor breach under assumptions</span>'
        b+='<section class="card"><div class="section-head"><h2>Cash outlook <span class="mut">/ '+esc(ccy)+'</span></h2>'+breach+'</div><p class="stat-note">'+str(ledger.horizon_days)+' days from '+str(ledger.as_of)+'. Expected collections are conditional; cash is never pooled across currencies.</p><div class="chart-meta"><div>Lowest projected<b>'+web.money(cf.min_balance_minor,ccy)+'</b></div><div>Cash floor<b>'+web.money(cf.floor_minor,ccy)+'</b></div><div>Worst shortfall<b>'+web.money(cf.worst_shortfall_minor,ccy)+'</b></div></div><div class="chart-frame">'+web.forecast_svg(cf)+'</div><div class="chart-legend"><span class="legend-line">Closing balance</span><span class="legend-line low">Intraday low</span><span class="legend-line floor">Cash floor</span></div><details><summary>Daily data and accessible table</summary>'+web.forecast_table(cf)+'</details><a class="data-link" href="/forecast">Inspect inputs and assumptions →</a></section>'
    due=sorted((o for o in ledger.outflows.values() if o.status=='scheduled' and o.date>=ledger.as_of),key=lambda o:o.date)
    b+='<section class="card"><div class="section-head"><h2>Upcoming obligations</h2><a href="/payments">All bills →</a></div>'
    for o in due[:5]:
        b+='<div class="compact-item"><div>'+esc(o.id)+(' <span class="pill warn">Protected</span>' if o.protected else '')+'<small>'+str(o.date)+' · unpaid remainder · '+web.evidence_link(ledger,o.evidence_id)+'</small></div><b>'+web.money(o.remaining_minor,o.currency)+'</b></div>'
    if not due:b+='<p class="empty-state">No future scheduled obligation recorded.</p>'
    b+='</section></div><div>'
    b+='<section class="card"><div class="section-head"><h2>Needs attention</h2><span class="count-bubble">'+str(len(attention))+'</span></div>'
    for item in attention[:4]:
        b+='<div class="need-item"><span class="need-icon">'+ui.icon('file')+'</span><div class="need-copy"><a href="'+esc(item['href'])+'">'+esc(item['title'])+'</a><small>'+esc(item['why'])+'</small></div></div>'
    if not attention:b+='<p class="stat-note">No flags in the saved-state checks. This does not prove data completeness.</p>'
    b+='<a class="btn text small" href="/today">Review priorities '+ui.icon('arrow')+'</a></section><section class="card"><div class="section-head"><h2>Expected collections</h2><a href="/collections">Review →</a></div>'
    recs=sorted((r for r in ledger.receivables.values() if r.remaining_minor>0),key=lambda r:(r.expected_date is None,str(r.expected_date)))
    for r in recs[:4]:
        b+='<div class="compact-item"><div>'+esc(r.id)+'<small>'+str(r.expected_date or 'Date unknown')+' · conditional</small></div><b>'+web.money(r.remaining_minor,r.currency)+'</b></div>'
    if not any(r.remaining_minor>0 for r in recs):b+='<p class="stat-note">No unpaid receivable recorded.</p>'
    b+='</section><section class="agent-callout">'+ui.icon('spark')+'<h2>A clearer next step.</h2><p>Ask a cash-flow question, review what Fluxora understood, and run a report or prepare a DRAFT.</p><a class="btn primary small" href="/agent/tasks">Open guided agent '+ui.icon('arrow')+'</a></section><section class="card"><div class="eyebrow">Keep the facts current</div><h2>Start with the sources.</h2><p class="stat-note">Recorded cash is not a live bank balance. Review opening sources, restrictions and missing facts.</p><a class="btn small" href="/freshness">Review data freshness</a></section></div></div>'
    b+='<div class="meta-strip"><span>Snapshot: '+str(ledger.as_of)+'</span><span>Local deterministic calculations</span><span>Protected payroll remains protected</span></div>'
    b+='<details class="dashboard-more"><summary>Assumptions, clarifications and display-only FX</summary><div><h2>Forecast assumptions</h2><ul>'+''.join('<li>'+esc(a)+'</li>' for a in fc.assumptions)+'</ul><h2>Open clarifications</h2><ul>'+(''.join('<li>'+esc(c['question'])+' — '+esc(c['why_it_matters'])+'</li>' for c in fc.clarifications) or '<li>None returned by these recorded-input checks.</li>')+'</ul><h2>Display-only currency conversion</h2>'+web.fx_status_html(ledger)+'</div></details>'
    b+='<p class="stat-note">Approval, counterparty acceptance, observed payment and reconciliation remain separate. <a href="/demo">Open the synthetic demo workflow</a>.</p>'
    return b


def mount(web):
    @web.route('GET','/demo/examples')
    def examples(handler,req):
        from .demo_examples import EXAMPLES,render
        from .errors import CashflowError
        values=req.query.get('example',['atlas'])
        if set(req.query)-{'example'} or len(values)!=1 or values[0] not in EXAMPLES:raise CashflowError('Choose one displayed demo example.')
        return handler._send_html(200,render(web,values[0],local=not hasattr(handler.server,'config')))
    @web.route('GET','/assets/<name>')
    def asset(handler,req,name):
        if name not in assets:return handler._send_html(404,web.page('Not found','<h1>Asset not found</h1>'))
        data=(Path(__file__).parent/'static'/name).read_bytes()
        handler.send_response(200);handler.send_header('Content-Type',assets[name]);handler.send_header('Content-Length',str(len(data)))
        handler.send_header('X-Content-Type-Options','nosniff');handler.send_header('Cache-Control','no-cache');handler.end_headers();handler.wfile.write(data)

    @web.route('GET','/welcome')
    def home(handler,req):
        from .errors import CashflowError
        values=req.query.get('example',['baseline'])
        if len(values)!=1 or values[0] not in {'baseline','delay'}:raise CashflowError('Choose one of the displayed synthetic examples.')
        return handler._send_html(200,public_page('Cashflow Action Copilot',home_body(web,values[0]),req.path))

    @web.route('GET','/product')
    def product(handler,req):
        title,heading,lead,features=PUBLIC['/product']
        b='<div class="page-hero"><div class="eyebrow">The Fluxora workspace</div><h1>'+heading+'</h1><p>'+lead+'</p></div><div class="site-feature-grid">'+''.join(feature(*f) for f in features)+'</div>'+brand_story()+cta_panel()
        return handler._send_html(200,public_page(title,b,req.path))

    @web.route('GET','/how-it-works')
    def how(handler,req):
        steps=[('Start with the recorded position','Review cash accounts, invoices, obligations and their evidence. Missing facts stay visible instead of becoming invented values.'),('Look ahead with local calculations','Forecast cash, check payroll account funding, and test scenarios. Each currency and conditional assumption remains distinct.'),('Prepare a practical response','Use direct forms or the guided agent to prepare a DRAFT or pending workflow. Review the exact task before it is prepared.'),('Keep the owner decision explicit','The owner reviews current terms. Approval does not supply external acceptance or establish that a payment occurred.'),('Observe, match and reconcile','Record source-backed synthetic facts once. Reconcile them separately and compare the forecast with what was observed.')]
        b='<div class="page-hero"><div class="eyebrow">From evidence to reviewed action</div><h1>A clear path.<br>At every step.</h1><p>Know what is recorded, what is proposed, and what still needs a decision.</p></div>'+''.join('<section class="process-step"><div class="process-number">0'+str(i)+'</div><div><h2>'+h+'</h2><p>'+p+'</p></div></section>' for i,(h,p) in enumerate(steps,1))+'<div class="site-callout">The model assists with supported tasks. It never approves itself, authenticates source documents, invents receipts, or executes real payments.</div>'+brand_story('evidence')+cta_panel()
        return handler._send_html(200,public_page('How it works',b,req.path))

    @web.route('GET','/trust')
    def trust(handler,req):
        items=[('Local by default, optional hosted demo','The default CLI uses a local SQLite database and loopback listener. A hosted demo stores its synthetic records on its server. Authenticated mode requires a shared owner credential; optional public mode separates visitor browser sessions into expiring synthetic workspaces without login. Direct tools and reviewed forms need no model.'),('Optional model, visible destination','Guided tasks preview the question and static catalog before sending them to the configured provider. Questions can themselves contain sensitive information.'),('Financial results from code','The deterministic engine computes financial results. Guided answers are rendered by the server; advanced-agent commentary remains unverified.'),('Decisions stay with the owner','The model prepares DRAFT/pending work only. Approval, acceptance, observed cash and reconciliation remain separate.'),('Evidence is traceable, not authenticated','Record IDs and source references let you inspect the inputs. They do not prove that a source is true, complete or authorized.'),('A demonstration with clear limits','This release has synthetic data, no production login, no real bank connection, and no counterparty message or payment execution. The optional private Telegram companion uses separate opt-in delivery. Provider uptime and universal accuracy are not guaranteed.')]
        b='<div class="page-hero"><div class="eyebrow">Trust & limitations</div><h1>Clarity includes<br>knowing the boundaries.</h1><p>Understand what Fluxora does, what it relies on, and what still needs your judgment.</p></div><div class="trust-list">'+''.join('<section class="card"><h2>'+h+'</h2><p>'+p+'</p></section>' for h,p in items)+'</div><p><a href="/docs/security">Read the security boundaries →</a></p>'+cta_panel()
        return handler._send_html(200,public_page('Trust & limitations',b,req.path))

    @web.route('GET','/about')
    def about(handler,req):
        b='<div class="page-hero"><div class="eyebrow">About Fluxora</div><h1>Better visibility.<br>More deliberate decisions.</h1><p>Cashflow Action Copilot for small and medium businesses.</p></div><section class="feature-split"><div><h2>The problem we are working on</h2><p>Profitable work can still leave a business short of cash when collections and obligations fall on different dates. Fluxora brings the timing, evidence and possible responses into a local, reviewable workflow.</p><p>The project was developed for the 2026 Shenzhen International FinTech Competition, Topic E. This describes its intended project context; it is not a claim of selection, eligibility, endorsement or an award.</p></div><div class="card"><div class="eyebrow">Our specific contribution</div><h2>Connect analysis to controlled action.</h2><p>Combine deterministic forecasting and plan comparison with evidence-linked records and constrained task preparation. Keep owner decisions and external financial facts explicit.</p><p>This is a synthetic demonstration with local and optional hosted modes. General financial accuracy, production security, fresh human usability feedback and brand clearance are not claimed.</p><a href="/docs">Explore the documentation →</a></div></section>'+cta_panel()
        return handler._send_html(200,public_page('About',b,req.path))

    @web.route('GET','/docs')
    def docs_home(handler,req):return handler._send_html(200,public_page('Documentation',docs_page('getting-started'),req.path))

    @web.route('GET','/docs/<slug>')
    def docs(handler,req,slug):
        if slug not in DOCS:return handler._send_html(404,public_page('Page not found','<div class="page-hero"><h1>Documentation page not found.</h1><p><a href="/docs">Back to documentation</a></p></div>',req.path))
        return handler._send_html(200,public_page(DOCS[slug][0],docs_page(slug),req.path))

    @web.route('GET','/app')
    @web.route('GET','/')
    def overview(handler,req):
        return handler._send_html(200,web.read_state(lambda l:web.page('Overview',dashboard(web,l),active='/')))

    @web.route('GET','/forecast')
    def forecast_page(handler,req):
        from .forecast import forecast
        def build(l):
            f=forecast(l,l.as_of,l.horizon_days)
            b='<div class="eyebrow">Cash planning</div><h1>Forecast</h1><p>Recorded snapshot '+str(l.as_of)+' · '+str(l.horizon_days)+' days. Expected collections are conditional. Currencies remain separate.</p><p><a class="btn" href="/scenarios">Test a scenario</a> <a class="btn" href="/weekly-cashflow">Thirteen-week report</a></p>'
            for c,cf in f.currencies.items():
                b+='<section class="card"><h2>'+esc(c)+' · '+str(l.horizon_days)+'-day outlook</h2><div class="chart-meta"><div>Lowest / date<b>'+web.money(cf.min_balance_minor,c)+'</b>'+str(cf.min_balance_date)+'</div><div>Cash floor<b>'+web.money(cf.floor_minor,c)+'</b></div><div>First floor breach<b>'+str(cf.first_breach_date or 'None under assumptions')+'</b></div><div>Worst shortfall<b>'+web.money(cf.worst_shortfall_minor,c)+'</b></div></div><div class="chart-frame">'+web.forecast_svg(cf)+'</div><details><summary>Daily forecast data</summary>'+web.forecast_table(cf)+'</details></section>'
            b+='<section class="card"><h2>Assumptions</h2><ul>'+''.join('<li>'+esc(a)+'</li>' for a in f.assumptions)+'</ul><h2>Missing facts</h2>'+(''.join('<p>'+esc(c['question'])+' — '+esc(c['why_it_matters'])+'</p>' for c in f.clarifications) or '<p>No clarification returned by these saved-input checks.</p>')+'<p><a href="/ledger">Inspect source records</a> · <a href="/agent/tasks/report/forecast">Open evidence-linked tool result</a></p></section>'
            return web.page('Forecast',b,active='/forecast')
        return handler._send_html(200,web.read_state(build))

    @web.route('GET','/workspace/features')
    def features(handler,req):
        from .workspace_ui import profile,FAMILIES
        total=sum(1+len(children) for _,rows in ui.GROUPS for _,_,_,children in rows)
        b='<h1>All workspace features</h1><div class="directory-controls"><div><b>'+str(total)+' dedicated workspace pages</b>Browse by the job you need to do.</div><label hidden>Find a workspace page<input type="search" data-feature-filter placeholder="Try payroll, receipt or forecast" autocomplete="off"></label></div><p class="directory-status" data-directory-status role="status" hidden></p><p class="directory-empty" data-directory-empty hidden>No page matches that search. Try a task, record type or shorter word.</p>'
        for group,rows in ui.GROUPS:
            count=sum(1+len(children) for _,_,_,children in rows)
            b+='<section class="directory-group"><header><h2>'+group+'</h2><span>'+str(count)+' pages</span></header><div class="directory-grid">'
            for url,label,glyph,children in rows:
                for path,title in ((url,label),)+children:
                    purpose,family,_=profile(path)
                    b+='<a class="feature-tile" href="'+path+'"><span>'+ui.icon(glyph)+'<span class="tile-family">'+esc(FAMILIES[family][0])+'</span></span><h3>'+esc(title)+'</h3><p>'+esc(purpose)+'</p><span class="tile-open">Open workspace page '+ui.icon('arrow')+'</span></a>'
            b+='</div></section>'
        return handler._send_html(200,web.page('All workspace features',b))
