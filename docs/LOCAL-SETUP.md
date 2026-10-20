# Run the full Fluxora website on your computer

This is the English synthetic sandbox, including its multipage website, workspace, deterministic reports, evidence/ledger, planning, approval workflows, action history, reconciliation, six agent skills, backups and documentation. No VPS, Docker, Node, npm or database server is needed. Runtime uses Python's standard library. Python 3.10+ must already be installed; Python itself is not bundled. For Windows install Python from https://www.python.org/downloads/windows/ if missing.

## Windows: start in one step

1. Extract the complete source folder to a writable directory, such as `Documents\Fluxora`. In a submission ZIP, open the inner `source` folder that contains `START_FLUXORA.cmd`.
2. Double-click **START_FLUXORA.cmd**. It creates `.venv` with your installed Python, uses the source directly, and opens the local product website. No pip install or network download is required. The terminal stays open while the app runs.
3. Open **http://127.0.0.1:8765/app** for the workspace, `/welcome` for the website and `/docs` for the documentation. Use the displayed `127.0.0.1` address, not `localhost`, for the local Host/Origin checks.
4. Stop with Ctrl+C. Run the same launcher again to continue the saved database. There is no 24-hour expiry or 128-visitor limit in this private single-user mode. Closing the browser does not stop the server.

The CMD file uses a process-only PowerShell execution-policy override for this included script; it does not change the Windows policy globally. Inspect `tools/setup_local.ps1` if desired. You can also run directly from a terminal:

```powershell
.\tools\setup_local.ps1 -Python python
# Verify bundled files/skills/examples without creating a database or contacting a provider:
.\tools\setup_local.ps1 -Python python -CheckOnly
# Another free port; initial example applies only if the database is absent:
.\tools\setup_local.ps1 -Python python -Port 8875 -Example retail -NoBrowser
```

No third-party runtime packages are installed. Developer tests are separate: create a development environment with pip and install `.[test]`, then `python -m pytest -q`. Do not assume the minimal no-pip runtime contains pytest.

## macOS / Linux

From the complete extracted source folder:

```sh
python3 -m tools.local_setup --check
python3 -m tools.local_setup --start --open-browser
```

This creates `.venv` without pip, uses the same stdlib app and binds to 127.0.0.1. If Python's `venv` module is absent, install the matching system venv package through your normal OS setup. Commands are portable, but actual platform results belong to the release verification report.

## Data, existing databases and recovery

Default database: `.runtime/local/state.sqlite`, relative to the extracted source folder. Recovery copies created by the app stay in `state.sqlite.backups/`; optional bot metadata is `telegram-control.sqlite` in the database's parent directory. Back up private financial data separately from source. Opt-in remembered credentials use `.local-connections.dpapi` beside the database; this is tied to the current Windows account and is not a portable credentials backup. Re-enter credentials on a different computer/account. These runtime files are intentionally absent from distributed source ZIPs.

To continue a pre-existing local database, stop its old server first, keep a private backup, and use the exact database path:

```powershell
.\tools\setup_local.ps1 -Python python -Database 'C:\path\to\existing\state.sqlite'
```

The launcher does not copy VPS data, discover credentials or reset an existing database. A first run seeds a new Atlas (default), Studio or Retail example. An existing database ignores `-Example` and stays unchanged. `/demo/examples` is a read-only preview; it does not replace the local database. The original explicit Reset demo operation still restores Atlas. Use Backup & Restore for reviewed data recovery, not file replacement while running.

Only one new local-app process may open the same database through this launcher. A harmless `.runlock` file may remain after stopping; the OS lock, not file existence, controls admission. The legacy `serve` entrypoint does not acquire this new lock; never run both against the same database. This remains one trusted local owner, not production authentication or isolated local user accounts. OS users/processes with filesystem access are trusted.

## Optional live AI

All deterministic tools, labelled mock tasks and skill reports work without a key. For Web setup, open **Settings & help → API & Telegram** (`/settings/connections`) in the new local launcher:

1. Add a named profile. Choose OpenRouter, Gemini compatible, DeepSeek, Groq, Mistral or Custom. Enter the exact provider model ID, API key and, for Custom, the API base URL. A provider's native protocol may differ: this adapter uses OpenAI-compatible chat completions/tools. OpenRouter offers multiple model families through that gateway. Local compatible servers can use a loopback HTTP URL and optional key-free mode; model tool support is still required.
2. Save (no network call), then Activate. Up to 12 profiles can coexist; only one is active. There is no automatic fallback or automatic spend across profiles. Switch to Environment to use process configuration, or Disconnect AI.
3. Tick the destination/cost confirmation and Test active connection. Reload retains the same server-side status. An edit, activation or restart starts unverified; qualification and reviewed tasks must use the current configuration. Changing the active configuration clears staged Web previews and revokes pending Telegram model-task/check reviews.
4. Open Agent and explicitly run the current synthetic qualification before natural-language tasks. This is bounded evidence, not a guarantee that arbitrary questions are understood. Financial answers come from deterministic tools. Model changes do not grant additional authority.

Default is **session memory**: stopping the app forgets Web-entered keys/profiles. On Windows, **Remember** optionally saves encrypted settings with current-user DPAPI. No plaintext fallback exists; macOS/Linux Web settings are session-only. Keys/tokens are password fields and are never prefilled or returned in page HTML. A blank key on an edit keeps the existing key, but re-enter the other profile fields. Forget/delete removes local copies, not the provider's actual credential; revoke externally if needed. OS-account software/admins can still access process memory/decrypt that account's settings. See [threat boundaries](LOCAL-CONNECTIONS-0.25.0.md).

