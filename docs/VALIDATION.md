# Validation

This page distinguishes automated synthetic tests, local HTTP stubs, mocked providers, focused browser checks and real provider checks. No test count certifies every arbitrary model question, independent source truth or production readiness.

## Publication tree

### Final acceptance, source 0.26.1

The [final acceptance report](FINAL-REVIEW-2026-10-20.md) records **2,211 passed in 425.31s**, **97 affected tests in 61.51s** and **143 hosted tests in 117.70s**. It includes a new actual VPS scheduler/decision/restart canary, one fresh public live model forecast and replay, 15 HTTP routes, a bounded Windows early-refusal correction, preserved shared services and the still-blocked rendered desktop/360px/390px check. The fresh-user walkthrough is now **user-reported complete with no problems found**. Detailed observations were not supplied, so this is not relabelled as a documented independent study. Earlier pending statements below describe their historical checks.

### Autopilot, source 0.26.0

Final complete local suite: **2209 passed in 426.26s (0:07:06)**. The command from this checkout was `../../../work/venv/Scripts/python.exe -X utf8 -m pytest -q --basetemp C:/Users/shaha/Documents/Codex/t265release -o cache_dir=../../../work/autopilot265/release-cache`, with `PYTHONDONTWRITEBYTECODE=1`. The new Autopilot selection passed **59 tests in 36.84s**, including all private local-connection regressions. The hosted-branch selection passed **123 passed in 99.18s (0:01:39)**, using `python -X utf8 -m pytest -q tests/test_autopilot_web_telegram.py tests/test_telegram.py tests/test_public_ai.py tests/test_hosted.py` with a short external basetemp/cache. Local setup readiness passed with six skills, three synthetic fixtures and zero model/Telegram calls. Automated tests use local SQLite/HTTP stubs, selector doubles and simulated Telegram APIs; these are not real-delivery evidence.

New regressions cover policy role/business/visitor boundaries, exact currency limits, sufficient-plan-only preparation, no-feasible-plan reporting, unknown dates, self-trigger prevention, saved-policy changes, persisted rolling limits despite history pruning, stopped/paused policy, late candidate rejection, stale generation/ABA, read/DRAFT-only invariant, claimed-job concurrency, effect-journal/control-index crash recovery and actual local worker lifecycle. Telegram tests keep exact approvals, separate snapshots, stale/foreign buttons, uncertain sends and current pairing isolation distinct.

An early complete implementation passed **2,200 tests in 414.51s**, then an expanded run passed **2,206 in 411.42s**. The VPS canary subsequently exposed a real trigger defect: snapshot-save audit metadata was treated as financial input and prepared another pending snapshot. No duplicate DRAFT or cash movement occurred. The trigger now excludes only `forecast_snapshot_saved` owner-operation metadata while retaining financial/source operations. A specific approval→restart→follow-up regression was added. Explicit strategy/limit changes also trigger a fresh analysis without duplicating existing work. The final complete result above includes both corrections. An expanded full run then returned **1 failed / 2,207 passed in 424.40s**: a disabled Autopilot notification check briefly competed for the bot busy lock while a local bot-stop test expected idle access. The check now returns before taking that lock when its policy is missing or notifications are disabled. A dedicated regression and all local-connection tests passed; no busy-operation or approval guard was relaxed. One superseded intermediate full run was deliberately stopped while these changes were made; it is not counted as passing.

The hosted packaging branch initially lacked two visitor workspace helpers used by the new integration; only the disposable canary returned 500. Adding the existing reviewed helper definitions corrected it, and the hosted selection passed. Canary-harness retries also corrected a missing binding digest and reused Telegram update IDs; the persisted duplicate-update guard correctly refused the reused ID. These attempts are not recorded as successful real messaging.

The final VPS canary ran the actual background worker on disposable synthetic data: financial change→analysis→two DRAFTs→one pending snapshot. The measured scheduled completion sample was **3.446s**. Telegram transport in this canary was simulated. Explicit synthetic owner buttons saved the snapshot, approved one exact DRAFT and cancelled another; no cash or observed payment changed. After actual container restart, enabled policy, action IDs/states and saved snapshot were retained, with no duplicate DRAFT. Existing core qualification fingerprints were independently checked; no bypass granted model authority.

A fresh private-model check passed **13 cases in 40.1s**: eight actual Qwen calls and five local guards. A direct private natural-language task then passed with one additional real Qwen call and simulated Telegram transport. Two later corrected canary runs each made one further actual task call. Scheduled analysis itself made zero provider calls. Public HTTPS then passed an isolated opt-in Autopilot policy, scheduled DRAFT/pending preparation, foreign-job denial, private-chat isolation, pause and one actual public model task/replay. These are finite samples, not universal intent accuracy, source certification or provider-uptime guarantees.

