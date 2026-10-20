"""Local-only settings forms. No secrets in returned HTML, URLs or history."""
from .errors import CashflowError
from .local_connections import PRESETS


def mount(web):
    def manager(handler):
        current=getattr(handler.server,'local_connections',None)
        if current is None:raise CashflowError('Web connection settings are available only in the private local launcher.')
        return current
    def form(req,allowed):
        if req.query or req.files or len(req.body)>16384 or set(req.form)-set(allowed)-{'csrf'} or any(len(v)!=1 for v in req.form.values()):
            raise CashflowError('Use the current connection settings form.')
    def confirm(req):
        if req.field('confirmed')!='yes':raise CashflowError('Review the destination and effect, then tick the confirmation.')
    def post(action,label,ident='',warning=''):
        return "<form method='post' action='/settings/connections/"+action+"'>"+web.csrf_field()+"<input type='hidden' name='id' value='"+web.esc(ident)+"'>"+("<label><input type='checkbox' name='confirmed' value='yes' required> "+web.esc(warning)+"</label>" if warning else '')+"<button>"+web.esc(label)+"</button></form>"
    def remember(m,checked=False):
        if not m.vault.available:return '<p>Session only: encrypted Remember is unavailable on this OS. Nothing is saved in plaintext.</p>'
        return "<label><input type='checkbox' name='remember' value='yes'"+(' checked' if checked else '')+"> Remember encrypted for this Windows account. OS account/admin access remains trusted; do not include the vault in a submission.</label>"

    @web.route('GET','/settings/connections')
    def page(handler,req):
        m=manager(handler)
        if req.query:raise CashflowError('Open the settings link without query parameters.')
        status=m.active_adapter().status()
        body='<h1>API & Telegram connections</h1><p>Private local settings. Credentials stay server-side; saved keys are never filled back into a form. Saving makes no provider call. Connection checks and live tasks contact your selected provider and may incur charges.</p>'
        body+='<section class="card"><h2>Active AI connection</h2><p>State: <b>'+web.esc(status.state)+'</b>. Activation, edits and restart start unverified. Qualification remains a separate synthetic test, not a guarantee of correct prose.</p><p>Selected: '+web.esc(m.data['active'] if m.data['active'] in {'environment','off'} else next((p['label'] for p in m.public() if p['id']==m.data['active']),'profile'))+'</p>'
        body+=post('activate','Use environment configuration','environment')+post('activate','Disconnect AI','off')+'<p><a href="/agent/tasks">Open guided Agent</a> · <a href="/agent">Connection and qualification</a></p></section>'
        if m.load_error:body+='<p class="err">Saved settings could not be decrypted/validated. Session-only profiles remain available; saved settings will not be overwritten until you explicitly forget them.</p>'
        body+='<section class="card"><h2>Provider profiles</h2><p>Up to 12 named API/model profiles. Exactly one active connection; no automatic fallback or parallel billing across profiles. The model must support the existing chat-completions/tool contract. Native Anthropic/other protocols are not direct adapters here; models on OpenRouter can use its compatible gateway.</p>'
        for p in m.public():
            body+='<article class="card"><h3>'+web.esc(p['label'])+'</h3><p>'+web.esc(p['provider'])+' · '+web.esc(p['base_url'])+' · '+web.esc(p['model'])+'</p><p>Key: '+('not needed by local endpoint' if p['no_key'] else 'stored, hidden')+' · '+('remembered encrypted' if p['remember'] else 'session only')+' · '+web.esc(p['state'])+'</p>'
            body+=post('activate','Activate profile',p['id'])
            if p['id']==m.data['active']:body+=post('verify','Test active connection',p['id'],'Contact this displayed API endpoint/model using its key. Provider charges may apply; no financial task will run.')
            body+=post('remove','Delete profile',p['id'],'Remove this local profile and its remembered copy. This does not revoke the provider key.')+'</article>'
        body+='</section><section class="card"><h2>Add or edit API profile</h2><form method="post" action="/settings/connections/profile">'+web.csrf_field()
        body+='<label>Edit existing (blank = new)<select name="id"><option value="">New profile</option>'+''.join('<option value="'+p['id']+'">'+web.esc(p['label'])+'</option>' for p in m.public())+'</select></label>'
        body+='<label>Profile label<input name="label" maxlength="64" required></label><label>Provider<select name="provider">'+''.join('<option value="'+k+'">'+web.esc(v[0])+'</option>' for k,v in PRESETS.items())+'</select></label>'
        body+='<p>Preset endpoints: '+', '.join(web.esc(v[0])+': '+web.esc(v[1]) for k,v in PRESETS.items() if v[1])+'. No fixed model is selected for you.</p>'
        body+='<label>Custom API base URL (only for Custom)<input type="url" name="base_url" placeholder="https://your-compatible-provider.example/v1"></label><label>Exact model ID<input name="model" maxlength="160" required></label><label>API key<input type="password" name="key" autocomplete="new-password" maxlength="4096" placeholder="New key; blank keeps the key when editing"></label><label><input type="checkbox" name="no_key" value="yes"> This loopback endpoint needs no key (loopback only; a non-secret placeholder header is used).</label>'+remember(m)+'<button>Save profile (no API call)</button></form></section>'
        tg=m.data['telegram'];running=handler.server.telegram is not None
        body+='<section class="card"><h2>Telegram bot</h2><p>Status: '+('local worker active ('+web.esc(handler.server.telegram.connection)+')' if running else 'stopped')+'. Saved token: '+('present, hidden' if tg else 'not configured')+'. Remembered settings never start a poller automatically after restart.</p><p>Use a separate local bot if your VPS bot is already polling. These controls affect only this local app; they do not stop a VPS poller or remove a webhook.</p>'
        if running:body+=post('bot-stop','Stop local bot',warning='Stop this local worker; in-flight requests must finish first.')
        else:
            body+='<form method="post" action="/settings/connections/bot-save">'+web.csrf_field()+'<label>BotFather token<input type="password" name="token" autocomplete="new-password" maxlength="200" placeholder="Blank keeps an existing saved token"></label><label>Allowed private numeric user ID<input name="owner" inputmode="numeric" placeholder="Blank keeps an existing owner ID"></label><label>Bot username (no @)<input name="name" maxlength="32" placeholder="Blank keeps an existing username"></label><label><input type="checkbox" name="use_model" value="yes"> Use the active AI profile for Telegram translation; separate qualification is required.</label>'+remember(m,bool(tg and tg['remember']))+'<button>Save bot (no network call)</button></form>'
            if tg:
                body+=post('bot-test','Test bot identity',warning='Contact Telegram getMe using the saved token. This does not start polling or send a message.')
                body+=post('bot-start','Start local bot',warning='No other poller/webhook is using this token. Start outbound Telegram polling for my configured private account; my app/computer must stay on.')
                body+=post('bot-forget','Forget local bot credentials',warning='Remove the local token/configuration; this does not revoke the bot token with Telegram.')
        body+='<p>Changing the bot token, owner, username or model mode requires a new pairing and cancels old pending bot reviews/deliveries. Changing only Remember preserves the pairing.</p><p><a href="/telegram">Pair this local workspace and review bot permissions</a></p></section>'
        body+='<section class="card"><h2>Saved settings</h2><p>Default is session memory. Optional Windows storage uses current-user DPAPI, separately from financial data and audit exports. Same-account software/admin access is outside that protection. Remembered credentials are not portable backup material.</p>'+post('forget-saved','Forget all remembered settings',warning='Delete the encrypted local saved copy. Current session settings stay in memory until the app stops; provider keys are not revoked.')+'</section>'
        return handler._send_html(200,web.page('API & Telegram connections',body,active='/settings/connections'))

    @web.route('POST','/settings/connections/profile')
    def profile(handler,req):
        m=manager(handler);form(req,{'id','label','provider','base_url','model','key','remember','no_key'})
        m.save_profile(ident=req.field('id'),label=req.field('label'),provider=req.field('provider'),base_url=req.field('base_url'),model=req.field('model'),key=req.field('key'),remember=req.field('remember')=='yes',no_key=req.field('no_key')=='yes')
        return handler._redirect('/settings/connections')

    @web.route('POST','/settings/connections/activate')
    def activate(handler,req):
        m=manager(handler);form(req,{'id'});m.activate(req.field('id'));return handler._redirect('/settings/connections')

    @web.route('POST','/settings/connections/verify')
    def verify(handler,req):
        m=manager(handler);form(req,{'id','confirmed'});confirm(req)
        state=m.verify(req.field('id'))
        return handler._send_html(200,web.page('Connection check','<h1>Connection check</h1><p>State: '+web.esc(state)+'. A successful connection check does not certify financial answers or model accuracy.</p><p><a href="/settings/connections">Back to connections</a></p>',active='/settings/connections'))

    @web.route('POST','/settings/connections/remove')
    def remove(handler,req):
        m=manager(handler);form(req,{'id','confirmed'});confirm(req);m.remove(req.field('id'));return handler._redirect('/settings/connections')

    @web.route('POST','/settings/connections/bot-save')
    def bot_save(handler,req):
        m=manager(handler);form(req,{'token','owner','name','use_model','remember'})
        m.save_bot(token=req.field('token'),owner=req.field('owner'),name=req.field('name').lstrip('@'),use_model=req.field('use_model')=='yes',remember=req.field('remember')=='yes');return handler._redirect('/settings/connections')

    @web.route('POST','/settings/connections/<operation>')
    def operation(handler,req,operation):
        m=manager(handler);form(req,{'id','confirmed'});confirm(req)
        functions={'bot-test':m.test_bot,'bot-start':m.start_bot,'bot-stop':m.stop_bot,'bot-forget':m.forget_bot,'forget-saved':m.forget_saved}
        if operation not in functions:raise CashflowError('Use a displayed settings action.')
        functions[operation]()
        if operation=='bot-test':
            return handler._send_html(200,web.page('Bot identity checked','<h1>Bot identity checked</h1><p>Telegram accepted the configured bot identity. No poller was started and no message was sent.</p><a href="/settings/connections">Back to connections</a>',active='/settings/connections'))
        return handler._redirect('/settings/connections')
