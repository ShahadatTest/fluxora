# Fluxora security self-assessment

Source 0.26.4. Updated 20 October 2026 after the rendered judge walkthrough and two live guided tasks. Synthetic demonstration only.

## Assets and trusted boundaries

Assets include financial records, source attachments, exact action terms, review tokens, session cookies, API/bot credentials and audit history. The local OS account and server administrators are trusted. SQLite records, attachments and backups are plaintext files. HTTPS protects hosted transport, not data from a trusted host administrator.

External model and Telegram providers form separate trust boundaries. Questions can themselves contain sensitive information even when the app does not send a financial ledger. Provider retention, capacity and billing require operator assessment. Anonymous visitor cookies are bearer access to one synthetic workspace, not production identity.

## Permission tiers

| Actor/path | Permitted scope | Required boundary |
| --- | --- | --- |
| Public visitor | Its own synthetic session, deterministic reports and existing reviewed owner forms | Secure cookie, business scope, Host/Origin/CSRF and existing form checks |
| Model/agent | Supported read tools and DRAFT/pending preparation | No approve, consent-fabrication, real-send, payment or permission tool |
| Owner decision | Exact supported local workflow or lifecycle transition | Current review token, revision/scope checks and explicit decision |
| Observation/acceptance recording | Existing source-backed domain workflow | Specific confirmation and source requirements; owner approval alone is insufficient |
| Private Telegram owner | Explicitly allowlisted and paired workspace | One-time linking, scoped/stale-button checks and opt-in alerts |
| Server/OS operator | Deployment and private configuration | Trusted administrative access; outside anonymous visitor authority |

An agent can prepare supported local changes for review. This does not let it approve itself. A server executor may apply the reviewed local scope after the owner decides. Documents never grant a permission. A receipt, acceptance event or bank observation cannot be invented to make a plan feasible.

The one-submit route may prepare an unintended but schema-valid DRAFT/pending instruction. Literal values, deterministic validation and the read/DRAFT boundary constrain it; they do not prove intent. Exact selected terms are shown in results and require separate owner approval before any supported local execution. Use Options → Preview before sending when interpretation needs checking before preparation. A changed ledger or expired deadline rejects candidate work. Durable replay prevents a repeated completed submission from contacting the provider or creating another draft.

Autopilot adds explicitly authorized background jobs, not model approval authority. Policies require owner role and current workspace/business scope. The worker has no model/network tool, uses an agent context and checks the existing read/DRAFT-only invariant before committing. A separate job claim, policy revision, unchanged ledger generation and deadline gate precede the atomic ledger/effect-journal commit. The scheduling index can recover a committed journal after restart. Telegram messages use the current paired owner only, with notification opt-in, escaped exact review cards and durable delivery keys. Unknown sends are not automatically repeated. A host compromise, same-account software, false input evidence, resource exhaustion or arbitrary future model intent remains outside those guarantees. See [Autopilot](AUTOPILOT.md).

## Implemented controls and evidence

| Threat | Implemented control | Evidence and limit |
| --- | --- | --- |
| Provider body leaks a secret | Discard arbitrary provider error bodies and use local diagnostics before display/history | Dummy-key 401 regressions inspect HTML, answers and SQLite bytes. This does not scan every possible dependency or log path. |
| Wrong configuration appears verified | Server-side adapter state follows the active configuration and restarts unverified | Loopback HTTP verification/reload/configuration-change regressions. Verification does not promise provider capacity. |
| Late response creates work | Remaining deadline reaches requests/retries, response bytes are bounded, and late results are rejected before tools | Late-answer, late-DRAFT, byte-limit and retry-exhaustion regressions. Remote work/billing may outlive the local timeout. |
| Prompt or document authorizes a change | Fixed tool/schema surface, code authorization and separate final owner decisions; optional exact interpretation preview | Forbidden tools, document-injection and business-role tests. Finite language guards do not recognize every paraphrase. |
| Cross-business access | Business-scoped contexts, visitor separation and scoped identifiers | Cross-CSRF, preview, backup and history canaries on isolated sessions. Cookies and administrators remain trusted. |
| Repeated or stale request creates effects | Durable identities, transaction boundaries and revision-bound review | Lifecycle/restart/replay tests, stale-button and duplicate-review practice checks. Users should inspect history after uncertain responses. |
| Public visitor changes or reads credentials | Locked static hosted preview with disabled empty fields and server refusal before admission | 15 empty disabled controls, zero forms and 11 refused POST variants in the prior byte-verified release checks. |
| Payroll/consent/FX misuse | Deterministic protection, source-backed consent rules, separate currencies and display-only FX | Financial/domain regressions plus three explicit local guards in the fresh smoke. Source truth is not independently authenticated. |
| Real money or counterparty messages | No real banking/payment rail or business-counterparty transport | Code/catalog scope. Optional private Telegram sends notifications about synthetic records separately. |

