"""Core data models. All money fields are integer minor units."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


# ---------------------------------------------------------------------------
# Evidence and reference data
# ---------------------------------------------------------------------------

@dataclass
class Evidence:
    id: str
    kind: str
    summary: str


@dataclass
class FxQuote:
    from_ccy: str
    to_ccy: str
    rate: Decimal  # parsed from a decimal string; never a binary float
    as_of: date
    source: str


@dataclass
class Document:
    """An uploaded/provided document.

    Documents are evidence, never authorization. Their text is scanned at the
    mutation boundary (see actions.require_trustworthy) and must never be
    treated as executable instructions.
    """

    id: str
    kind: str
    content: str
    source: str = "owner_upload"


# ---------------------------------------------------------------------------
# Ledger items
# ---------------------------------------------------------------------------

@dataclass
class Account:
    id: str
    currency: str
    opening_minor: int
    floor_minor: int
    evidence_id: str | None = None


@dataclass
class Allocation:
    """One allocation of a receipt against a receivable.

    Kept as history separate from future forecasts: a partial payment reduces
    the receivable's remaining balance and the original invoice is never
    recounted.
    """

    receipt_id: str
    amount_minor: int
    date_received: date
    observed: bool  # True = actual cash movement; False = projected (plan only)


@dataclass
class Receivable:
    id: str
    currency: str
    original_minor: int
    expected_date: date | None  # None = unknown timing -> clarification, not invention
    evidence_id: str
    allocations: list[Allocation] = field(default_factory=list)
    due_date: date | None = None  # contractual due date; never inferred from expected_date
    counterparty: str = ""
    invoice_reference: str = ""
    identity_evidence_id: str = ""
    collection_hold: dict | None = None
    face_adjustments: list[dict] = field(default_factory=list)
    refunds: list[dict] = field(default_factory=list)
    credit_entries: list[dict] = field(default_factory=list)  # noncash settlement transfers/reversals

    @property
    def effective_total_minor(self) -> int:
        return self.face_adjustments[-1]['new_total_minor'] if self.face_adjustments else self.original_minor

    @property
    def credit_balance_minor(self) -> int:
        return max(0, self.allocated_minor - self.effective_total_minor)

    @property
    def allocated_minor(self) -> int:
        return sum(a.amount_minor for a in self.allocations) + sum(c['delta_minor'] for c in self.credit_entries) - sum(
            r['adjustments'][-1]['new_amount_minor'] if r['adjustments'] else r['amount_minor'] for r in self.refunds)

    @property
    def remaining_minor(self) -> int:
        return max(0, self.effective_total_minor - self.allocated_minor)


@dataclass
class Outflow:
    """A scheduled payment (payable). Status distinguishes a scheduled payment
    from an actual payment: explicit observed payments move actual cash;
    legacy fully paid records are also counted once. Partial observations
    leave the residual schedule payable.

    When an accepted proposal amends the recorded terms, the original date
    and amount are retained in original_date / original_amount_minor and the
    amendment (with its evidence) is appended to amendments.
    """

    id: str
    currency: str
    amount_minor: int
    date: date
    protected: bool
    evidence_id: str
    counterparty_consent_required_for_change: bool = False
    maximum_deferrable_minor: int = 0
    status: str = "scheduled"  # scheduled | paid | adjusted (closed/overpaid by source adjustment)
    paid_date: date | None = None
    original_date: date | None = None
    original_amount_minor: int | None = None
    # Current recorded payment schedule; None = single payment on `date`.
    recorded_splits: list[tuple[date, int]] | None = None
    amendments: list = field(default_factory=list)  # list[dict]
    observed_payments: list[dict] = field(default_factory=list)
    counterparty: str = ""
    invoice_reference: str = ""
    identity_evidence_id: str = ""
    payment_adjustments: list[dict] = field(default_factory=list)
    legacy_payment: dict | None = None
    face_adjustments: list[dict] = field(default_factory=list)
    refunds: list[dict] = field(default_factory=list)
    credit_entries: list[dict] = field(default_factory=list)

    @property
    def effective_total_minor(self) -> int:
        return self.face_adjustments[-1]['new_total_minor'] if self.face_adjustments else self.amount_minor

    agreement_schedules: list[dict] = field(default_factory=list)

    @property
    def overpaid_minor(self) -> int:
        return max(0, self.paid_minor - self.effective_total_minor)

    @property
    def paid_minor(self) -> int:
        legacy = self.legacy_payment['amount_minor'] if self.legacy_payment else (
            self.amount_minor if self.status == 'paid' and not self.observed_payments and not self.credit_entries else 0)
        return legacy + sum(c['delta_minor'] for c in self.credit_entries) + sum(p['amount_minor'] for p in self.observed_payments) + sum(
            c['new_amount_minor']-c['old_amount_minor'] for c in self.payment_adjustments) - sum(
                r['adjustments'][-1]['new_amount_minor'] if r['adjustments'] else r['amount_minor'] for r in self.refunds)

    @property
    def remaining_minor(self) -> int:
        return max(0, self.effective_total_minor - self.paid_minor)


@dataclass
class Receipt:
    """A payment received against a receivable. The id is the idempotency key:
    recording the same id twice is a no-op; reusing the id with different
    data is a conflict.

    The idempotency key is scoped to (business_id, id): the engine stamps
    business_id at the mutation boundary and refuses cross-business reuse.
    """

    id: str
    receivable_id: str
    amount_minor: int
    date_received: date
    state: str = "observed"  # observed | projected
    evidence_id: str = ""
    actor: str = ""
    business_id: str = ""  # stamped by Ledger.record_receipt


@dataclass
class BusinessEvent:
    """A recorded event with a stable ID. Re-applying the same event ID is
    idempotent."""

    id: str
    type: str  # e.g. 'client_delay'
    receivable_id: str
    new_expected_date: date
    evidence_id: str
    observed_date: date


# ---------------------------------------------------------------------------
# Plans (what-if scenarios — never mutate the ledger)
# ---------------------------------------------------------------------------

@dataclass
class ProjectedReceipt:
    receivable_id: str
    amount_minor: int
    date: date
    label: str = ""  # e.g. 'conditional on client paying'


@dataclass
class OutflowAmendment:
    outflow_id: str
    new_date: date | None = None
    # Splits replace the single scheduled payment, e.g. [(date, minor), ...].
    splits: list[tuple[date, int]] | None = None
    consent_evidence_id: str | None = None
    approval_evidence_id: str | None = None


@dataclass
class Plan:
    id: str
    name: str
    description: str
    projected_receipts: list[ProjectedReceipt] = field(default_factory=list)
    amendments: list[OutflowAmendment] = field(default_factory=list)
    conditional_labels: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Owner constraints
# ---------------------------------------------------------------------------

@dataclass
class OwnerConstraints:
    """Which actions the owner allows the planner to consider."""

    max_advance_minor: int | None = None  # None = full remaining balance allowed
    allow_supplier_date_change: bool = True
    allow_ops_deferral: bool = True
    max_discount_minor: int = 0
    # Currency-specific owner limits. The legacy scalar applies to base currency.
    advance_caps_minor: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Actions (explicit persisted workflow states)
# ---------------------------------------------------------------------------

ACTION_STATES = (
    "draft",
    "cancelled",
    "owner_approved",
    "sent",
    "counterparty_accepted",
    "counterparty_rejected",
    "payment_observed",
    "reconciled",
)

# Server-side validated transitions. A reminder retry keeps 'sent' -> 'sent'
# so it can never create a duplicate action.
ACTION_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"owner_approved", "cancelled"},
    "cancelled": set(),
    "owner_approved": {"sent", "draft"},
    "sent": {"counterparty_accepted", "counterparty_rejected", "sent"},
    "counterparty_accepted": {"payment_observed"},
    "counterparty_rejected": set(),
    "payment_observed": {"reconciled"},
    "reconciled": set(),
}

# States whose approval may authorize a financial mutation at the boundary.
MUTATION_AUTHORIZING_STATES = {"owner_approved", "counterparty_accepted"}


@dataclass
class Action:
    id: str
    kind: str
    terms: dict
    actor: str
    version: int = 1
    state: str = "draft"
    approved_version: int | None = None
    counterparty_response: dict | None = None
    audit: list[dict] = field(default_factory=list)
    # Business the action belongs to. Set server-side at creation; state
    # transitions authorize the caller against it. "" = legacy/unscoped.
    business_id: str = ""