The user's existing currently paired private workspace was enabled under their Autopilot request, using existing owner collection caps or a source-derived eligible invoice balance bound when no owner cap was set, earlier-collections strategy, five-minute checks, pending snapshot preparation and private-owner update delivery. Exact selected currency limits appear in the release's PRIVATE-POLICY.json without credentials or owner identity. The Telegram API acknowledged real Autopilot delivery; this is send acknowledgment, not proof the owner read it. No owner decision was clicked on the real private workspace by these checks. Ordinary `/alerts` consent remained unchanged. Model qualification metadata was refreshed only after its exact deployed configuration matched the passing report; credentials were unchanged.

Only Fluxora was deployed, with a private stopped-state backup and rollback. Environment, unrelated services, proxy and base ledger were preserved; legitimate enabled-policy work is isolated to its selected visitor workspace. Seven domain canaries returned 200. Browser inventory still returned `Local browser use is disabled in Work settings`: new rendered desktop/360px/390px inspection was **not run**. Markup/HTTP tests do not replace visual inspection or independent fresh-user feedback. No local Ollama/LM Studio model was evaluated by this release. No real bank/payment/business-counterparty integration or competition submission was added.

### Single Send composer, source 0.25.12

The default composer and assistant handoff now expose only **Send**. Skill selection and optional preview are inside a closed native Options disclosure. An explicit skill-selection link opens those options to show its selected focus. The one-submit backend, model contract, financial tools, deadline, retry policy and approval boundary are unchanged.

The affected selection passed **135 tests in 58.89s**, using synthetic records, mocked selectors and local HTTP stubs. It ran `python -X utf8 -m pytest -q tests/test_direct_tasks.py tests/test_floating_assistant_local.py tests/test_workspace_ui.py tests/test_public_ai.py tests/test_guided_tasks.py tests/test_floating_assistant.py tests/test_fluxora_ui.py` with `PYTHONDONTWRITEBYTECODE=1`, external cache and short basetemp `C:/Users/shaha/Documents/Codex/t264final`. The preceding run returned 1 failed / 134 passed because the disclosure assertion expected lowercase provider at the start of a sentence; its case-independent wording check now retains destination, billing/retention, authority and default-control assertions. No permission or data-disclosure control was removed.

The disposable hosted canary passed HTTP/HTML checks with **zero model calls**. Its unchanged core contract independently matched the existing qualification. A real public HTTPS Send submission then passed forecast in **4.86s** with **one actual Qwen call**, no intermediate disclosure/interpretation confirmation page, selected-task/results rendering and saved-result replay. The floating handoff also showed one primary Send and optional preview. This is one latency/compatibility sample, not universal language accuracy or a speed guarantee.

The previous complete suite was **2,172 passed in 393.71s for 0.25.11**, with the published GitHub workflow also passing. It is not relabeled as a new complete run for this UI-only change. Current rendered browser checks remain unavailable because local browser use is disabled in Work settings; HTTP/markup/DOM tests are not a screenshot review. Final owner approval, missing-fact clarification and independent fresh-user feedback retain their separate boundaries.

### One-submit guided tasks, source 0.25.11

The final complete local suite returned **2172 passed in 393.71s (0:06:33)** on 20 October 2026. It includes mocked-provider selectors, real local HTTP stubs and synthetic deterministic/domain tests; it makes no live-provider claim. The preceding complete run returned **2 failed / 2,170 passed in 397.57s** because two assertions expected the old selected-skill label and old floating-handoff form. Those assertions were updated for the intended interface while keeping qualification, escaped input, unchanged ledger and authority checks. Their focused selection passed **36 tests in 4.76s**. The final complete rerun passed. The direct/guided/public/UI selection passed **118 tests in 46.68s**; the deployed-source selection passed **193 tests in 80.84s**.

Measured full-suite command from the publication checkout (the existing Python 3.12.14 development environment):

```powershell
../../../work/venv/Scripts/python.exe -X utf8 -m pytest -q --basetemp C:/Users/shaha/Documents/Codex/t263final -o cache_dir=../../../work/fast263/final-cache
```

Bytecode was disabled with `PYTHONDONTWRITEBYTECODE=1`. A normal fresh checkout can use `python -m pytest -q` after installing test dependencies; the short basetemp avoids the documented Windows path-depth issue. The hosted selection was `python -X utf8 -m pytest -q tests/test_direct_tasks.py tests/test_guided_tasks.py tests/test_public_ai.py tests/test_agent_skills.py tests/test_workspace_ui.py tests/test_floating_assistant.py tests/test_hosted.py` with external cache/temp paths.

