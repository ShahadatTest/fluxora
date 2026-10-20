# Hosted API & Telegram settings preview

In Settings & help, open API & Telegram (`/settings/connections`). The hosted demo displays a static, explicitly labelled locked preview. Inputs and buttons are disabled. No real key, token, selected provider/model, API endpoint, private owner ID, bot identity, configured flag or connection status is read into the page. Password fields have no secret value. There is no reveal/export control and no configuration form.

The server denies all POST requests under this path before dispatch or workspace admission, including crafted requests with JavaScript disabled or controls re-enabled in developer tools. Only the exact GET path without query parameters is a preview. Queries and other subpaths are refused. The existing Host/Origin, framing, rate-limit and protected-mode authentication checks still apply. The static page needs no visitor slot and issues/renews no cookie, including when capacity is full.

The persistent local launcher retains its working private settings at the same path. This hosted preview cannot administer the local installation or the VPS worker. It performs no provider test, model call, Telegram request, bot restart, credential save, financial read or ledger mutation. It uses code-owned text and placeholders only. Approval and financial authority are unchanged.

`tests/test_connection_preview.py` covers static rendering without configuration/ledger access, disabled empty controls, crafted POST refusal for every settings operation, query/subpath refusal, protected-mode authentication, and capacity/cookie behavior. Automated HTTP/HTML tests are separate from rendered browser/mobile validation and real-provider verification.
