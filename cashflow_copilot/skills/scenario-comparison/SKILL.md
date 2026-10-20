---
name: scenario-comparison
description: Compare conditional what-if forecasts and bounded allowed plans without changing recorded cash.
---

# Scenario Comparison

Use forecast with explicitly supplied scenario parameters for a simple what-if, scenario for a compound case, plans for the bounded allowed strategies, and affordability for a specified proposed expense. Do not silently add a condition or change recorded dates.

Ask for exact invoice identifiers, date shifts, percentages, currency and amounts where the operation requires them. Preserve integer minor-unit handling in code; copy user-provided major-unit decimal strings without converting them yourself.

A plan can be conditional on counterparty acceptance or later payment. Keep those conditions visible in the deterministic result. Report no feasible plan honestly when the tool finds none; the bounded search does not establish global optimality. Protect payroll and treat display FX as display only.

Saving a scenario or forecast prepares a pending workflow using exact supplied terms and label. It does not apply the scenario to the ledger or approve any action. The user reviews the selected task before local execution.
