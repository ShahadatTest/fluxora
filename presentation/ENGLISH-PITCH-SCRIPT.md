# Fluxora by SKLab: English pitch

12 main slides and 3 hidden Q&A appendix slides. Planned pitch: 8:30, 1060 spoken words. Timings are targets and need rehearsal. This is a presentation script, not a recorded video. The PDF includes all 15 pages.

## 1. Fluxora

00:00 to 00:20 (20 seconds)

We are SKLab, and this is Fluxora, our cash-flow decision workspace for small businesses. It connects deterministic financial calculations with traceable evidence and an agent that prepares work for owner review. We will show one synthetic business case for Topic E, SME Finance Copilot.

## 2. The cash timing problem

00:20 to 01:00 (40 seconds)

A business can look profitable and still run short of cash before payroll. Our Atlas example starts with ten thousand US dollars and one thousand euros. Those balances stay separate. The owner needs to know whether the next collection arrives before obligations fall due, and which assumptions support that answer. An unpaid invoice contributes to a conditional projection, but it does not establish an observed receipt. This case uses invented records from the bundled fixture, rather than the finances of a real company.

## 3. The owner workspace

01:00 to 01:35 (35 seconds)

The workspace groups tasks around the owner’s day. Evidence and Ledger holds source records. Forecast and Scenarios examine cash timing. The approval queue and action history keep preparation separate from a decision. These are dedicated pages, with documentation and a guided start for someone seeing the product for the first time. The public judge demo provides an expiring synthetic workspace for each visitor. A private local installation keeps the selected SQLite database across restarts. The financial reports work without a model key.

## 4. A forecast the owner can inspect

01:35 to 02:20 (45 seconds)

This is an actual forecast report from the local application. The baseline reaches a minimum of six thousand US dollars on October twenty-eighth, above the recorded three-thousand-dollar floor. The report exposes assumptions and provides links to source records. Unknown intraday timing uses outflows before inflows on the same day, so a closing balance cannot hide that ordering risk. The euro forecast stays separate. These results come from integer minor-unit calculations in Python. They remain conditional on the dates and amounts supplied to the ledger.

## 5. A seven-day delay creates a cash gap

02:20 to 03:25 (65 seconds)

The scenario delays invoice inv-client-001 by seven days while preserving the other recorded assumptions. The green line shows the baseline and the red line shows the delayed collection. The dashed line is the owner’s three-thousand-dollar floor. The delayed minimum becomes negative five hundred dollars, leaving a worst shortfall of three thousand five hundred dollars against that floor. This chart is editable and uses values freshly computed by the same deterministic engine. A scenario lets the owner compare timing before changing any record. It does not record a receipt or establish counterparty consent. The application protects payroll, checks feasibility, and can report that no allowed plan meets the constraints. The euro balance cannot fill this dollar gap through a display exchange rate.

## 6. One Send prepares supported work

03:25 to 04:20 (55 seconds)

The optional model interprets an explicit instruction. Here the user supplies an invoice, amount, currency and date. The current interface uses one Send action. Local code validates the selected task and prepares a report, a DRAFT, or pending work. It displays the selected task and exact terms. Preview before sending remains available in Options. Final owner approval stays separate. The archived screenshot shows the earlier disconnected local interface, rather than a new live completion. Separate live qualification passed eight model interpretation cases and five local safety guards. These finite tests do not prove unrestricted language accuracy.

## 7. The owner retains approval authority

04:20 to 05:10 (50 seconds)

Approval is a separate authority boundary. The agent can read supported reports and prepare DRAFT or pending work. It cannot approve itself. The owner decides on the exact reviewed local scope, and a separate executor applies only that scope. Counterparty acceptance still needs its own supported event. Observed receipts or payments need the source and explicit confirmation required by the domain rules. Reconciliation then performs a separate match. This distinction matters because approval cannot prove that a customer agreed or that money arrived. Source documents provide evidence, and never grant an agent authority to act.

## 8. Evidence stays attached to the decision