A disposable VPS canary used **two actual Qwen calls**, returning forecast in **3.786s** and an exact USD 100 DRAFT in **4.101s**. Its direct local forecast took **0.038s**. It created one DRAFT, zero approvals, receipts, observed payments or real messages. Local missing-fact refusal made no provider call. Foreign CSRF was refused. After an actual container restart, both submissions replayed with zero extra calls and no duplicate draft. The final candidate differs from that tested candidate only in an updated test assertion; all application-file hashes match. Its unchanged model/tool/skill contract independently matched the existing qualification report; no new qualification run is claimed.

After rollout, **two further actual Qwen calls** over public HTTPS passed one-submit forecast (**4.312s**) and exact USD 100 DRAFT (**9.094s**), selected-task display, deterministic results and duplicate submission. Missing-fact handling made no call; connection reload showed verified, settings stayed locked, the operator-only endpoint returned 403 and floating handoff offered Run task/Review first. These four live samples are compatibility/latency observations, not a guarantee for arbitrary questions or future provider capacity.

Only Fluxora was replaced, with a stopped-state private backup and retained rollback. The first rollout guard compared environment-list ordering and rolled back; comparing all key/value pairs then passed with the private environment file unchanged. The base ledger and public attempt counter were preserved. Unrelated containers and proxy configuration were unchanged; seven domain canaries returned 200. No competition submission, business-counterparty message or payment occurred.

Browser inventory returned `Local browser use is disabled in Work settings`. New rendered 360px/390px/desktop checks were therefore **not run**. Responsive CSS, HTML disclosure and offline DOM behavior were checked; these do not replace rendered inspection or an independent human study. Existing screenshot assets predate the one-submit controls.

### Public guided AI, source 0.25.10

The fresh complete local suite passed **2,153 tests in 382.41s** after adding public AI controls. The preceding run returned 2 failed / 2,150 passed: one stale model-free product-copy assertion and one Windows loopback WinError 10053. The copy assertion now describes the explicit operator option. The passing full rerun retained all domain and security checks. The deployed-branch targeted selection passed **142 tests in 74.22s** using local HTTP stubs and mocked providers. `python -m tools.local_setup --check` passed with zero model/Telegram calls.

The operator explicitly enabled public guided AI on 20 October 2026. A disposable VPS candidate passed **13 qualification cases in 50.81s**, comprising eight actual Qwen task translations and five local guards. One separate real verification request succeeded. Two subsequent candidate HTTP tasks passed forecast and exact USD 100 DRAFT preparation. The canary harness initially expected a token-isolation 400 after it had already consumed the six-request/minute model-route limit, so it received an earlier rejection. Its follow-up checked token isolation separately, corrected the harness's Action.state field access and passed without extra model calls. No canary approval or payment occurred.

After rollout, a fresh public HTTPS visitor completed question disclosure, model translation, exact review, deterministic forecast and exact DRAFT preparation. Replays retained one effect, connection status stayed verified on reload, task qualification was qualified, settings stayed locked and public operator checks returned 403. The public attempt counter recorded 13 total attempts: one verification, eight qualification translations, two candidate HTTP translations and two public HTTPS translations. No raw provider response, credential or visitor cookie is exported in this evidence. Private Telegram model calls are separate from this public counter.

The full source suite includes new atomic daily-budget, restart, UTC rollover, credential-change qualification, HTTP 429/no-retry, locked-settings, operator endpoint, query-path rate-limit and visitor-token isolation regressions. Financial calculations, approval authority and settlement semantics are unchanged. Browser rendering of the new live mode could not be checked because local browser use is disabled in Work settings. Earlier screenshots intentionally show a local disconnected installation. Independent human usability and unrestricted language accuracy remain pending.

### Earlier source 0.25.9

The GitHub preparation contains complete source **0.25.9**, including scrolling and Settings navigation corrections. Application runtime, tools, examples and deployment files are unchanged from the reviewed source snapshot. Repository presentation adds documentation, media, contributor templates and CI. One HTTP retry test was made independent of Windows opener initialization time, as described below. No tests, safety comments, dummy security fixtures, bundled skill instructions or financial controls were removed.

**Fresh full publication-tree result: 2136 passed in 371.02s.** The command ran from this repository with bytecode/cache/test outputs directed outside the publication folder. It used the existing development environment and synthetic tests, local HTTP stubs and mocks. No new live-model or real Telegram delivery evaluation was run. The clean standard-library readiness check passed with six bundled skills, three synthetic examples and zero model/Telegram calls. Source-distribution and wheel builds passed in a separate copy; all 103 application files/assets matched the publication source. The first build attempt lacked setuptools in the development environment; separately installing build dependencies resolved that environment blocker without changing runtime dependencies or the app.

## GitHub Actions

