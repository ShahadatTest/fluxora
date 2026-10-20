"""The ledger: validated fixture state plus recorded reality.

- Events carry stable IDs; re-applying one is idempotent.
- Receipts carry idempotency keys; a repeat is a no-op, a conflicting reuse
  is an error. Partial payments reduce the invoice's remaining balance;
  the original invoice is never recounted. Idempotency keys are scoped to
  (business_id, operation): every stored receipt is stamped with the
  ledger's business_id and cross-business reuse is refused; the SQLite
  receipts table uses PRIMARY KEY (business_id, id).
- Caller authentication + business authorization (authz.require_access)
  is the FIRST step of every mutating entry point, before any idempotency
  short-circuit.
- The opening snapshot includes prior settled events; rolling the as-of
  date forward re-bases opening balances so settled receipts are not
  counted twice.
"""

from __future__ import annotations

from datetime import date

from . import actions as actions_mod
from . import authz as authz_mod
from .errors import (
    AuthorizationError,
    CashflowError,
    IdempotencyConflict,
    OverpaymentError,
)
from .fixture import seed_validated
from .forecast import forecast
from .models import (
    Account,
    Action,
    Allocation,
    BusinessEvent,
    Document,
    Evidence,
    FxQuote,
    Outflow,
    OwnerConstraints,
    Receivable,
    Receipt,
)
from .money import parse_rate


def _d(value) -> date:
    y, m, d = (int(p) for p in value.split("-"))
    return date(y, m, d)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_id(value: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "-" for c in value)[:64]


