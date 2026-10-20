# Validation

This page distinguishes automated synthetic tests, local HTTP stubs, mocked providers, focused browser checks and real provider checks. No test count certifies every arbitrary model question, independent source truth or production readiness.

## Publication tree

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
