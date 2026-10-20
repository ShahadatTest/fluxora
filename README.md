# Fluxora

**Cash-flow decisions for small businesses, with traceable evidence and explicit owner review.**

Fluxora is an English-language SME cash-flow assistant built for Topic E of the 2026 Shenzhen International FinTech Competition. Version **0.25.9** uses Python, SQLite and deterministic calculations with integer minor units. All bundled records are synthetic. This is a demonstration, not a production banking or accounting service.

[Public demo](https://fluxora.sklab.cc/welcome) · [Judge tour](https://fluxora.sklab.cc/start) · [All feature pages](https://fluxora.sklab.cc/workspace/features) · [Documentation](docs/README.md)

## Start locally

Requires Python 3.10+ with SQLite and venv. The runtime uses the Python standard library; no API key or third-party runtime package is required for direct reports.

On Windows, extract the complete repository and double-click **START_FLUXORA.cmd**, or run:

```powershell
python -m tools.local_setup --check
python -m tools.local_setup --start --open-browser
```

On macOS/Linux, use `python3` in the same commands. Windows setup was exercised; current real-device macOS/Linux behavior has not been verified. Open <http://127.0.0.1:8765/welcome> for the website or <http://127.0.0.1:8765/app> for the workspace. Stop with Ctrl+C. Local data persists at `.runtime/local/state.sqlite`; restart with the same path to retain it. The launcher creates a no-pip virtual environment and binds to loopback.

On Windows, use a short, writable extraction/data path such as `C:/Fluxora`. Deeply nested paths can exceed the SQLite build's supported path length and make backups fail. See [local setup](docs/LOCAL-SETUP.md) for existing databases, another port, API profiles, Telegram and troubleshooting. Never upload runtime databases, remembered settings or backups.

## What you can explore

| Area | Capabilities |
| --- | --- |
| Cash visibility | Separate currency balances, daily forecasts, account availability, payroll funding and cash-floor alerts |
| Planning | Timing and partial-collection scenarios, affordability checks, bounded plan comparison and honest infeasibility results |
| Evidence and records | Staged imports, traceable sources, receivables, bills, loans, payment observations and corrections |
| Review and recovery | DRAFT actions, exact owner approval, history, reconciliation, exports, backups and replay protection |
| Agent | Direct deterministic reports, six bundled task skills and optional constrained model interpretation |
| Telegram | Account-free browser practice; separately configured private companion with allowlist, pairing and reviewed buttons |

The [judge quickstart](docs/JUDGE-QUICKSTART.md) explains the public path. The public feature directory currently lists 64 dedicated pages; page and operation counts are different measures. Not every operation has been tested through arbitrary live language.

## AI and Telegram boundaries

- Direct reports and owner forms work without a model. Public visitor Web sessions are model-free.
- The model can read supported reports and prepare DRAFT or pending work. It cannot self-approve, invent receipts or execute real payments.
- Owner approval, counterparty acceptance, observed cash and reconciliation are separate facts. Documents supply evidence, never authorization.
- Deterministic tools produce authoritative financial results. Optional advanced-agent prose remains unverified; a schema-valid interpretation can still misunderstand a question.
- Payroll protections, consent requirements and separate currencies remain enforced. Display-only FX creates no spendable cash.
- [Telegram browser practice](https://fluxora.sklab.cc/telegram/demo) is simulated and needs no bot account. Real private Telegram messages require explicit local/operator setup and are about synthetic records, not messages to business counterparties.

For private local model setup, open **Settings & help → API & Telegram**. Multiple compatible profiles can be saved, with one active profile. An alternative is process environment configuration:

```text
CASHFLOW_MODEL_API_KEY
CASHFLOW_MODEL_BASE_URL
CASHFLOW_MODEL_NAME
```

These are variable names, not values. Enter credentials in the private local password fields or hidden setup prompt. Verification and synthetic qualification are separate, explicit potentially billed actions. Restart starts verification unverified. Hosted settings are a locked preview and disclose no private configuration. See [model setup](docs/MODEL-SETUP.md) and [Telegram setup](docs/TELEGRAM.md).

## Development and tests

Use a separate development environment with pip:

```powershell
python -m venv .venv-dev
./.venv-dev\Scripts/python.exe -m pip install -e ".[test]"
./.venv-dev\Scripts/python.exe -m pytest -q
```

On macOS/Linux, the corresponding executable is `.venv-dev/bin/python`. Tests use synthetic fixtures, fake transports and local HTTP stubs. Ordinary pytest does not establish live provider behavior. Explicit tools under `tools/` perform separately opted-in model evaluations and may incur charges.

See [validation](docs/VALIDATION.md) for actual results, their versions and remaining checks.

## Repository map

```text
cashflow_copilot/   Application, deterministic engine, UI, assets and bundled skills
tests/             Automated synthetic and regression tests
tools/             Local setup and explicit evaluation utilities
examples/          Synthetic import and workflow examples
docs/              Current setup, architecture, security and judge guidance
deploy/vps/        Optional Docker deployment and operational instructions
```

Read [architecture](docs/TECHNICAL.md), [security self-assessment](docs/SECURITY.md), [asset provenance](docs/BRAND-ASSETS-0.20.1.md) and [deployment](deploy/vps/README.md). A fresh clone has no provider credentials, private bot state, financial database or virtual environment.

## Limitations

No real bank connection, payment rail, lender integration or production identity system is provided. Forecasts depend on recorded inputs and conditional assumptions; source references do not authenticate documents. Public hosted workspaces expire and have finite admission/storage limits. Provider availability, costs and model language accuracy remain external variables. Focused browser tests are not an independent user study, full accessibility review or security certification.

The project name/mark have not received trademark clearance. No license has been selected automatically; review distribution rights before adding one. Competition eligibility, registration and acceptance are separate from this repository. No entry is submitted by uploading this code.
