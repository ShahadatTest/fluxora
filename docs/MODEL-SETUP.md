# Optional model setup

Direct reports and reviewed owner forms need no model. Public guided AI is disabled by default. The hosted operator can explicitly enable it with the controls in [public AI deployment](PUBLIC-AI.md). For private local use, start through `python -m tools.local_setup --start`, then open Settings & help → API & Telegram.

1. Save a compatible provider profile with a precise model ID and private API key. Save makes no provider call. Multiple profiles may exist; one is active.
2. Activate the profile, inspect the destination/cost disclosure and explicitly test the connection.
3. Run the current synthetic qualification when requested. Passing finite cases does not establish universal question understanding or uptime.
4. Review the exact task and parameters before running a report or preparing DRAFT/pending work. Use deterministic financial cards as the authoritative result; advanced model prose remains unverified.

Environment configuration is an alternative: `CASHFLOW_MODEL_API_KEY`, `CASHFLOW_MODEL_BASE_URL`, and `CASHFLOW_MODEL_NAME`. The model name is required for chat. Never commit values or put credentials in command arguments, questions, screenshots or public issues. The [local setup guide](LOCAL-SETUP.md) documents hidden prompts and optional Windows Remember storage.

This adapter uses OpenAI-compatible chat completions/tools; provider-native protocols may differ. Endpoint, model availability, quota, cost and tool support are provider-dependent. No automatic fallback/provider purchase is performed. Restart starts verification unverified; relevant configuration/source changes invalidate qualification. Failures do not authorize background financial work.

The model may read supported reports and prepare proposals. It cannot self-approve, invent consent/receipts, contact business counterparties or move money. Exact owner decisions and source facts remain separate. See [security](SECURITY.md).
