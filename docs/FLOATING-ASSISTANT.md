# Floating Ask Fluxora guide

The product website, documentation and workspace include a collapsed **Ask Fluxora** launcher. Desktop opens a side dialog; narrow viewports use a bottom sheet. The launcher is hidden until its local script supports native modal dialogs; without JavaScript a normal Agent link is available. The dialog uses a visible close button, native Escape/focus containment and focus return. It starts closed and reserves lower content spacing. The locked API/Telegram preview and focused Telegram practice remain self-contained without the floating panel.

## What happens in the panel

The panel provides **local guidance, not live model chat**. It does not inspect financial records, configuration, active model or uploaded evidence. Page purpose/help links come from the code-owned page registry. Example buttons suggest deterministic forecast, Daily review/plans/missing facts, DRAFT preparation, exact owner decisions and Telegram practice. The guide does not invent balances, risks or the best plan. Those require opening the existing deterministic report. Financial results remain separate from generic explanation.

Typed text is routed by a small local keyword matcher to these same fixed guidance cards. It may misunderstand a request and cannot approve/pay/send or certify consent/receipts. Text and links are inserted with DOM `textContent`/attributes, never interpreted as HTML. At most 12 displayed messages are retained. No background request, provider call, transcript persistence, cookie/localStorage/sessionStorage write or browser credential entry is added. Closing/navigation is not a saved conversation; do not type secrets.

With public AI disabled, the public hosted demo is model-free: guidance and links only. A crafted `/assistant/continue` POST cannot access the private model or execute anything. When the operator explicitly enables public guided AI, Continue in Agent becomes available; it opens the disclosed task form and makes no provider call itself. Product/docs pages and the new local static assets remain cookie-free and allocate no visitor slot.

## Private or explicitly enabled public handoff

Private local mode and operator-enabled public AI add **Continue in Agent**. This explicit POST sends only the exact typed question to the same app, not a model. The response supplies a normal prefilled Agent form with server-selected business/request identity. Qualification is checked; an unqualified configuration cannot continue. The prefilled form displays provider/data disclosure. Run task uses `/agent/tasks/start` to translate once and run only a validated report or prepare DRAFT/pending work. Review first retains the outbound preview and exact interpretation confirmation. Both model routes retain the public rate/daily limits. There is no automatic verification, qualification, fallback or self-approval. See [one-submit tasks](DIRECT-TASKS.md).

Model financial prose never enters the floating guide. A qualified model still can misunderstand a question, and a successful connection/qualification is not universal accuracy. DRAFT/pending preparation, human owner approval, counterparty acceptance, observed payment and reconciliation retain their existing separate boundaries.

## Verification boundaries

`test_floating_assistant.py`, `test_floating_assistant_local.py` and the offline Node DOM-double test cover public no-data/config/model access, cookie-free public assets, disabled-preview/practice separation, crafted public handoff denial, private exact escaped question handoff, qualification gate, unchanged ledger, Host/Origin/CSRF, input framing and local dialog/message behavior. The DOM-double test checks open/close/focus return, safe text rendering, local intent routing, message bounds and explicit form submission; it is not a rendered browser/mobile test. Runtime uses only standard-library Python and locally served CSS/JS. Actual full-suite/HTTPS/candidate results and remaining visual limitations belong to the accompanying release report.
