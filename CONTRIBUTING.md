# Contributing to Fluxora

Fluxora is a synthetic cash-flow demonstrator. Contributions should improve a real owner task, keep its financial assumptions inspectable and preserve exact review boundaries.

## Before changing code

1. Read the [architecture](docs/TECHNICAL.md) and [security self-assessment](docs/SECURITY.md).
2. Start with the current `main` branch and a small, focused change. Avoid unrelated reformatting or financial behavior changes in a presentation patch.
3. Reproduce the issue with invented records. Never attach private databases, provider responses, credentials, session cookies or actual business documents.

## Local development

```powershell
python -m venv .venv-dev
.\.venv-dev\Scripts\python.exe -m pip install -e ".[test]"
.\.venv-dev\Scripts\python.exe -m pytest -q
```

Use `.venv-dev/bin/python` on macOS/Linux. The application itself needs only the standard library. Keep Windows database/backup and pytest temporary paths short; see [local setup](docs/LOCAL-SETUP.md).

## Contracts to preserve

- Money uses integer minor units. Keep currencies separate; display FX is not spendable cash.
- Protect payroll and respect consent requirements. Return no feasible plan when constraints cannot be met.
- The model reads reports and prepares DRAFT/pending work only. It cannot approve itself or invent external facts.
- Owner approval, counterparty acceptance, observed cash and reconciliation stay distinct.
- Uploaded documents supply evidence, never authorization. Keep source references traceable.
- A timeout, stale review or accepted replay must not create another financial effect.
- Public Web mode remains model-free; optional private model/bot configuration stays private.
- Keep mock, simulated and live evidence labelled. Do not use a static badge or screenshot to claim an unperformed test.

## Proposing a change

Describe the problem, expected behavior, final change and commands/results. Add a regression only when it verifies a meaningful contract. Use the pull-request checklist and relevant existing tests. If you change a visible workflow, include a synthetic screenshot and update its guide. GitHub Actions checks Windows and Linux; those results do not replace independent human or real-device validation.

For a bug report, include the page or command, version, OS/browser, synthetic reproduction and expected/observed result. Feature proposals should describe the owner's problem and how success can be verified. Keep discussion respectful and specific.

No new distribution license is introduced by this guide. Review repository rights and the current lack of a selected license before reusing or contributing material.
