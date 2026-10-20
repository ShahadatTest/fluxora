# Home and workspace routing

Source 0.26.4 separates the public product page from the active synthetic workspace:

| Destination | Route |
| --- | --- |
| Home / product website | `/` |
| Business Overview | `/app` |
| Existing Home bookmark | `/welcome` — retained product-page alias |
| Guided introduction | `/start` |

Home navigation and brand links return to `/`. The **Open workspace** and **Explore the workspace** buttons open `/app`; the workspace's **Overview** navigation stays at `/app`. Existing `/app?demo_example=...` links keep their reviewed synthetic-example behavior. Opening Home does not reset an existing workspace or switch its data.

In public hosted mode, Home is served before visitor admission, like the existing product and documentation pages. It uses the fixed synthetic product example and does not read the active business ledger or model configuration. It issues no workspace cookie, consumes no visitor slot and remains available when workspace capacity is full. Entering `/app` still uses the existing isolated visitor workspace. Protected hosting retains its existing authentication checks.

The affected routing/UI/hosted selection passed **143 tests in 85.22s**:

```text
../../../work/venv/Scripts/python.exe -X utf8 -m pytest -q tests/test_home_routing.py tests/test_fluxora_ui.py tests/test_workspace_ui.py tests/test_hosted.py tests/test_demo_access.py tests/test_takeover_regressions.py tests/test_web.py --basetemp C:/Users/shaha/Documents/Codex/t270target -o cache_dir=../../../work/home270/cache
```

Tests exercise actual local HTTP servers with synthetic SQLite data and local provider stubs. New regressions check Home/alias without admission or provider access, full-capacity Home access, explicit workspace entry, selected Overview links and preservation of a saved local business. Workspace-specific assertions in older tests now visit `/app`; their financial and security assertions remain intact.

Actual Chrome navigation was also checked locally at 360, 390, 1024 and 1280 CSS pixels. The added Home header item introduced no document horizontal overflow in those views. Mobile menu → Open workspace → Overview stayed at `/app`, and the workspace Home/brand link returned to `/`. Browser emulation is separate from physical-device or independent-user testing.

The complete local suite independently passed **2,219 tests in 429.90s (0:07:09)**:

```text
../../../work/venv/Scripts/python.exe -X utf8 -m pytest -q --basetemp C:/Users/shaha/Documents/Codex/t270full -o cache_dir=../../../work/home270/full-cache
```

Both measured commands ran with `PYTHONDONTWRITEBYTECODE=1`. Public HTTPS confirmed that `/` and `/welcome` issue no workspace cookie, `/app` admits the visitor once, and returning between Home and workspace preserves that session and its four cash/decision cards. Desktop and mobile Home → workspace → Overview → Home were also exercised through the browser UI.

Hosted release **0.24.4-8c53b6d99858** passed a fresh disposable actual-worker and restart canary. Scheduled work prepared two DRAFTs and a pending snapshot in 4.656s; separate synthetic owner decisions saved the snapshot, approved one DRAFT and cancelled another. Restart preserved the policy, action identities/states and snapshot without duplication. Cash and observed payments did not change. Telegram transport was simulated and no provider call was made. These finite checks do not certify arbitrary model instructions or a real Telegram decision delivery.

Only Fluxora was deployed, with a stopped-state backup and rollback. The runtime environment, base ledger, shared budget, private Telegram configuration, proxy and unrelated containers were preserved. Seven existing domain canaries returned 200.

No financial, model, Telegram-delivery or approval behavior was added. No live-model accuracy evaluation was repeated, and slides/video remain deferred. Temporary browser viewport overrides were cleared after inspection.