Environment remains an alternative, including the older CLI. Set these process values:

- `CASHFLOW_MODEL_API_KEY`
- `CASHFLOW_MODEL_BASE_URL`
- `CASHFLOW_MODEL_NAME` (required for chat)

Use the hidden terminal prompt for a key, with the endpoint/model identifiers from your provider account:

```powershell
.\tools\setup_local.ps1 -Python python -PromptForModelKey -ModelBaseUrl 'YOUR_COMPATIBLE_HTTPS_ENDPOINT' -ModelName 'YOUR_MODEL_ID'
```

The placeholders are not working credentials; replace endpoint/model with your own configuration. The script keeps prompted keys in process memory, passes them through environment only, restores previous process values when it exits, and saves no secret file. Never put keys in chat, URLs, screenshots or command arguments. No model request or qualification runs on startup. In the Agent UI, verify the connection and explicitly confirm the model qualification/data preview before live tasks. Provider network access and charges apply. Arbitrary model accuracy is not guaranteed; financial results come from deterministic tools.

## Optional Telegram with the same local workspace

Local Telegram is supported by this new launcher. It uses the exact local database and existing allowlist/pairing/review/outbox mechanisms. Local data persists across restart, and expiring review buttons still expire normally. The computer and app must remain on, with Internet access, for replies. You do not need a public IP or webhook.

For Web setup, use `/settings/connections`: enter the BotFather token in its password field, your private numeric user ID and optional bot username. Save makes no network call. **Test bot identity** calls only Telegram `getMe`; **Start local bot** explicitly begins polling. **Stop** must finish before editing. Changing token, owner, username or model mode requires fresh pairing and cancels old pending bot reviews/queued sends. Changing only Remember preserves the pairing. Remembered configuration is loaded on restart, but the bot stays stopped until you explicitly start it. To use natural language, select the active AI profile for the bot, then verify/qualify it with the existing separate consent steps.

**Do not run the same bot token in two long-polling processes.** If your existing bot is already polling elsewhere, use a separate BotFather bot/token for independent local testing; this setup does not stop or change another server. If you later want to move the existing bot, deliberately stop its old poller first as a separate operation.

For the alternative environment/hidden-prompt setup, local deterministic Telegram commands (without model calls):

```powershell
.\tools\setup_local.ps1 -Python python -Telegram -PromptForTelegramToken -TelegramOwnerId 'YOUR_NUMERIC_USER_ID' -TelegramBotName 'YOUR_LOCAL_BOT_USERNAME'
```

Replace the public identity placeholders; enter the actual token only in the hidden prompt. Alternatively set `FLUXORA_TELEGRAM_ENABLED=1`, `FLUXORA_TELEGRAM_BOT_TOKEN`, `FLUXORA_TELEGRAM_OWNER_ID`, optional `FLUXORA_TELEGRAM_BOT_NAME` in the terminal environment and launch with `-Telegram`. Telegram is off without that explicit flag. Only the allowlisted private account can link/act; other accounts and groups are ignored.

Open `/telegram`, acknowledge the destinations, generate a one-time code and send `/link CODE` only to your local bot. Then `/forecast`, `/plans`, `/draft ...` and `/snapshot ...` work with exact review. Preparation and owner approval stay separate. Only snapshot-save workflows can be approved through Telegram; other workflows need detailed Web review. No real payment or business-counterparty message is sent.

To also translate Telegram natural-language tasks, add `-UseTelegramModel` and configure the model environment/hidden prompt above. Run the explicit bot `/verify` and `/qualify` checks after cost confirmation. No automatic fallback, paid qualification or extra authority is added. Telegram/model traffic is online even though the workspace UI and financial computation are local.

## Troubleshooting

- **Windows backup/reset fails in a deeply nested folder:** use a short, writable extraction and database path, for example `C:/Fluxora`, and stop the app before moving private data. A 271-character synthetic backup path failed with the tested Windows SQLite build while a 117-character control opened successfully. Enabling long paths for Python does not establish that the SQLite build supports the same paths. This release does not automatically move or reset data.
- **Python not found:** install Python 3.10+; or pass `-Python 'C:\full\path\python.exe'`.
- **Address already in use:** reuse the already-running app or select another `-Port`. No process is killed automatically. Do not run a second process on the same database.
- **Host/Origin refused:** use the exact displayed `http://127.0.0.1:PORT` address and reload its form. Another hostname, foreign origin or stale CSRF is refused.
- **Locked environment/data folder:** use a writable extracted folder outside the ZIP. Security software can block script/process execution; inspect the local restriction rather than disabling protection globally.
- **Missing styles/images/skills:** extract the whole archive. Do not copy only the Python entrypoint.
- **Telegram not connected:** confirm Web Start or the explicit launch flag, correct private allowlist/token, Internet access and no other poller/webhook. Tokens are never returned by UI. Never reuse the actively polling VPS bot for an independent local test.
- **Remember unavailable/locked:** non-Windows systems use session memory. A damaged vault or different Windows account leaves AI disconnected without overwriting the file; explicitly Forget saved settings before remembering replacements. Re-enter keys in their password fields. This does not repair or recover a lost key.
- **No AI key:** use deterministic reports and labelled mock tools. Live verification stays pending until configured; setup success is not model success.
- **Backup/move:** stop the app, copy the private database and its backup/control folder through your own secure file transfer; never place these in a public submission ZIP.

The website is intentionally loopback-only. No public deployment, tunnel, production login or real bank/payment integration is created by this setup.
