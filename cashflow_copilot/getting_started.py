"""Code-owned navigation guidance, never task completion or authorization."""
from html import escape
import json


GUIDES={
    'judge':dict(title='Five-minute judge tour',description='Suggested demo timing, not a competition requirement. Public model-free features plus a separate practice ledger.',steps=[
        dict(title='Understand the recorded position',path='/app',time='45 sec',text='Find the business snapshot, recorded USD and EUR cash, and the lowest forecast. Expected collections are conditional; recorded cash is not a live bank balance.',check='Explain the snapshot date and why currencies stay separate.'),
        dict(title='Trace the cash risk',path='/forecast',time='45 sec',text='Inspect the currency-specific cash floor, daily low and source references. A forecast can show no breach under the current assumptions; do not invent a gap.',check='Name one assumption and follow a supporting source.'),
        dict(title='Explore a hypothetical change',path='/scenarios',time='60 sec',text='Preview an invoice delay or partial collection using the displayed form. Inspect the before/after results and feasibility. Previewing a scenario is separate from recording a payment.',check='Explain what changed in the scenario and what remains hypothetical.'),
        dict(title='Prepare a DRAFT in isolated practice',path='/telegram/demo',time='75 sec',text='In this separate Atlas lesson, press Prepare sample DRAFT. Inspect the exact invoice, currency, amount and date, then choose Create practice DRAFT. Supported commands and transport are simulated; no live AI interprets this request.',check='Find the practice DRAFT. It has no owner approval, acceptance, receipt or payment.'),
        dict(title='Inspect a separate owner decision',path='/telegram/demo',time='75 sec',text='In the same lesson, press Request snapshot, review the terms and choose Prepare pending snapshot. Inspect the separate owner review before approving or rejecting that exact practice snapshot. This does not approve the earlier DRAFT.',check='Distinguish a DRAFT, owner approval, counterparty acceptance, observed cash and reconciliation. Practice snapshot approval is not a payment.')]),
    'owner':dict(title='Explore as a business owner',description='Learn the current synthetic workspace. These guide steps do not import, reset, approve or change records.',steps=[
        dict(title='Start with the evidence',path='/ledger',time='Read',text='Inspect the existing sources, invoice and obligation identifiers. Stage an import only when you want to review its explicit confirmation. A document supplies evidence, never authorization.',check='Find the source supporting one recorded amount.'),
        dict(title='Check missing or stale facts',path='/freshness',time='Check',text='Review missing dates, restrictions and stale records. Resolve facts through their source-backed forms only when you have the required evidence.',check='Identify uncertainty before treating the forecast as dependable.'),
        dict(title='Read the cash outlook',path='/forecast',time='Inspect',text='Compare the daily low with each currency cash floor. Inspect protected payroll and account availability. Display FX does not provide spendable cash.',check='Explain recorded cash versus conditional future collections.'),
        dict(title='Compare an option',path='/scenarios',time='Preview',text='Use the existing preview to inspect a hypothetical timing or collection change. Inspect no-feasible-plan outcomes honestly and keep a proposal separate from an agreed change.',check='State the assumptions and any consent required for the option.'),
        dict(title='Review the action lifecycle',path='/actions',time='Review',text='Inspect DRAFTs and their versioned owner controls. Existing reviewed forms remain the authority. Keep approval, acceptance, observed receipts and reconciliation distinct. A blank history can be normal for a new workspace.',check='Locate the exact review boundary. Do not invent a receipt or acceptance to finish a guide.')])
}


def target(mode,index):
    return GUIDES[mode]['steps'][index]['path']+'#tour-'+mode+'-'+str(index+1)


def entry():
    from .ui import icon
    return '<aside class="start-callout" aria-label="Getting started">'+icon('book')+'<div><b>A clearer place to start.</b><p>Choose a short judge tour or learn the owner workflow.</p></div><a class="btn" href="/start">Get started '+icon('arrow')+'</a></aside>'


