"""Code-owned hosted settings preview. No configuration, storage or API reads."""
from . import ui


def render():
    body='''<h1>API &amp; Telegram connections</h1>
<p>Configure your connections in a private local installation. This hosted demo shows the settings layout only.</p>
<section class="card"><div class="eyebrow">DEMO PREVIEW · LOCKED</div><h2>Private settings stay private</h2>
<p>Keys, tokens, provider/model selections, endpoints, owner identifiers and connection status are not displayed here. All controls are disabled. This page cannot save settings, test a provider, start a bot or make a model call.</p></section>
<div class="connection-preview-grid">
<section class="card"><h2>AI provider profiles</h2><p>Local installations support named profiles with one active connection. OpenRouter and other OpenAI-compatible endpoints can be configured privately.</p>
<fieldset disabled><legend>Configuration hidden in hosted demo</legend>
<label>Provider<select disabled><option>Private provider selection</option></select></label>
<label>Model ID<input disabled placeholder="Not displayed in this demo"></label>
<label>API base URL<input disabled placeholder="Not displayed in this demo"></label>
<label>API key<input type="password" disabled autocomplete="off" placeholder="Hidden — no credential is loaded"></label>
<div class="toolbar"><button type="button" disabled>Save profile</button><button type="button" disabled>Activate</button><button type="button" disabled>Test connection</button></div></fieldset></section>
<section class="card"><h2>Telegram companion</h2><p>Private local setup uses a bot token, an allowed private owner and workspace pairing. The hosted operator's configuration is never loaded into this preview.</p>
<fieldset disabled><legend>Configuration hidden in hosted demo</legend>
<label>Bot token<input type="password" disabled autocomplete="off" placeholder="Hidden — no credential is loaded"></label>
<label>Bot username<input disabled placeholder="Not displayed in this demo"></label>
<label>Allowed private owner ID<input disabled placeholder="Not displayed in this demo"></label>
<label><input type="checkbox" disabled> Use the active AI profile</label>
<div class="toolbar"><button type="button" disabled>Save bot</button><button type="button" disabled>Test identity</button><button type="button" disabled>Start bot</button><button type="button" disabled>Stop bot</button></div></fieldset></section></div>
<section class="card"><h2>Run it privately on your computer</h2><p>The complete source includes the working local settings. Save credentials in session memory, or explicitly opt into Windows current-account encrypted storage. A model still needs separate verification and qualification; proposals still require the existing owner review.</p><p><a class="btn" href="/docs/local-setup">Read local setup</a> <a class="btn" href="/docs/security">Security boundaries</a></p></section>'''
    css='.connection-preview-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.connection-preview-grid .card{min-width:0}.connection-preview-grid fieldset{min-width:0;border:1px solid var(--line,#dce4e1);border-radius:12px;padding:18px}.connection-preview-grid legend{font-size:.85rem;padding:0 6px}.connection-preview-grid input,.connection-preview-grid select{max-width:100%;box-sizing:border-box}.connection-preview-grid :disabled{cursor:not-allowed;opacity:.72}.connection-preview-grid .toolbar{display:flex;flex-wrap:wrap;gap:8px}@media(max-width:800px){.connection-preview-grid{grid-template-columns:1fr}}'
    return ui.app_page('API & Telegram',body,'/settings/connections',
        'Hosted demo preview — connection details hidden; configuration locked.',legacy_css=css)