The [cross-platform workflow](https://github.com/ShahadatTest/fluxora/actions/workflows/tests.yml) runs the complete synthetic suite on Python 3.12, Windows and Linux, without optional provider credentials. Its badge reports the actual workflow result; current logs are linked from that page.

The [first run](https://github.com/ShahadatTest/fluxora/actions/runs/39374834372) returned **2,130 passed / 6 skipped on Linux in 277.20s**, and **2,135 passed / 1 failed on Windows in 309.86s**. The Windows failure expected exactly one HTTP request within a 100 ms whole-run budget. The budget expired before the opener reached the stub, so zero requests were correctly made. This is a test timing assumption, rather than evidence that a late financial result was accepted.

That regression now allows the local request to start, then supplies a controlled clock with exactly 100 ms remaining to the real retry policy when the real HTTP stub returns 429. It asserts one request, no backoff sleep, no tool execution and no credential in the answer. Separate real timeout and late-result tests still exercise whole-run deadlines. The focused local HTTP-adapter/takeover selection passed **38 tests in 21.37s** after the change. No runtime deadline, retry policy or financial permission was relaxed. Subsequent complete outcomes are available in the workflow logs.

## Earlier measured results

| Check | Actual outcome | Boundary |
| --- | --- | --- |
| Full automated source suite, 0.25.7 | 2,136 passed in 376.40s | Earlier source; not a fresh full result for 0.25.9 |
| Initial full Windows run, 0.25.7 | 2,135 passed, one loopback WinError 10053 | Failure retained; isolated/group/subsequent full runs passed; cause unconfirmed |
| 0.25.9 UI selection | 60 passed locally; the same 60 passed on the hosted branch | UI regressions, not 120 distinct tests |
| Hosted candidate, 0.23.12 | 85-page workflow, restart, isolation and replay canary passed | Disposable candidate; no production state or external network |
| Live HTTPS and Settings browser checks | Focused navigation passed at 1366×768, 360×800 and 390×844 | Clicks, native keyboard controls and locked settings destination; not every page/device |
| Local provider/takeover regressions, earlier unchanged implementation | 38 passed | Local HTTP stubs/fake responses, including dummy-credential handling; not a real provider |
| Mocked agent selection, earlier unchanged implementation | 34 passed | Explicit deterministic mock; not arbitrary language understanding |
| Fresh live-model smoke on 20 October | Forecast and exact DRAFT translation passed | Two measured tasks in a corrected in-memory harness; one earlier forecast request was also made |
| Telegram identity and practice | Identity API and simulated public workflow passed | No new real private message delivery/callback test |

The initial live-smoke harness counted the wrong transport function and treated the expected audit entry as a financial change. It was corrected before the passing run. Across those attempts there were three model requests, two bot identity requests and zero real bot messages. No passing general-language benchmark or all-operation live evaluation is claimed.

## Reproduce local tests

From the repository root, with a separate development environment containing `.[test]`:

```powershell
python -m pytest -q
python -m pytest tests/test_model_adapter.py tests/test_takeover_regressions.py -q
python -m pytest tests/test_agent.py -q
python -m pytest tests/test_local_setup.py tests/test_local_connections.py -q
python -m tools.local_setup --check
```

Use `python3` or the appropriate environment executable on other platforms. The runtime's no-pip venv does not include pytest. Explicit evaluation utilities under `tools/` use separately configured private environment credentials and can incur charges; ordinary tests use synthetic fixtures, local stubs and mocks.

## Open validation work

Independent fresh-user feedback, a complete current rendered mobile/desktop/accessibility audit, broader live-language evaluation, new real Telegram delivery/callback checks and real-business operational/security review remain separate work. No production financial audit, penetration-test certification or demo recording is supplied by this source cleanup.

The first full publication-tree run used an unusually deep custom test directory and returned 2 failed / 2,134 passed in 372.27s. Both failures involved creating a backup during reset/recovery. A synthetic 271-character SQLite backup path failed with OperationalError while a 117-character control opened; both affected tests passed in 3.16s when rerun with a short test root. The complete short-root rerun is the fresh full result above. No financial/authorization check or application code was relaxed. The Windows long-path limitation and short-directory workaround remain documented, rather than represented as a product fix.

## Publication hygiene

Gitleaks 8.30.1 scanned the final directory with full redaction and inline allow comments disabled. It reported three reviewed test-only findings: an explicitly dummy `CANARY_KEY` in the adapter regression and two `token=token` variable references in workflow tests. No actual provider/bot credential was identified in those findings. No baseline or blanket test-directory exclusion was added. A separate private-key/key-shape/personal-path check found no candidates. Pattern scanners cannot prove the absence of every possible secret.

The archive excludes local databases, vaults, cookies, generated reports, caches, environments, builds and Git history. Fifteen representative private/generated paths were checked against `.gitignore`; six legitimate source/fixture/doc paths remained eligible. All relative Markdown file links resolve. Detailed build/test/scan logs remain outside the upload folder. No license, team identity, repository URL, security certification or competition entry was invented.
