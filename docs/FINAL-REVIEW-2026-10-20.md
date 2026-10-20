# Final acceptance review, 20 October 2026

Source version **0.26.1**. The corresponding restricted hosted packaging branch is **0.24.1-9a12ef4ee4a2**. This is a synthetic demonstration with no real bank integration, counterparty message execution or payment rail.

## Measured checks

| Check | Actual result | Boundary |
| --- | --- | --- |
| Complete local suite | **2,211 passed in 425.31s** | Synthetic SQLite data, local HTTP stubs, mocked selectors and simulated Telegram APIs |
| Affected selection | **97 passed in 61.51s** | Locked settings, hosted boundary, Autopilot and Telegram integration |
| Hosted source selection | **143 passed in 117.70s** | Hosted boundary, public AI controls, Telegram and Autopilot |
| Actual VPS worker | Passed | New disposable visitor, financial change, two DRAFTs and one pending snapshot |
| Synthetic owner decisions | Passed | Snapshot save, one DRAFT approval, one rejection, unchanged cash and zero observed payments |
| Actual canary container restart | Passed | Same action IDs/states and saved snapshot, enabled policy retained, zero duplicate DRAFTs |
| Public HTTPS | Passed | Separate visitor policy, completed job/history, foreign access refusal, pause and private-chat isolation |
| Fresh real model task | Passed | One explicit public forecast, saved-result replay, verified connection. One new provider task call |
| Public route review | **15 routes returned 200** | UTF-8 decoding and viewport metadata checked. This is HTTP/markup evidence |
| Locked settings POST | **403** | No submitted dummy key echo, no configuration operation |
| Shared VPS checks | **7 domain canaries returned 200** | Unrelated container image/start times preserved |
| Fresh-user walkthrough | User reports completion with no problems found | No participant/device/task/timing log supplied. Not a documented usability study |
| Rendered desktop, 360px, 390px | **Not run in this review** | Browser inventory returned “Local browser use is disabled in Work settings” |

The 0.26.0 private-model qualification remains separate evidence: eight actual model interpretation cases and five local guards passed. This review did not rerun that qualification or test an actual local Ollama/LM Studio model. Finite live cases do not establish universal language accuracy or provider availability. The scheduled worker makes **zero model calls**, including this acceptance run.

Telegram transport for the new callback tests was **simulated**. Earlier real private Telegram API delivery acknowledgments remain recorded in [validation](VALIDATION.md). No assistant-operated owner decision, counterparty message or payment occurred in the real private workspace. An API acknowledgment is not proof the owner read a message.

## Commands and reproduction

Actual complete-suite command from the clean checkout, with `PYTHONDONTWRITEBYTECODE=1`:

```powershell
../../../work/venv/Scripts/python.exe -X utf8 -m pytest -q --basetemp C:/Users/shaha/Documents/Codex/t266full -o cache_dir=../../../work/final266/full-cache
```

Actual affected selection used the same interpreter and external cache, with:

```text
tests/test_connection_preview.py tests/test_hosted.py tests/test_autopilot.py tests/test_autopilot_web_telegram.py
```

The hosted branch used `../../venv/Scripts/python.exe -X utf8 -m pytest -q` from its isolated source folder, with short external basetemp/cache and:

```text
tests/test_connection_preview.py tests/test_hosted.py tests/test_autopilot_web_telegram.py tests/test_telegram.py tests/test_public_ai.py
```

For another checkout, create its documented test environment and run `python -m pytest -q`. The paths above describe the measured run, not an installation prerequisite. The VPS checks exercised the actual background scheduler and restarted only a disposable canary. Public checks used ordinary certificate-verified HTTPS sessions with disposable visitor cookies held in memory. Private tokens/cookies, databases and raw provider output are excluded from publication.

## Windows early-refusal correction

The previous GitHub Actions run [39397587098](https://github.com/ShahadatTest/fluxora/actions/runs/39397587098) passed Linux but returned **1 failed / 2,208 passed** on Windows. One locked-settings POST received `WinError 10053` instead of its expected 403. The response path closed a connection with a small POST body still unread, a known reset mechanism on Windows.

The hosted handler now sends and half-closes the refusal first, then discards only a correctly framed body of at most 64 KiB, with a total 250 ms budget and at most 50 ms per read. It never parses or dispatches those bytes. Already consumed, oversized or ambiguous bodies do not enter that discard loop. New HTTP regressions withhold the body until after receiving 403, then either send it or close without it. All pre-existing lock, origin, CSRF and visitor admission checks remain enforced. The complete local and affected hosted results above include this correction. Consult the repository's actual Actions status for the subsequently published commit.

## Deployment and rollback

Only Fluxora changed. The restricted container remains healthy, runs as UID 10001 with a read-only root filesystem, and exposes no host port directly. Existing private environment, model qualification boundary, Telegram configuration, base ledger and durable public attempt budget were preserved. Model verification correctly resets after restart and the explicit HTTPS task verified the active adapter again.

A private stopped-state backup and rollback script exist on the VPS. No proxy or unrelated service was modified. Disposable canary removal followed successful live route checks. A browser tool limitation is not a passing visual inspection, and code review of mobile breakpoints, scrolling containers and reduced-motion rules does not establish device behavior.

## Remaining review

Enable the browser tool or manually inspect desktop, 360px and 390px. Check page scrolling over tables/chat, menu access, Settings/help, forms and the floating guide, then repeat a DRAFT and separate approval workflow. Independent accessibility and real-business security/operations reviews remain outside these synthetic checks.

The owner excluded video creation and entry submission from this work. Neither was performed.
