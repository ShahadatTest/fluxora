"""Visitor-owned pairing UI. No bot/model secret, Telegram ID or cookie exposed."""
from .errors import CashflowError


def mount(web):
    from . import telegram_demo
    telegram_demo.mount(web)
    def service(handler):
        bridge=getattr(handler.server,'telegram',None)
        if bridge is None:raise CashflowError('Telegram is not configured on this server. No bot call was made.')
        return bridge

    def fields(req,expected):
        if set(req.form)!=expected|{'csrf'} or any(len(v)!=1 for v in req.form.values()):raise CashflowError('Use the current Telegram settings form.')

    @web.route('GET','/telegram')
    def page(handler,req):
        bridge=getattr(handler.server,'telegram',None)
        local=bool(getattr(getattr(handler.server,'visitors',None),'durable',False))
        body='<h1>Telegram companion</h1><p>Private owner chat · synthetic workspace only. Read reports, review DRAFTs and approve exact forecast snapshots. No real payments or counterparty messages.</p>'
        body+='<section class="card"><h2>Automatic monitoring &amp; preparation</h2><p><a href="/autopilot">Open Autopilot</a> to choose scheduled checks, conditional DRAFT strategy, per-currency preparation limits and private Telegram updates. Final approval stays separate. In your linked bot, /autopilot on resumes the saved policy and opts into its notifications; /autopilot off stops future preparations and queued Autopilot updates.</p><p>Autopilot can offer exact DRAFT approval/rejection and forecast-snapshot decisions in Telegram. Observations, consent and other workflow changes still need their detailed source-backed Web review.</p></section>'
        body+='<section class="card"><h2>Try it without Telegram access</h2><p>Judges and visitors can try a separate disposable practice chat. Real deterministic financial tools; simulated transport and no live AI. Your main workspace and the private bot are unchanged.</p><a class="btn primary" href="/telegram/demo">Open Telegram interactive demo</a></section>'
        if getattr(handler.server,'local_connections',None) is not None:body+='<p><a class="btn primary" href="/settings/connections">Configure API profiles and Telegram here</a></p>'
        if bridge is None:body+='<section class="card"><h2>Not connected</h2><p>The server operator must configure a bot and one allowed Telegram owner account privately. No Telegram or live model call has been made by this page.</p></section>'
        else:
            state=bridge.state(web.database_path())
            body+='<section class="card"><h2>'+('Linked to this workspace' if state['linked'] else 'Link your own workspace')+'</h2><p>Bot transport: '+web.esc(state['connection'])+'. Model: '+web.esc(state['model'])+'.</p>'
            if bridge.config.bot_name:body+='<p>Bot: <a href="https://t.me/'+web.esc(bridge.config.bot_name)+'" rel="noreferrer">@'+web.esc(bridge.config.bot_name)+'</a></p>'
            body+='<p>Only the configured private owner account can redeem a code or act. This does not give other public visitors Telegram permissions. Never enter another person’s pairing code.</p>'
            if state['linked']:
                body+='<p>Alerts: '+('on' if state['alerts'] else 'off')+'. Uncertain deliveries: '+str(state['uncertain_deliveries'])+'. Use /status in Telegram or Web history if a send is uncertain; it is not retried automatically.</p>'
                body+=f"<form method='post' action='/telegram/disconnect'>{web.csrf_field()}<button>Disconnect and revoke pending buttons</button></form>"
            body+=f"<form method='post' action='/telegram/pair'>{web.csrf_field()}<label><input type='checkbox' name='confirmed' value='yes' required> I will send this code only to my own configured bot from my allowed private account. Commands/replies and enabled alerts leave this workspace through Telegram. Natural-language questions go to the configured model only after a private model check; with Autopilot OFF, interpretation review applies; while ON, validated report/DRAFT preparation is direct and final approval remains separate.</label><button>Create one-time pairing code</button></form></section>"
        lifetime=('This local workspace is persistent on this computer; there is no 24-hour visitor expiry. Keep the app running for Telegram replies. Back up its database and recovery folder before moving computers.' if local else 'Pairing does not extend the browser workspace’s 24-hour lifetime. Clearing cookies loses browser access.')
        body+='<section class="card"><h2>What stays separate</h2><p>'+lifetime+' Only one workspace can be linked to the configured owner at a time; linking another revokes old buttons. Reports and preparation are not owner approval. Telegram snapshot approval is not evidence of payment or counterparty acceptance. Other changes require detailed Web review.</p><p>Local commands: /forecast, /plans, /draft INVOICE_ID USD 100 YYYY-MM-DD, /snapshot LABEL, /status, /alerts on, /alerts off, /unlink. Financial alerts are off until you opt in. A natural-language task is translated only when the private model is configured and qualified. Unsupported instructions do not run arbitrary commands.</p></section>'
        return handler._send_html(200,web.page('Telegram companion',body,active='/telegram'))

    @web.route('POST','/telegram/pair')
    def pair(handler,req):
        fields(req,{'confirmed'})
        if req.field('confirmed')!='yes':raise CashflowError('Confirm the private pairing/data destination.')
        code=service(handler).pairing(web.database_path())
        body='<h1>Link your private Telegram chat</h1><p>Open your configured bot from the allowed private Telegram account and send:</p><pre>/link '+code+'</pre><p>This code expires in five minutes, is single-use, and is only shown in this response. Do not share it or use a code from another browser workspace. Alerts start OFF. <a href="/telegram">Check connection</a></p>'
        return handler._send_html(200,web.page('Private Telegram pairing',body,active='/telegram'))

    @web.route('POST','/telegram/disconnect')
    def disconnect(handler,req):
        fields(req,set());service(handler).disconnect(web.database_path())
        return handler._redirect('/telegram')