class Ledger:
    def __init__(self):
        self.business_id = ""
        self.business_name = ""
        self.base_currency = "USD"
        self.timezone = "UTC"
        self.as_of: date = date(2026, 10, 16)
        self.horizon_days = 30
        self.accounts: dict[str, Account] = {}
        self.receivables: dict[str, Receivable] = {}
        self.outflows: dict[str, Outflow] = {}
        self.receipts: dict[str, Receipt] = {}
        self.events: dict[str, BusinessEvent] = {}
        self.evidence: dict[str, Evidence] = {}
        self.documents: dict[str, Document] = {}
        self.actions: dict[str, Action] = {}
        self.fx_quotes: list[FxQuote] = []
        self.applied_event_ids: set[str] = set()
        self.constraints = OwnerConstraints()
        # Evidence created by explicit runtime events (never by the seed).
        # Counterparty consent for a date change is only valid when the cited
        # evidence is registered here; consent is never pre-granted.
        self.runtime_evidence_ids: set[str] = set()
        # outflow_id -> evidence_id of the explicit acceptance event.
        self.consent_map: dict[str, str] = {}
        # Ledger-level audit trail (persisted): every financial change.
        self.audit: list[dict] = []
        # Demo-workflow checklist flags (persisted): {"inspected": True, ...}.
        self.demo_checks: dict[str, bool] = {}
        self.collection_date_sources: dict[str, str] = {}
        self.receipt_reviews: dict[str, dict] = {}
        self.owner_operations: dict[str, dict] = {}
        self.receipt_inbox: dict[str, dict] = {}
        self.receipt_adjustments: dict[str, dict] = {}
        self.receipt_review_archive: dict[str, dict] = {}
        self.forecast_snapshots: dict[str, dict] = {}
        self.invoice_due_sources: dict[str, str] = {}
        self.recurring_bills: dict[str, dict] = {}
        self.recurring_occurrences: dict[str, dict] = {}
        self.duplicate_reviews: dict[str, dict] = {}
        self.split_receipts: dict[str, dict] = {}
        self.fact_checks: dict[str, dict] = {}
        self.entry_records: dict[str, dict] = {}
        self.split_adjustments: dict[str, dict] = {}
        self.cash_movements: dict[str, dict] = {}
        self.statement_batches: dict[str, dict] = {}
        self.statement_reviews: dict[str, dict] = {}
        self.payment_acknowledgements: dict[str, dict] = {}
        self.payment_pair_reviews: dict[str, dict] = {}
        self.date_corrections: dict[str, dict] = {}
        self.instalment_proposals: dict[str, dict] = {}
        self.scenarios: dict[str, dict] = {}
        self.setup_receipts = {}
        self.payment_promises = {}
        self.unallocated_refunds = {}
        self.evidence_attachments = {}
        self.attachment_import_receipts = {}
        self.statement_balance_checks = {}
        self.credit_transfers = {}
        self.loans = {}
        self.forecast_routes = {}
        self.statement_profiles = {}
        self.agent_workflows = {}
        for name in ('party_register', 'party_links', 'account_details', 'cash_defaults',
                     'movement_accounts', 'statement_accounts', 'internal_transfers', 'cash_restrictions'):
            setattr(self, name, {})

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def log_audit(self, event: str, actor: str, detail: str = "") -> None:
        self.audit.append({"at": _utcnow_iso(), "event": event,
                           "actor": actor, "detail": detail})

    # ------------------------------------------------------------------
    # Runtime evidence and explicit counterparty acceptance
    # ------------------------------------------------------------------

    def record_runtime_evidence(self, evidence: Evidence, actor: str,
                                ctx=None) -> str:
        """Register evidence produced by an explicit runtime event.

        Seed-fixture evidence is never registered here: being present in this
        registry is what makes an evidence item usable as counterparty
        consent for an outflow date change.
        """
        # Mutation boundary: caller authentication + business authorization
        # happen before anything is recorded.
        authz_mod.ensure(ctx, self)
        if evidence.id in self.evidence and evidence.id not in self.runtime_evidence_ids:
            raise CashflowError(
                f"evidence {evidence.id!r} already exists from the seed; "
                "runtime events must use a distinct evidence id"
            )
        self.evidence[evidence.id] = evidence
        self.runtime_evidence_ids.add(evidence.id)
        self.log_audit("runtime_evidence_recorded", actor,
                       f"{evidence.id} kind={evidence.kind}")
        return evidence.id

    def record_counterparty_acceptance(self, outflow_id: str, new_date: date,
                                       action_id: str, actor: str,
                                       ctx=None) -> str:
        """Explicit event: the sandbox counterparty accepts a date change.

        Creates and registers the consent evidence that a plan amendment may
        cite. Without calling this, no supplier date change can be feasible —
        consent is never auto-granted when the demo starts.
        """
        # Mutation boundary first: authenticate the caller and authorize the
        # business before any state changes.
        authz_mod.ensure(ctx, self)
        out = self.outflows.get(outflow_id)
        if out is None:
            raise KeyError(f"unknown outflow {outflow_id!r}")
        if not out.counterparty_consent_required_for_change:
            raise CashflowError(
                f"{outflow_id} does not require counterparty consent; "
                "an acceptance event is not applicable"
            )
        evid = Evidence(
            id=f"ev-{_sanitize_id(outflow_id)}-accept-{_sanitize_id(action_id)}",
            kind="synthetic_counterparty_message",
            summary=(
                f"Simulated sandbox response: counterparty agrees to move "
                f"{outflow_id} ({out.amount_minor} minor) to "
                f"{new_date.isoformat()}. Recorded as an explicit runtime "
                f"event for action {action_id}; this is simulated, never real."
            ),
        )
        eid = self.record_runtime_evidence(evid, actor)
        self.consent_map[outflow_id] = eid
        self.log_audit("counterparty_acceptance_recorded", actor,
                       f"{outflow_id} -> {new_date.isoformat()} evidence={eid}")
        return eid

    def consent_evidence_id(self, outflow_id: str) -> str | None:
        """The explicit acceptance evidence for an outflow, if one exists."""
        return self.consent_map.get(outflow_id)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_fixture(cls, data: dict) -> "Ledger":
        led = cls()
        biz = data["business"]
        led.business_id = biz["id"]
        led.business_name = biz["name"]
        led.base_currency = biz["base_currency"]
        led.timezone = biz.get("timezone", "UTC")
        led.as_of = _d(biz["as_of"])
        led.horizon_days = int(biz["horizon_days"])
        for acc in data["accounts"]:
            led.accounts[acc["id"]] = Account(
                id=acc["id"], currency=acc["currency"],
                opening_minor=int(acc["opening_minor"]),
                floor_minor=int(acc["floor_minor"]),
                evidence_id=acc.get("evidence_id"),
            )
        for ev in data["evidence"]:
            led.evidence[ev["id"]] = Evidence(ev["id"], ev["kind"], ev.get("summary", ""))
        for fx in data.get("display_fx", []):
            led.fx_quotes.append(FxQuote(
                from_ccy=fx["from"], to_ccy=fx["to"],
                rate=parse_rate(fx["rate"]), as_of=_d(fx["as_of"]),
                source=fx.get("source", "")))
        for rec in data["receivables"]:
            led.receivables[rec["id"]] = Receivable(
                id=rec["id"], currency=rec["currency"],
                original_minor=int(rec["original_minor"]),
                expected_date=_d(rec["expected_date"]) if rec.get("expected_date") else None,
                evidence_id=rec["evidence_id"],
                due_date=_d(rec['due_date']) if rec.get('due_date') else None,
                counterparty=rec.get('counterparty', ''), invoice_reference=rec.get('invoice_reference', ''),
            )
            if rec.get("received_minor"):
                # Fixture-declared prior receipts are settled history.
                led.receivables[rec["id"]].allocations.append(Allocation(
                    receipt_id=f"seed-{rec['id']}",
                    amount_minor=int(rec["received_minor"]),
                    date_received=led.as_of, observed=True))
        for out in data["outflows"]:
            led.outflows[out["id"]] = Outflow(
                id=out["id"], currency=out["currency"],
                amount_minor=int(out["amount_minor"]), date=_d(out["date"]),
                protected=bool(out["protected"]), evidence_id=out["evidence_id"],
                counterparty_consent_required_for_change=bool(
                    out.get("counterparty_consent_required_for_change", False)),
                maximum_deferrable_minor=int(out.get("maximum_deferrable_minor", 0)),
                counterparty=out.get('counterparty', ''), invoice_reference=out.get('invoice_reference', ''),
            )
        for evt in data["events"]:
            led.events[evt["id"]] = BusinessEvent(
                id=evt["id"], type=evt["type"], receivable_id=evt["receivable_id"],
                new_expected_date=_d(evt["new_expected_date"]),
                evidence_id=evt["evidence_id"], observed_date=_d(evt["observed_date"]))
        return led

    @classmethod
    def seed(cls) -> "Ledger":
        return cls.from_fixture(seed_validated())

    # ------------------------------------------------------------------
    # Events (stable IDs, idempotent)
    # ------------------------------------------------------------------

    def collection_date_source(self, receivable_id: str) -> str:
        """Date provenance, including old saved demo events without a source map.

        Explicit reviewed sources take precedence. An applied event must match
        this invoice's current date; merely importing an event grants no source.
        This lookup never mutates historical state or executes an event.
        """
        rec=self.receivables[receivable_id]
        if receivable_id in self.collection_date_sources:
            return self.collection_date_sources[receivable_id]
        matches={event.evidence_id for ident,event in self.events.items()
                 if ident in self.applied_event_ids and event.type=='client_delay'
                 and event.receivable_id==receivable_id and event.new_expected_date==rec.expected_date}
        return next(iter(matches)) if len(matches)==1 else rec.evidence_id

    def apply_event(self, event_id: str, ctx=None) -> dict:
        """Apply a recorded event. Re-applying the same stable ID is a
        no-op returning already_applied=True.

        The caller is authenticated and authorized for this ledger's
        business BEFORE the idempotency short-circuit: even a no-op retry
        requires a valid request context.
        """
        authz_mod.ensure(ctx, self)
        if event_id in self.applied_event_ids:
            return {"event_id": event_id, "already_applied": True,
                    "changed": False}
        evt = self.events.get(event_id)
        if evt is None:
            raise KeyError(f"unknown event {event_id!r}")
        if evt.type == "client_delay":
            rec = self.receivables[evt.receivable_id]
            rec.expected_date = evt.new_expected_date
            self.collection_date_sources[rec.id] = evt.evidence_id
        else:  # pragma: no cover - validation restricts types today
            raise CashflowError(f"unsupported event type {evt.type!r}")
        self.applied_event_ids.add(event_id)
        self.log_audit("event_applied", "owner",
                       f"{event_id}: {evt.receivable_id}.expected_date -> "
                       f"{evt.new_expected_date.isoformat()}")
        return {"event_id": event_id, "already_applied": False, "changed": True,
                "detail": f"{evt.receivable_id}.expected_date -> "
                          f"{evt.new_expected_date.isoformat()}"}

    # ------------------------------------------------------------------
    # Receipts (idempotency keys, guarded mutation boundary)
    # ------------------------------------------------------------------

    def record_receipt(self, receipt: Receipt, actor: str = "owner",
                       documents: list[Document] | None = None,
                       authorizing_action: Action | None = None,
                       ctx=None, acknowledge_duplicates=False) -> dict:
        """Record an observed payment, allocated to its invoice.

        Order of checks (the mutation boundary):
        1. Caller authentication + business authorization (require_access)
           — FIRST, before the idempotency short-circuit.
        2. Idempotency, scoped to (business_id, receipt id): same key +
           identical payload + authorized caller -> the original result is
           returned WITHOUT re-executing (no trust re-scan, no
           re-authorization). Same key + different payload ->
           IdempotencyConflict. A receipt id recorded for, or presented
           for, a different business -> AuthorizationError.
        3. New execution: full validation + document trust scan + a current
           valid approval (authorize_mutation) before any state change.
        """
        # 1. Mutation boundary first: authenticate the caller and authorize
        # this ledger's business before the idempotency short-circuit.
        authz_mod.ensure(ctx, self)
        if receipt.business_id and receipt.business_id != self.business_id:
            raise AuthorizationError(
                f"receipt id {receipt.id!r} is presented for business "
                f"{receipt.business_id!r} but this ledger belongs to "
                f"{self.business_id!r}")

        documents = documents or []
        from .settlement import bill_payment_entries
        from .extension import refund_entries
        if receipt.id in self.unallocated_refunds or any(r['id']==receipt.id for _,_,r in refund_entries(self)):
            raise IdempotencyConflict('Observation ID already belongs to a refund; no duplicate cash was recorded.')
        if receipt.id in self.split_receipts:
            raise IdempotencyConflict('This ID is an observed split payment. Allocate its existing balance without recording cash again.')
        if receipt.id in self.cash_movements or any(receipt.id==p['id'] for o in self.outflows.values() for p in bill_payment_entries(o)):
            raise IdempotencyConflict('This observation ID already belongs to another cash/payment path; no second movement was recorded.')
        # 2. Idempotency, explicitly scoped to (business_id, receipt id).
        existing = self.receipts.get(receipt.id)
        if existing is not None:
            if existing.business_id and existing.business_id != self.business_id:
                raise AuthorizationError(
                    f"receipt id {receipt.id!r} is already recorded for "
                    f"business {existing.business_id!r}; refusing cross-business reuse"
                )
            if (existing.receivable_id == receipt.receivable_id
                    and existing.amount_minor == receipt.amount_minor
                    and existing.date_received == receipt.date_received
                    and existing.state == receipt.state):
                # Authorized retry of an already-recorded receipt: return the
                # original completed result without re-executing. The original
                # recording already passed the trust scan and authorization.
                return {"receipt_id": receipt.id, "duplicate": True,
                        "changed": False}
            raise IdempotencyConflict(
                f"receipt id {receipt.id!r} already recorded with different "
                "data; refusing to merge or overwrite"
            )

        # 3. New execution: full validation + trust scan + current valid
        # approval before any state change.
        actions_mod.require_trustworthy(documents)
        pending_observation = self.receipt_inbox.get(receipt.id)
        if pending_observation and (pending_observation['status'] != 'unallocated'
                or receipt.evidence_id != pending_observation['evidence_id']):
            raise CashflowError('This ID belongs to an observation inbox row. Use its explicit owner allocation review, not another receipt-import path.')
        if authorizing_action is not None:
            actions_mod.authorize_mutation(authorizing_action, documents, actor)
            if (authorizing_action.business_id
                    and authorizing_action.business_id != self.business_id):
                raise AuthorizationError('receipt approval belongs to a different business')
            if (authorizing_action.kind != 'request_early_payment'
                    or authorizing_action.terms.get('receivable_id') != receipt.receivable_id):
                raise CashflowError('receipt requires a payment-request approval for the same invoice')

        rec = self.receivables.get(receipt.receivable_id)
        if rec is None:
            raise KeyError(f"unknown receivable {receipt.receivable_id!r}")
        if type(receipt.amount_minor) is not int or receipt.amount_minor <= 0:
            raise CashflowError("receipt amount must be a positive integer")
        if receipt.date_received < self.as_of:
            raise CashflowError('A new receipt cannot predate the opening snapshot. Review a cash snapshot correction instead; old receipt retries remain idempotent.')
        if receipt.amount_minor > rec.remaining_minor:
            raise OverpaymentError(
                f"receipt {receipt.id!r} of {receipt.amount_minor} minor exceeds "
                f"the remaining balance {rec.remaining_minor} minor on {rec.id}; "
                "over-allocation refused. Handle the excess through a separate, "
                "explicit overpayment path."
            )

        from .payment_review import payment_candidates, require_ack, remember_ack, stamp_ack_fingerprint
        candidates = payment_candidates(self, rec.currency, receipt.amount_minor, receipt.date_received)
        if receipt.state == 'observed':
            require_ack(candidates, acknowledge_duplicates)

        receipt.business_id = self.business_id  # stamp the business scope
        self.receipts[receipt.id] = receipt
        if receipt.state == 'observed':
            remember_ack(self, 'receipt', receipt.id, candidates, actor, receipt.evidence_id)
            stamp_ack_fingerprint(self,'receipt',receipt.id)
        if receipt.state == "observed":
            rec.allocations.append(Allocation(
                receipt_id=receipt.id, amount_minor=receipt.amount_minor,
                date_received=receipt.date_received, observed=True))
        self.log_audit("receipt_recorded", actor,
                       f"{receipt.id}: {receipt.amount_minor} minor on "
                       f"{receipt.receivable_id} (remaining "
                       f"{rec.remaining_minor} minor)")
        return {"receipt_id": receipt.id, "duplicate": False, "changed": True,
                "remaining_minor": rec.remaining_minor}

    # ------------------------------------------------------------------
    # Actual cash (settled reality only — plans and drafts never touch this)
    # ------------------------------------------------------------------

    def actual_cash(self, currency: str, on_date: date | None = None) -> int:
        """Cash actually on hand: opening + observed receipts - paid outflows.

        Drafts, pending approvals, projected receipts and scheduled (unpaid)
        outflows never alter this.
        """
        on_date = on_date or self.as_of
        if on_date < self.as_of:
            raise CashflowError('Historical cash before the opening snapshot is not available from the current ledger.')
        from .treasury import currency_opening
        from .settlement import cash_events
        return currency_opening(self, currency) + sum(e['amount_minor'] for e in cash_events(self)
            if e['currency'] == currency and self.as_of <= e['date'] <= on_date)

    def apply_recorded_amendment(self, outflow_id: str, actor: str,
                                   evidence_id: str, kind: str,
                                   new_date: date | None = None,
                                   splits: list[tuple[date, int]] | None = None,
                                   detail: str = "", ctx=None) -> dict:
        """Amend the RECORDED terms of a scheduled payable.

        - kind == "counterparty_accepted_move": requires the evidence to be an
          explicit runtime acceptance event (never pre-granted).
        - kind == "owner_approved_deferral": requires owner approval evidence;
          the deferred amount must stay within maximum_deferrable_minor.
        Original terms are retained on the outflow and the amendment (with
        its evidence) is appended to the outflow's amendment history.
        Re-applying the identical amendment is an idempotent no-op.

        Caller authentication + business authorization happen FIRST, before
        the idempotency short-circuit.
        """
        authz_mod.ensure(ctx, self)
        out = self.outflows.get(outflow_id)
        if out is None:
            raise KeyError(f"unknown outflow {outflow_id!r}")
        if out.status == "paid":
            raise CashflowError(f"{outflow_id} is already paid; cannot amend")
        if out.paid_minor or out.agreement_schedules:
            raise CashflowError('A partially paid bill needs manual terms review; its remaining schedule is not automatically amended.')
        if out.face_adjustments:
            raise CashflowError('A source-adjusted bill needs manual terms review before its schedule can be changed.')
        if evidence_id not in self.evidence:
            raise CashflowError(f"unknown evidence {evidence_id!r}")
        if out.protected and (new_date is not None or splits is not None):
            raise CashflowError(
                f"{outflow_id} is a protected obligation; its date cannot be amended")
        if kind == "counterparty_accepted_move":
            if not out.counterparty_consent_required_for_change:
                raise CashflowError(
                    f"{outflow_id} does not require counterparty consent")
            if evidence_id not in self.runtime_evidence_ids:
                raise CashflowError(
                    "recorded supplier terms change requires an explicit "
                    "counterparty acceptance event; consent is never pre-granted")
            if new_date is None:
                raise CashflowError("new_date is required for a date move")
            new_schedule = [(new_date, out.amount_minor)]
        elif kind == "owner_approved_deferral":
            if splits is None:
                raise CashflowError("splits are required for a deferral")
            total = sum(m for _, m in splits)
            if total != out.amount_minor:
                raise CashflowError(
                    f"split amounts sum to {total}, expected {out.amount_minor}")
            moved = sum(m for d, m in splits if d > out.date)
            if moved > out.maximum_deferrable_minor:
                raise CashflowError(
                    f"deferring {moved} exceeds the allowed maximum of "
                    f"{out.maximum_deferrable_minor}")
            new_schedule = list(splits)
        else:
            raise CashflowError(f"unknown amendment kind {kind!r}")

        current = out.recorded_splits if out.recorded_splits else [
            (out.date, out.amount_minor)]
        if current == new_schedule:
            return {"outflow_id": outflow_id, "duplicate": True, "changed": False}

        if out.original_date is None:
            out.original_date = out.date
            out.original_amount_minor = out.amount_minor
        if new_date is not None:
            out.date = new_date
            out.recorded_splits = None
        else:
            out.recorded_splits = new_schedule
        out.amendments.append({
            "at": _utcnow_iso(), "kind": kind, "actor": actor,
            "evidence_id": evidence_id,
            "schedule": [(d.isoformat(), m) for d, m in new_schedule],
            "detail": detail,
        })
        self.log_audit("outflow_terms_amended", actor,
                       f"{outflow_id}: {kind} evidence={evidence_id} {detail}")
        return {"outflow_id": outflow_id, "duplicate": False, "changed": True}

    def mark_paid(self, outflow_id: str, paid_date: date, actor: str,
                  evidence_id: str, ctx=None, acknowledge_duplicates=False) -> dict:
        """Record an outflow as actually paid.

        Guarded mutation boundary: the caller is authenticated and
        authorized for this ledger's business FIRST (before the idempotency
        short-circuit); the caller must cite existing evidence for the
        payment; re-marking with a different date is a conflict. Marking
        with the same date twice is an idempotent no-op.
        """
        authz_mod.ensure(ctx, self)
        out = self.outflows.get(outflow_id)
        if out is None:
            raise KeyError(f"unknown outflow {outflow_id!r}")
        if evidence_id not in self.evidence:
            raise CashflowError(
                f"mark_paid requires an existing evidence id; "
                f"{evidence_id!r} is unknown"
            )
        if out.legacy_payment and out.legacy_payment['date']==paid_date.isoformat():
            return {'outflow_id':outflow_id,'duplicate':True,'changed':False}
        if out.status == "paid":
            if out.paid_date == paid_date:
                return {"outflow_id": outflow_id, "duplicate": True,
                        "changed": False}
            raise IdempotencyConflict(
                f"{outflow_id} already marked paid on {out.paid_date}; "
                f"re-marking with {paid_date} refused"
            )
        if paid_date < self.as_of:
            raise CashflowError('A newly recorded paid bill cannot predate the opening snapshot. Review a cash snapshot correction instead.')
        if out.observed_payments or out.legacy_payment or out.payment_adjustments or out.face_adjustments:
            from .accounting import record_bill_payment
            from .owner_workflows import review_revision
            from .authz import owner_context_for
            from .planning import _hash
            return record_bill_payment(self, outflow_id, 'remainder-' + _hash(outflow_id)[:32] + '-' + paid_date.isoformat(),
                out.remaining_minor, paid_date, evidence_id, 'Owner recorded the remaining bill payment.',
                review_revision(self), ctx=ctx or owner_context_for(self.business_id), acknowledge_duplicates=acknowledge_duplicates)
        from .extension import observation_ids
        if 'legacy-paid-'+out.id in observation_ids(self):
            raise IdempotencyConflict('Legacy payment observation ID already belongs to another cash path.')
        from .payment_review import payment_candidates,require_ack,remember_ack,stamp_ack_fingerprint
        candidates=payment_candidates(self,out.currency,-out.remaining_minor,paid_date)
        require_ack(candidates,acknowledge_duplicates)
        out.status = "paid"
        out.paid_date = paid_date
        remember_ack(self,'bill_payment','legacy-paid-'+out.id,candidates,actor,evidence_id)
        stamp_ack_fingerprint(self,'bill_payment','legacy-paid-'+out.id)
        self.log_audit("outflow_marked_paid", actor,
                       f"{outflow_id}: {out.amount_minor} minor on "
                       f"{paid_date.isoformat()} evidence={evidence_id}")
        return {"outflow_id": outflow_id, "duplicate": False, "changed": True}

    # ------------------------------------------------------------------
    # As-of roll-forward
    # ------------------------------------------------------------------

    def roll_forward(self, new_as_of: date, ctx=None) -> dict:
        """Move the as-of date forward, re-basing opening balances.

        The opening snapshot absorbs settled events and receipts dated
        before the new as-of, so reforecasting never counts them twice.
        Caller authentication + business authorization happen first.
        """
        authz_mod.ensure(ctx, self)
        if type(new_as_of) is not date:
            raise CashflowError('Snapshot date must be a calendar date.')
        if new_as_of < self.as_of:
            raise CashflowError("cannot roll the as-of date backwards")
        if new_as_of == self.as_of:
            return {"as_of": self.as_of.isoformat(), "changed": False}
        if (new_as_of - self.as_of).days > 366:
            raise CashflowError('Advance the snapshot by at most 366 days at a time.')
        from .settlement import cash_events
        observed = list(cash_events(self, include_internal=True))
        for account in self.accounts.values():
            # Absorb ONLY settled events strictly before the new opening.
            # Expected collections and scheduled unpaid bills stay proposals.
            account.opening_minor += sum(e['amount_minor'] for e in observed
                if e['account_id'] == account.id and self.as_of <= e['date'] < new_as_of)
        self.as_of = new_as_of
        self.log_audit("as_of_rolled_forward", "owner",
                       f"as_of -> {new_as_of.isoformat()}")
        return {"as_of": new_as_of.isoformat(), "changed": True,
                "opening_minor_by_account": {
                    a.id: a.opening_minor for a in self.accounts.values()}}
