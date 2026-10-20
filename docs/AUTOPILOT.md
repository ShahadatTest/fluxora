# Autopilot: automatic preparation, separate owner decisions

Autopilot is an opt-in background workflow for the synthetic workspace. It does not operate a real bank account, authenticate production business users, send business-counterparty messages or execute payments. It reuses the existing deterministic forecasting, missing-fact, adaptive planning and DRAFT/workflow tools. Scheduled checks make **zero model calls**.

## Start locally

Run `python -m tools.local_setup --start` and open `/autopilot`. The persistent local launcher starts the worker; the legacy bare web entrypoint displays setup guidance instead. Local data and saved enabled policies survive restart. The computer/app must remain running. Telegram still needs its separate private configuration and explicit bot start; remembering a token does not auto-start local polling.

1. Select a check interval and periodic summary interval. Summary periods are elapsed durations, not a promised calendar-time delivery.
2. Choose monitor-only, earlier collections, permitted deferrals or the combined bounded strategy.
3. Enter a maximum **per DRAFT** separately for each account currency. Zero disables new DRAFTs in that currency; existing owner constraints still apply. New currencies require an explicitly reviewed limit and initially display zero.
4. Choose whether financial changes should prepare a forecast snapshot for separate approval.
5. Optionally link your own configured private Telegram account and select delivery. Other public visitors cannot access it.
6. Review and save the enabled policy. This is a persistent authorization for read/DRAFT/pending preparation, not approval of proposed financial terms, counterparty acceptance or payment evidence.

## What runs automatically

- Changed recorded financial inputs trigger per-currency forecasts, missing-fact/source checks and three existing bounded strategies. The ledger's `as_of` date does not advance itself. Invoice overdue status uses a recorded contractual due date; missing dates are not guessed.
- Only a financially sufficient selected strategy, within every currency limit and existing source/protection/overlap rules, prepares its complete DRAFT batch. Over-limit plans are not partially prepared. The planner is bounded, not an exhaustive optimum; an insufficient result is reported honestly.
- Optional snapshots are prepared as pending owner workflows. No snapshot is saved until its separate decision.
- Action/workflow changes produce follow-up. Unchanged data produces only the periodic digest. Autopilot's own new drafts and report metadata do not retrigger financial preparation.
- `/autopilot` shows private status. `/autopilot on` enables/resumes the saved policy and opts into its notifications; a new policy remains monitor-only until strategy/limits are explicitly configured. `/autopilot off` pauses future preparations and cancels unsent Autopilot notifications.
- While ON, explicitly submitted supported private Telegram tasks run reports or prepare DRAFT/pending work without a preparation-review click. Natural-language entry still needs a configured, qualified provider; model prose is discarded. With it OFF, the existing preparation-review flow remains. Final approval is always separate.

The same financial rules apply to a cloud or compatible local task-entry model. Optional local provider setup uses existing Custom OpenAI-compatible loopback settings; this release does not claim a new actual Ollama/LM Studio evaluation. The background worker needs no model/provider key. Telegram delivery always needs Internet access.

## Owner decisions and notifications

The currently paired private owner may receive summaries and exact review cards. **Approve DRAFT** records only owner approval of the displayed unchanged terms. **Reject** cancels that unapproved DRAFT. Neither sends a message, changes supplier consent, records a receipt or moves cash. A separate snapshot approval saves only that exact local forecast snapshot. Other workflow/source/consent operations require their detailed Web review.

Buttons bind business, pairing generation, exact terms/version, current review revision and expiry. A later change can make a button stale; it must not authorize new terms. A stale snapshot is directed to Web rather than reissued as a valid approval. Documents never grant authority. The system does not infer acceptance, payment or reconciliation from approval.

Delivery keys prevent re-queuing the same notification/card during recovery. Queued/prepared is not confirmed delivered or read. Unknown sends are retained as uncertain and are not automatically retried. Pausing cancels unsent Autopilot messages; already sent messages cannot be recalled. Private commands remain explicit owner requests. Existing `/alerts on` is an independent notification opt-in; it does not enable an Autopilot DRAFT strategy.

## Jobs, restart and limits

Policies and scheduling metadata live in `autopilot-control.sqlite` beside the server/local base database. Financial effects and their `autopilot_runs` result journal commit atomically in the selected ledger database. If the process stops before the scheduling index is updated, a recovered claim reads that journal and finishes bookkeeping without repeating effects.

Only one queued/running job per workspace is admitted. Claims have a 30-second recovery lease; local analysis has a ten-second commit deadline. A stale ledger or changed state generation, lost claim, policy change/pause or stopping worker discards candidates before saving. Failed jobs retain their identity and do not silently become new requests. New financial data or a new explicitly saved policy can trigger another check.

Limits include 8 DRAFTs per batch, 48 admitted jobs per workspace per rolling 24 hours, 256 registered policies, 1,024 shared indexed jobs, four workspaces per worker cycle, 300 invoice/bill records and a 1–120 day forecast window. Public visitor storage/admission limits still apply. Displayed history retains approximately the latest 30 jobs; a separate persisted attempt counter prevents pruning/restart from resetting the rolling allowance. Resource capacity can delay or block work rather than certify completion.

Hosted visitor workspaces still expire after 24 hours; monitoring/pairing does not extend them. Expired visitor policies and indexed job summaries are pruned. Use the persistent local launcher for a durable private workspace. Financial files, scheduling data and backups are plaintext under the trusted OS/server account; this is not a production confidentiality boundary.

See [validation](VALIDATION.md) for measured commands/results, stub versus live transport/model checks, browser limitations and independent-human-study status.
