---
name: payroll-bills-review
description: Inspect protected payroll and upcoming obligations, account funding and permitted nonprotected bill drafts.
---

# Payroll and Bills Review

Use account_funding for account-specific obligation coverage, calendar for dated obligations, constraints for recorded limits and cash_accounts for account positions. A total across accounts does not establish spendable payroll funding in the required account.

Protected payroll cannot be deferred, including through a supplier-extension draft. Consent requirements and unpaid/deferrable bounds remain code-enforced. Never treat owner approval as counterparty consent or a projected transfer as observed cash.

A supplier extension or permitted operations deferral needs the exact eligible outflow ID, currency, decimal amount and ISO date. Ask for missing terms. Prepare only request_supplier_extension or defer_ops_expense DRAFTs; do not invent an acceptance or execute a payment.

An assign_forecast_route workflow uses exact supplied routing facts and remains pending review. A saved forecast needs an exact user label. Missing account routing should remain an explicit funding limitation rather than a guessed assignment.
