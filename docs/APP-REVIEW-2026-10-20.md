# Broader application review

Source **0.26.2**, 20 October 2026. Presentation and video work are deferred while application review continues. No new presentation or video was made for this correction.

## Hosted HTTP and workflow checks

A fresh certificate-verified HTTPS visitor opened **90 navigation/documentation pages** and **10 CSS/JavaScript assets**. All returned 200. HTML checks found one main heading and main target per page, no duplicate element IDs and no UTF-8 replacement characters. Internal anchor checks passed after distinguishing ordinary anchors from the ten code-owned judge/owner guide destinations. Those guide fragments select JavaScript walkthrough state and intentionally do not refer to static HTML IDs.

The live `/telegram/demo` workflow passed through its displayed form identities:

1. Forecast on its own disposable Atlas lesson.
2. Sample DRAFT request, followed by an explicit preparation review.
3. Replay of that accepted request without a second DRAFT.
4. Snapshot request, preparation review and a separate owner approval.
5. Earlier DRAFT still unapproved after snapshot approval.
6. Rejection of another preparation request without another DRAFT.
7. Main visitor financial audit unchanged before and after the whole lesson.

These requests used the actual deployed HTTPS endpoints with synthetic data. They made **zero model calls and zero real Telegram API calls**. This is a browser-practice backend check, not a claim of real private callback delivery or rendered visual inspection. The current hosted container remained healthy.

Earlier actual scheduler, restart, scoped history, exact owner decisions and live-model forecast evidence remains in [final acceptance](FINAL-REVIEW-2026-10-20.md). No real owner approval or counterparty payment was performed by the assistant.

## Local bot-settings race

GitHub run [39401524397](https://github.com/ShahadatTest/fluxora/actions/runs/39401524397) passed Windows but returned **1 failed / 2,204 passed / 6 skipped** on Linux. The failed test stopped an otherwise idle local bot while its polling housekeeping briefly held the same lock. A nonblocking acquire treated this short housekeeping interval as a busy business request.

Connection operations now allow **up to 250 ms** to acquire the existing lock. They proceed only after acquiring it. A request that remains busy still refuses the configuration change or stop, and the worker is not interrupted. No approval, idle-request or credential-revocation guard was removed. Controlled-contention regressions cover both Stop and profile activation, while existing genuine-busy and token-change tests retain their assertions.

Affected selection: **91 passed in 70.90s**, using:

```text
python -X utf8 -m pytest -q tests/test_local_connections.py tests/test_local_setup.py tests/test_autopilot_web_telegram.py tests/test_telegram.py
```

The measured Windows command used `PYTHONDONTWRITEBYTECODE=1`, interpreter `../../../work/venv/Scripts/python.exe`, external basetemp `C:/Users/shaha/Documents/Codex/t267target` and cache `../../../work/app-review267/cache` from the clean checkout. These tests use local HTTP/SQLite and simulated providers/Telegram. Use the actual [GitHub Actions result](https://github.com/ShahadatTest/fluxora/actions/workflows/tests.yml) for the subsequently published complete cross-platform run. The complete **2,211-pass** result in final acceptance belongs to predecessor source 0.26.1, not a relabelled complete run for this correction.

Only private local configuration code changed in 0.26.2. Hosted settings remain locked and do not import this manager, so the local correction does not require replacing the healthy hosted container. Financial calculations and model contracts are unchanged.

## Remaining visual check

Desktop, 360px and 390px rendered inspection remains **not run** because the browser tool reports “Local browser use is disabled in Work settings”. Mobile breakpoints, menu behavior, keyboard support, scrolling rules and existing DOM-double tests can be inspected in code, but that does not prove pixel layout, actual touch scrolling or device behavior. The HTTP audit does not substitute for this missing check.

The owner reports that a fresh person completed a walkthrough without finding problems. Device/task/timing details were not supplied. Treat that as user-reported feedback, rather than a documented accessibility or usability study. No universal correctness or production-readiness claim follows from these checks.
