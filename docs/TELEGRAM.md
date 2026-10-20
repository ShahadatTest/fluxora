# Telegram access and private setup

## Public judge practice

Open `/telegram/demo` locally or [the hosted practice](https://fluxora.sklab.cc/telegram/demo). It needs no Telegram account or token. Supported commands and delivery are simulated, using a separate disposable synthetic lesson and the actual deterministic tools. A DRAFT is separate from the later snapshot-approval example. Practice never grants access to the operator's private bot or model.

## Private local companion

1. Start the persistent local app. Open Settings & help → API & Telegram.
2. Enter a bot token privately, your allowed numeric owner ID and optional bot username. Save, Test identity and Start bot are separate operations. Do not reuse a token already polling in another app/server.
3. Open `/telegram`, acknowledge the destinations and generate a one-time pairing code. Send `/link CODE` only from your allowed private account to your own bot. Do not publish the code.
4. Use `/forecast`, `/plans` and `/status` for deterministic reports. `/draft inv-client-001 USD 100 2026-10-22` prepares an exact review for the bundled Atlas example; confirm only the terms you intend. Creation produces a DRAFT, not approval or payment.
5. `/snapshot Review label` has separate preparation and owner approval. Telegram execution is limited to the reviewed snapshot-save workflow; other workflow approvals use the detailed Web review.
6. `/autopilot on` enables/resumes the saved Autopilot policy and opts into its private updates. Configure exact strategy and currency limits in Web → Autopilot first; the default is monitor-only. Supported report/DRAFT/pending tasks run without a preparation-review click while ON. Exact final decisions remain separate. `/autopilot off` stops future preparations and queued Autopilot messages. See [Autopilot](AUTOPILOT.md).
7. Alerts are opt-in through `/alerts on`; `/alerts off` disables alerts and `/unlink` revokes the link. Ordinary replies follow explicit commands. These are real messages about synthetic records, not messages to customers or suppliers.

The computer/app and Internet connection must remain available. Optional natural-language interpretation requires a separately configured, verified and qualified model; interpretation review applies with Autopilot OFF; while ON, validated read/DRAFT preparation is direct and final approval stays separate. Remembered settings never start polling automatically on restart. Tokens remain in session memory by default; optional Windows Remember uses current-account DPAPI. No token is prefilled in HTML.

Environment/hidden-prompt alternatives, persistence, expiry, and restart instructions are in [local setup](LOCAL-SETUP.md). Private hosted operation uses environment configuration and the same allowlist/pairing boundary. Only one workspace is bound at a time. Public judges are not automatically allowlisted. See [security](SECURITY.md) and [practice design](TELEGRAM-INTERACTIVE-DEMO.md).