05:10 to 05:50 (40 seconds)

The evidence workflow connects a forecast assumption or proposed action to its source. Import staging gives the owner a chance to inspect records before accepting them. Settlement observations, corrections and reconciliation keep their own audit history. The illustration represents this review boundary rather than a real document or payment. A useful audit trail helps someone inspect a decision, but it does not authenticate every uploaded source or guarantee that an owner’s attestation is true. That limitation remains part of the product’s threat boundary.

## 9. Telegram updates and owner decisions

05:50 to 06:35 (45 seconds)

Judges can inspect Telegram behavior in the browser without joining our private bot. The lesson uses separate synthetic data and clearly labels simulated transport. It supports forecast, a sample DRAFT and a separately reviewed snapshot. In a private deployment, Autopilot can queue updates and exact Approve DRAFT or Reject buttons for the paired owner. Real Telegram API acknowledgments confirm delivery requests succeeded. Callback approval and rejection passed against a simulated transport, including restart and stale-button checks. These are different types of evidence. Approving a DRAFT does not send a counterparty message or record a payment.

## 10. Scheduled checks use local code

06:35 to 07:15 (40 seconds)

Autopilot runs on Python and SQLite. The enabled private workspace checks every five minutes. Changed recorded financial inputs trigger deterministic analysis. The selected strategy can prepare only an eligible DRAFT batch within the owner limits and a separately approved pending snapshot. Unchanged data produces a timed digest. Scheduled checks make zero AI calls. The model interprets explicitly submitted language tasks. It does not independently reason about new situations in the background. Hosted execution runs on the VPS. Local installation runs on the owner’s machine, and Telegram needs internet access. Persisted policies and effect identities survive restart.

## 11. Verification and the remaining limits

07:15 to 08:00 (45 seconds)

The fresh complete local suite passed two thousand two hundred eleven tests. It uses synthetic records, local HTTP stubs and mocked providers. The disposable VPS canary ran the actual scheduler, prepared two DRAFTs, saved a separately approved snapshot and handled exact synthetic approval and rejection. An actual container restart preserved the results without duplicate DRAFTs. Earlier live qualification passed eight real model cases and five local guards. A fresh public forecast and replay also passed. The user reports that a fresh person completed the walkthrough without finding problems. We have no detailed study log. New rendered mobile checks remain blocked because the browser tool is disabled. Broad language accuracy and real-business security review remain open.

## 12. Fluxora by SKLab

08:00 to 08:30 (30 seconds)

Fluxora connects cash timing with inspectable evidence and constrained preparation of the next action. Deterministic tools supply financial results. Autopilot handles scheduled checks and prepares work within recorded limits. The owner retains approval authority. Judges can start with the guided demo and the simulated Telegram lesson, or run the complete source locally. Broader language evaluation and a real-business security review will guide further work. Thank you.

## 13. Feature coverage

Q&A appendix, outside planned pitch

The application groups supported work into owner workflows. Autopilot adds persisted monitoring and bounded preparation. Model interpretation applies only to explicit supported tasks. A feature count does not prove a model understands every instruction.

## 14. Threat boundaries and known limitations

Q&A appendix, outside planned pitch

The model cannot approve, send counterparty messages or move money. Autopilot uses explicit limits and rejects stale or late work before committing. Owner approval, counterparty acceptance and observed payment stay distinct. Real private Telegram notifications need an existing paired owner. Public demo visitors have isolated, expiring synthetic workspaces. Local storage and operating-system administrators remain trusted. Automated checks and user feedback do not constitute a production security certification.

## 15. Demo access and reproducibility

Q&A appendix, outside planned pitch

These are the judge-facing entry points. Direct financial reports and the simulated Telegram lesson need no provider key or Telegram account. For local setup, use the short writable folder described in the repository. The repository includes the full test commands, documentation and known limitations. The linked official guidance distinguishes a maximum ten-minute final presentation from a maximum five-minute demo video. Do not concatenate a separate demo video with the complete spoken deck inside a ten-minute presentation slot.