The complete source contains the relevant tests. The current application source passed **2,219 tests in 429.90s**. A later adapter/takeover/direct/guided/Telegram selection passed **109 tests in 40.45s**, including local provider HTTP stubs, dummy credentials and fake/mock adapters. These are separate from actual live-provider checks. [Validation](VALIDATION.md) retains earlier failed runs and their subsequent outcomes; no security control was relaxed to obtain a pass.

## Current live evidence

The latest [rendered judge walkthrough](JUDGE-WALKTHROUGH-2026-10-20.md) used the actual public website and its configured OpenRouter `qwen/qwen3.7-flash` model. Two translations passed: a read-only baseline forecast and an exact USD 100 early-payment DRAFT. No 429 or timeout appeared in those two samples. Financial answers came from deterministic tools. The DRAFT remained unapproved after saved-result reload and a different forecast-snapshot approval. The snapshot was saved only through the explicit owner review form; its approval created no payment or counterparty acceptance. These synthetic records were persisted in that visitor workspace, not a real business ledger.

Rendered Telegram browser practice separately passed forecast, DRAFT preparation and snapshot approval. A fresh HTTPS visitor harness also checked replay, rejection and equality of the main financial audit before/after practice. That lesson uses simulated transport and zero model calls. No new real Telegram delivery/callback test, private pairing or second bot worker was performed in this pass. Earlier private API acknowledgments remain historical and do not prove message reading.

## Credential handling and packaging

Model and bot keys stay server-side. Default local Web configuration keeps secrets in process memory. Optional Windows Remember uses an OS-account DPAPI vault outside financial SQLite/export. Same-account software and administrators remain trusted. Hosted visitors cannot administer these private settings. No key is prefilled into HTML.

The GitHub publication tree includes source, design assets and summarized synthetic verification evidence. The reviewed package is built from the committed Git tree, with an explicit inventory and checksums. API/bot/SSH credentials, runtime databases, vaults, caches, virtual environments and session-cookie jars are excluded. Published screenshots/results contain invented task terms, model identity and provider-reported usage; they contain no raw provider error body, API key, pairing code, private bot chat or browser credential. A clean manifest/pattern scan is evidence for the inspected package, not proof that every possible secret or security defect is absent.

## Known risks and work still required

- A valid structured task can misunderstand intent. Exact review remains necessary. Optional advanced-agent prose can be wrong and remains unverified.
- Input evidence may be false, incomplete or stale. Code-generated results do not authenticate documents, observed facts or a counterparty identity.
- Provider 429/5xx, outages, billing and remote work after local timeout remain possible. No automatic provider switch is introduced. Direct deterministic reports remain available.
- Private bot delivery may be uncertain. User/bot account compromise, server compromise and lost browser cookies remain outside the demonstrated source-fact guarantees.
- Public admission is finite at 256 unexpired workspaces or the aggregate storage cap. Testing also consumes slots. Expiry and an examples fallback do not provide production denial-of-service protection.
- Rendered principal-page reviews and focused Settings navigation passed at desktop, 360px and 390px. The latest walkthrough checked eight surfaces at three widths with no document overflow. This is Chrome emulation; a full-site accessibility audit, physical phones and Safari remain untested.
- The owner reports a fresh-user walkthrough with no problems found, without participant/task/timing records. The agent-operated browser review is not an independent human study. No penetration test, certified financial audit or production authentication review is claimed.
- On Windows, deeply nested database/backup paths can fail in SQLite even when Python can create the parent directory. Use a short, writable local data path; no automatic data move or reset is performed.
- No real-bank integration, real payment execution, lender matching integration or real-business pilot is demonstrated.

Use this release for synthetic demonstrations and controlled evaluation. Further identity, operational, data-authentication and independent security work is required before a real-business pilot.


Public guided AI in 0.25.10 is an explicit operator option. See [public AI controls](PUBLIC-AI.md) for the durable daily attempt counter, zero retries, qualification, data disclosure and endpoint restrictions. The model still cannot approve work, invent source events or move money. Private Telegram model use has a separate scope from the public allowance.
