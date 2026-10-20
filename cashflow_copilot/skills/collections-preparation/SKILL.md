---
name: collections-preparation
description: Review invoice collection priorities and prepare exact early-payment request drafts for later review.
---

# Collections Preparation

Use collections for recorded collection priorities, promises for recorded commitments and party_statement only when the exact party is supplied. Expected dates and promises are conditional; neither is a receipt.

An early-payment request needs the exact receivable ID, ISO currency, positive decimal amount and ISO requested date. If any field is absent, clarify it rather than selecting a likely invoice, entire outstanding amount or relative date. Preserve the user's exact amount; code validates precision and bounds.

Use action_proposal with request_early_payment to prepare a DRAFT. Do not send a reminder, invent a reply, approve the draft or record cash. Preparation of create_promise, review_promise or set_collection_hold is pending-only and still requires explicit user facts and separate owner review; no promise may be invented from an invoice.

Show the recorded source links and missing facts supplied by deterministic reports. Keep proposed terms, owner approval, acceptance, observation and reconciliation distinct. Use another skill for supplier bills or reconciliation.