def panel():
    data=json.dumps(GUIDES).replace('<','\\u003c')
    return '''<aside class="guided-rail" data-guided-rail hidden aria-label="Workspace walkthrough">
<div class="guided-top"><span data-guide-name></span><button type="button" data-guide-exit>Exit guide</button></div>
<p class="guided-progress" data-guide-progress aria-live="polite" aria-atomic="true"></p><h2 data-guide-title tabindex="-1"></h2><p data-guide-instruction></p><p class="guided-check" data-guide-check></p>
<nav aria-label="Walkthrough navigation"><a class="btn" data-guide-open>Return to this step</a><a class="btn" data-guide-prev>Previous guide step</a><a class="btn primary" data-guide-next>Next guide step</a><a href="/start">All guide instructions</a></nav>
<p class="guided-disclaimer">Guide navigation only · not proof of task completion. Each financial change needs its existing review. No request runs automatically.</p>
</aside><script type="application/json" id="fluxora-guide-data">'''+data+'</script>'


def body():
    from .ui import icon
    html='<section class="page-hero start-hero"><div class="eyebrow">Getting started</div><h1>Your next step, made clear.</h1><p>Choose a short path through Fluxora. Direct reports work without a model. The public demo uses synthetic records.</p></section><div class="start-choices">'
    for mode,guide in GUIDES.items():
        html+='<article class="start-choice"><span class="start-choice-icon">'+icon('clock' if mode=='judge' else 'wallet')+'</span><h2>'+escape(guide['title'])+'</h2><p>'+escape(guide['description'])+'</p><a class="btn primary" href="'+target(mode,0)+'">Start this guide '+icon('arrow')+'</a><a href="#path-'+mode+'">Read all five steps</a></article>'
    html+='</div><section class="start-boundary"><b>Know which data you are using.</b><p>The first three judge steps inspect your current synthetic workspace. The last two use a separate, disposable Atlas practice ledger. Practice changes do not appear in the main workspace. The simulator does not demonstrate live-model language quality or real Telegram delivery.</p><p>Opening a guide never resets data, runs a model or completes a financial step. Guide position is an optional browser-session preference, not a saved task result.</p></section>'
    for mode,guide in GUIDES.items():
        html+='<section class="start-path" id="path-'+mode+'"><header><div class="eyebrow">Five clear steps</div><h2>'+escape(guide['title'])+'</h2></header><ol>'
        for i,step in enumerate(guide['steps']):
            html+='<li><div class="start-step-number" aria-hidden="true">'+str(i+1).zfill(2)+'</div><div><span class="start-step-time">'+escape(step['time'])+'</span><h3>'+escape(step['title'])+'</h3><p>'+escape(step['text'])+'</p><p class="start-step-check"><b>Check your understanding:</b> '+escape(step['check'])+'</p><a href="'+target(mode,i)+'">Open this step '+icon('arrow')+'</a></div></li>'
        html+='</ol></section>'
    return html+'''<section class="start-resources"><h2>Continue with confidence.</h2><div><a href="/docs/local-setup">Private local installation</a><a href="/docs/guided-agent">Optional model setup and Agent guide</a><a href="/workspace/features">Browse all workspace features</a><a href="/usability">Independent user walkthrough tasks</a></div></section><noscript><p class="article-note">JavaScript is off: all guide instructions and destination pages remain available. Use browser Back to return to this guide. The in-page reminder and session preference are optional.</p></noscript>'''


def mount(web):
    @web.route('GET','/start')
    def start(handler,req):
        from .site_web import public_page
        if req.query:
            return handler._send_html(400,public_page('Getting started','<div class="page-hero"><h1>Open the guide directly.</h1><p>This guide uses displayed links, without query parameters.</p><a class="btn primary" href="/start">Open getting started</a></div>','/start'))
        return handler._send_html(200,public_page('Getting started',body(),'/start'))
