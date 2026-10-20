"""Local recovery guidance. No provider text, automatic retry or model fallback."""

REPORTS = (
    ('/decision-brief', 'Decision Brief', 'cash gaps, sources and workflow states'),
    ('/weekly-cashflow', '13-week cash view', 'daily minima grouped by week'),
    ('/plans', 'Plan Comparison', 'bounded proposals and honest no-feasible results'),
    ('/entries', 'Owner Entries', 'reviewed forms for supported local work'),
    ('/agent/workflows', 'Workflow queue', 'pending, rejected and executed work'),
)

GUIDANCE = {
    'rate_limited': 'The provider rate limit was reached. Follow the connection panel cooldown before a fresh attempt.',
    'incomplete_response': 'The provider returned an incomplete response. Try one smaller, explicit question after reviewing current facts.',
    'transport_or_timeout': 'The provider connection failed or timed out. Check connectivity before a fresh attempt.',
    'provider_unavailable': 'The provider reported a temporary server failure. Use the direct reports while it recovers.',
    'authorization': 'The provider rejected authorization. Check the local environment configuration without sharing the key.',
    'credit_or_policy': 'The provider rejected this request. Check the account or request policy in the provider dashboard.',
    'timeout': 'The run deadline expired. Reduce the request to one report or one pending operation.',
    'tool_limit': 'The tool-call limit was reached. Split the task into smaller requests.',
    'empty_answer': 'No final answer was returned. Use a specific report or ask one smaller question.',
    'stale': 'The ledger changed during the run. Inspect current facts before previewing a fresh question.',
    'interrupted': 'The server stopped before completing this request. Inspect the queue and current ledger before asking again.',
    'failed': 'The run failed or a safety boundary rejected it. Inspect current facts and use an explicit supported request.',
}


def failure_category(result, adapter):
    """Persist a small symbolic category, never an arbitrary exception/status body."""
    outcome = getattr(result, 'outcome', 'failed')
    if outcome != 'provider_error':
        return outcome if outcome in GUIDANCE else 'failed'
    try:
        detail = adapter.status().detail
    except Exception:
        return 'failed'
    for status, category in ((429, 'rate_limited'), (401, 'authorization'),
                             (403, 'authorization'), (402, 'credit_or_policy')):
        if isinstance(detail, str) and detail.startswith(f'model provider returned HTTP {status}:'):
            return category
    if isinstance(detail, str) and any(detail.startswith(f'model provider returned HTTP {status}:') for status in (500, 502, 503, 504)):
        return 'provider_unavailable'
    if detail == 'provider did not complete its response; no partial tool request accepted':
        return 'incomplete_response'
    if detail == 'model provider call failed: transport error or timeout':
        return 'transport_or_timeout'
    if detail == 'model provider deadline budget exhausted':
        return 'timeout'
    return 'failed'


def reports_html(web):
    links = ''.join(f"<li><a href='{url}'>{web.esc(title)}</a> — {web.esc(description)}</li>"
                    for url, title, description in REPORTS)
    return ("<details class='card' data-direct-reports><summary>Continue without a model</summary>"
            "<p>These pages use local deterministic calculations and reviewed owner forms. "
            "Opening them makes no model request. Figures use recorded inputs; source truth is not independently verified.</p>"
            f"<ul>{links}</ul></details>")


def recovery_html(web, outcome):
    if outcome.status == 'running':
        return reports_html(web)
    result = outcome.result
    if outcome.status == 'completed':
        if result and any(c.get('ok') for c in result.tool_calls):
            return ''
        return ("<div class='card' data-agent-recovery><h2>No tool-backed result</h2>"
                "<p>The agent may be requesting details or declining the request. Open the optional, unverified "
                "model commentary below to read its response. No financial figure or completion claim in that text is verified.</p>"
                "<p>For a new observation, supply its actual source, record, currency, amount and date. "
                "Do not turn an estimate into an observed payment.</p></div>" + reports_html(web))
    category = (result.disclosure.get('failure_category') if result else None) or outcome.status
    guidance = GUIDANCE.get(category, GUIDANCE['failed'])
    return ("<div class='card' data-agent-recovery><h2>Continue after an incomplete request</h2>"
            f"<p>{web.esc(guidance)}</p>"
            "<p>No candidate drafts from this request were saved. Refreshing its saved result will not run it again. "
            "Earlier requests and owner-approved work remain separate. A fresh question uses a new identity and data preview; "
            "it can incur another provider charge. No background retry or automatic model switch is running.</p>"
            "<p><a href='/agent#agent-panel'>Review a fresh question</a> · <a href='/agent/workflows'>Inspect the queue</a></p></div>"
            + reports_html(web))
