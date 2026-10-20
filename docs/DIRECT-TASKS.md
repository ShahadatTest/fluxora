# Run a guided task in one submission

Open **Agent** at `/agent/tasks`, describe a supported task and select **Send**. The provider destination and data notice appear beside the controls before submission. This authorizes a read-only report or preparation of DRAFT/pending work. It never authorizes final approval, external sending or payment.

The server sends the typed question and static task definitions for one bounded interpretation, discards all model prose, validates the instruction and runs deterministic local tools. It shows the selected task, exact terms, computed answer and evidence in the saved result. A schema-valid instruction can still misunderstand intent: inspect that result, especially before approving proposed work.

Choose **Options → Preview before sending** instead to retain the separate outbound-data preview and exact interpretation confirmation. Floating guidance still makes no model call: **Continue in Agent** opens an editable task with the same Send control, optional preview and disclosure.

Examples for the bundled Atlas fixture:

- `Show the baseline cash forecast for every currency. Read only.`
- `Prepare an early-payment request draft for inv-client-001, USD 100 on 2026-10-22. Owner review only.`

Use record IDs that exist in your current workspace. Missing currency, amount, date or source facts can require clarification. Unsupported/invalid instructions and forbidden approval/payment requests stop without creating work. Documents remain evidence, never authorization. Protected payroll and currency separation remain enforced by the deterministic tools.

The one-step route shares a 30-second deadline across interpretation and local execution. No background worker or automatic retry is scheduled. A changed ledger invalidates candidate work. Reposting a saved request retrieves its historical SQLite result before another provider call, even after server restart; opening that result does not recompute it against newer facts. A different question must use a fresh request identity.

Public use retains the shared durable daily allowance, per-visitor hourly limit, global model-route limit and one in-flight adapter. Both translation routes are covered, including URLs with a query string. Duplicate submissions still count against request-rate limits; saved-result GET links remain the appropriate way to inspect history. See [public AI controls](PUBLIC-AI.md).

For the fastest supported reports, use the report links below the composer: these bypass the provider entirely and are explicitly labeled local deterministic reports. Provider latency and availability still affect natural-language tasks; there is no universal latency or language-accuracy guarantee. Do not enter credentials in a task.

See [measured validation](VALIDATION.md) for separate automated, HTTP-stub, mocked-provider, actual live-model and browser-check evidence.
