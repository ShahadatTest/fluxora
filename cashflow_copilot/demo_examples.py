"""Named synthetic examples. Never mutate an existing visitor's saved data."""
from .fixture import load_fixture,validate_fixture
from .ledger import Ledger
from .money import format_amount

EXAMPLES={
    'atlas':('Atlas Agency','Baseline service business; one customer collection and protected payroll.',1000000,800000,600000,450000,150000),
    'studio':('Beacon Studio','Larger opening cash and invoice; compare timing without pooling currencies.',1800000,1200000,800000,550000,200000),
    'retail':('Cedar Retail','Tighter cash position; inspect a projected floor breach honestly.',700000,500000,550000,400000,180000),
}


def seed(name='atlas'):
    if name not in EXAMPLES:raise ValueError('Choose a displayed synthetic example.')
    if name=='atlas':return Ledger.seed()
    label,_,opening,invoice,payroll,supplier,ops=EXAMPLES[name]
    data=load_fixture()
    data['business'].update(id='demo-'+name,name='Demo '+label)
    data['accounts'][0]['opening_minor']=opening
    data['receivables'][0]['original_minor']=invoice
    amounts={'payroll-001':payroll,'supplier-001':supplier,'ops-001':ops}
    for out in data['outflows']:out['amount_minor']=amounts[out['id']]
    summaries={
        'ev-client-invoice':format_amount(invoice,'USD')+' outstanding, originally expected 20 October.',
        'ev-payroll':format_amount(payroll,'USD')+' due 23 October. Protected hard date.',
        'ev-supplier-invoice':format_amount(supplier,'USD')+' due 25 October. No extension accepted yet.',
        'ev-ops-budget':format_amount(ops,'USD')+' due 28 October. Up to USD 500 may move to 6 November after approval.',
    }
    for evidence in data['evidence']:
        if evidence['id'] in summaries:evidence['summary']='Synthetic '+label+' example: '+summaries[evidence['id']]
    return Ledger.from_fixture(validate_fixture(data))


def render(web,selected='atlas',*,local=False):
    from .forecast import forecast
    from .site_web import public_page
    if selected not in EXAMPLES:raise ValueError('Choose a displayed synthetic example.')
    ledger=seed(selected);result=forecast(ledger,ledger.as_of,ledger.horizon_days)
    choices=''.join('<a class="btn" href="/demo/examples?example='+key+'"'+(' aria-current="page"' if key==selected else '')+'>'+web.esc(row[0])+'</a>' for key,row in EXAMPLES.items())
    body='<div class="page-hero"><div class="eyebrow">Synthetic examples · read only</div><h1>Explore a different cash position.</h1><p>Choose a business example. These previews use no visitor slot and never change your saved workspace or Telegram link.</p></div><nav class="row" aria-label="Demo examples">'+choices+'</nav>'
    body+='<section class="card"><h2>'+web.esc(ledger.business_name)+'</h2><p>'+web.esc(EXAMPLES[selected][1])+'</p><p>Snapshot '+str(ledger.as_of)+' · '+str(ledger.horizon_days)+' days · expected collections are conditional.</p><div class="grid">'
    for ccy,curve in result.currencies.items():
        body+='<article class="card"><h3>'+ccy+'</h3><p>Recorded opening: <b>'+web.money(ledger.actual_cash(ccy),ccy)+'</b></p><p>Projected minimum: <b>'+web.money(curve.min_balance_minor,ccy)+'</b> on '+str(curve.min_balance_date)+'</p><p>Worst gap to floor: '+web.money(curve.worst_shortfall_minor,ccy)+'</p><p>First floor breach: '+str(curve.first_breach_date or 'none under recorded assumptions')+'</p>'+web.forecast_svg(curve,width=600,height=210)+'</article>'
    body+='</div><details><summary>Inspect example inputs and sources</summary><ul>'
    for evidence in ledger.evidence.values():body+='<li>'+web.esc(evidence.id)+': '+web.esc(evidence.summary)+'</li>'
    note=('Your persistent local workspace opens unchanged. To seed another example, launch with --example '+selected+' and a new database path. A browser profile does not create another local business.' if local else 'A new browser session starts with the selected example. If this browser already has a workspace, that workspace opens unchanged. To use another example without replacing it, use a separate browser profile.')
    target='/app' if local else '/app?demo_example='+selected
    body+='</ul></details></section><section class="card"><h2>Try it in a workspace</h2><p>'+note+' Reloading never randomises saved data. Original demo reset restores Atlas.</p><a class="btn primary" href="'+target+'">Open workspace</a><p>Currencies stay separate. No receipt, consent, payment or real business message is created.</p></section>'
    return public_page('Demo examples',body,'/demo/examples')
