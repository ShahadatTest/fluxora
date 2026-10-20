# Local connection settings: scope, evidence and limitations

Version 0.25.0 adds Web credential administration only to `cashflow_copilot.local_app`, the persistent single-owner loopback launcher. Existing financial algorithms and approval boundaries remain. Hosted public mode shows a static locked preview; credential mutation requests are refused before visitor admission. Legacy servers have no local connection manager. This is not a public credential-management service or production authentication.

## Supported connection contract

Up to 12 named API/model profiles, with one active selection. An environment selection and an explicit disconnected selection remain available. Presets supply base URLs; model IDs are supplied by the owner. No background verification, automatic provider fallback or automatic paid qualification is added. Remote endpoints require HTTPS; loopback HTTP is permitted, with optional key-free local compatibility. URLs containing userinfo, query/fragment or a final `/chat/completions` suffix are rejected. Owners still control and must trust the chosen destination; this is not a vendor allowlist or protection against a malicious endpoint.

This adapter uses OpenAI-compatible chat completions and tools, not every vendor's native protocol. Official endpoint references: [OpenRouter](https://openrouter.ai/docs/api/reference/overview), [Gemini compatibility](https://ai.google.dev/gemini-api/docs/openai), [DeepSeek](https://api-docs.deepseek.com/), [Groq](https://console.groq.com/docs/openai), [Mistral](https://docs.mistral.ai/resources/migration-guides). A model's current availability, quotas, price and tool support remain provider-dependent. No new live model success or universal vendor compatibility is claimed.

Save does not contact providers. Verify is an explicit potentially billed minimal call. The same active adapter supplies the Web banner, Agent and opted-in local Telegram model. Edits/activation replace the adapter, clear staged Web previews, revoke pending Telegram model-task/check reviews and change the model qualification fingerprint with a non-secret configuration revision. Restart starts connection verification unverified. Qualification is separate, limited synthetic evidence.

## Credential storage and threat boundary

- Default Web credentials/profiles are server session memory; stopping forgets them. No environment mutation or secret write to financial SQLite, audit export, page HTML or browser storage is introduced. Password fields are empty on response; blank keys retain the old key only during explicit profile edits.
- Optional Windows Remember encrypts the entire remembered configuration using current-user `CryptProtectData` / DPAPI with UI disabled. The file `.local-connections.dpapi` lives beside the configured database. Writes use a temporary encrypted file and atomic replace. No plaintext fallback or portable master key. No OS keyring implementation is provided for macOS/Linux; Web settings there are session-only.
- DPAPI reduces exposure from copying the saved file. It does not protect against same-account programs, browser extensions reading entry, memory inspection, an administrator, compromised OS or a malicious owner-selected provider. Python strings are not guaranteed to be securely erased. Loopback HTTP is not TLS; local software/network stack remains trusted.
- Damaged/undecryptable saved settings leave AI off and are not overwritten silently. Session-only replacements remain available; explicitly Forget saved settings before saving remembered replacements. Forget/delete removes the local saved copy, not external provider validity and not a guaranteed physical disk wipe.
- Bind remains `127.0.0.1` only. Exact Host/Origin, existing CSRF, framing checks and no-store responses apply. These reduce browser cross-site requests; they do not authenticate local OS processes. Do not reverse-proxy this credential UI to the public Internet.
- Source packages exclude databases, `.runtime`, encrypted vaults, credentials, environments and caches. Financial backup/export does not include connection settings. Re-enter credentials when moving accounts/computers. Do not paste credentials into labels, model IDs, URLs, agent questions, screenshots or chat.

## Telegram lifecycle

Save only validates configuration. Test identity calls only `getMe`; Start explicitly starts an outbound poller. Numeric private-owner allowlist and one-time workspace pairing remain required. Remembered tokens do not auto-start a poller after restart. The existing computer/app must stay running and online. Use a separate local bot when the VPS token already has a poller/webhook.

Stop and API changes refuse a busy bot task. Stop checks prevent returned updates from starting more reviewed tasks after stopping. Token/owner/username/model-mode changes clear pairing and revoke old pending reviews/queued deliveries; changing only Remember preserves pairing. Previously sent messages cannot be recalled. A configuration call cannot stop an independently running VPS poller. Telegram and optional AI calls are real network traffic; records and financial operations remain synthetic.

## Verification evidence

`tests/test_local_connections.py` uses dummy credentials, a real loopback HTTP provider stub, in-process Telegram transport stubs, and actual Windows current-user DPAPI encryption/reload. It covers verification→page reload, configuration invalidation, 401 reflected-secret redaction, hostile Host/Origin/CSRF, hosted denial without visitor allocation, no financial secret writes, busy-bot refusal, stale pairing revocation, key-free loopback and restart without automatic bot calls. These tests do not establish live-provider quotas, model accuracy, real Telegram delivery, other OS behavior, another Windows account's decryption failure or rendered mobile layout. Actual commands/counts and clean-package verification belong to the accompanying release report.

No extra model authority: read reports and prepare DRAFT/pending work only. Owner approval, counterparty acceptance, observed payment and reconciliation remain separate. Documents supply evidence, never authority. No bank integration, money movement or business-counterparty messaging is added.
