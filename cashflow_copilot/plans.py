"""Response-plan construction, evaluation, ranking and generation.

A plan is a what-if scenario: evaluating it never mutates the ledger.
Plans are ranked only after satisfying hard constraints, by the visible
objective: fewer altered obligations, then lower direct cost, then smaller
date changes. This ordering is a documented heuristic, not a universal
financial optimum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .errors import MissingConsentError, ProtectedObligationError
from .forecast import ForecastResult, forecast
from .models import OutflowAmendment, OwnerConstraints, Plan, ProjectedReceipt

CONDITIONAL_ON_COLLECTION = (
    "Conditional on collection: projected receipts are not cash on hand. "
    "Actual balances are unchanged until receipts are observed and recorded."
)


@dataclass
class PlanResult:
    plan: Plan
    feasible: bool
    reasons: list[str] = field(default_factory=list)
    forecast: ForecastResult | None = None
    currency: str = "USD"
    min_balance_minor: int = 0
    closing_minor: int = 0
    altered_obligations: int = 0
    direct_cost_minor: int = 0
    total_date_shift_days: int = 0

    def rank_key(self) -> tuple[int, int, int]:
        return (self.altered_obligations, self.direct_cost_minor, self.total_date_shift_days)


# ---------------------------------------------------------------------------
# Plan constructors for the fixture scenarios
# ---------------------------------------------------------------------------

def build_plan_a(receivable_id: str = "inv-client-001") -> Plan:
    """Early partial collection: 5,000 on 22 Oct, remainder 3,000 on 5 Nov."""
    return Plan(
        id="plan-a",
        name="Plan A — early partial collection",
        description=(
            "Request USD 5,000 on 22 October 2026 with the remaining "
            "USD 3,000 on 5 November 2026. Payroll and all outflow dates "
            "stay unchanged."
        ),
        projected_receipts=[
            ProjectedReceipt(receivable_id, 500_000, date(2026, 10, 22),
                             "conditional on client paying the advance"),
            ProjectedReceipt(receivable_id, 300_000, date(2026, 11, 5),
                             "conditional on client paying the remainder"),
        ],
        conditional_labels=[CONDITIONAL_ON_COLLECTION],
    )


def build_plan_b_supplier_only(consent_evidence_id: str | None = None) -> Plan:
    """Move the supplier payment to 6 Nov (requires counterparty acceptance).

    Pass the evidence id produced by an explicit counterparty-acceptance
    event (Ledger.record_counterparty_acceptance); without it the plan is
    infeasible.
    """
    consent_note = (
        f"the supplier's acceptance ({consent_evidence_id})"
        if consent_evidence_id else
        "the supplier's acceptance (no explicit acceptance event recorded yet)"
    )
    return Plan(
        id="plan-b-supplier-only",
        name="Plan B (supplier move only) — insufficient",
        description="Move the USD 4,500 supplier payment to 6 November 2026.",
        amendments=[
            OutflowAmendment(
                "supplier-001",
                new_date=date(2026, 11, 6),
                consent_evidence_id=consent_evidence_id,
            )
        ],
        conditional_labels=[
            f"Conditional on {consent_note}; "
            "owner approval alone cannot change the supplier's terms."
        ],
    )


def build_plan_b_full(consent_evidence_id: str | None = None) -> Plan:
    """Supplier move to 6 Nov plus USD 500 of ops deferred to 6 Nov."""
    consent_note = (
        f"the supplier's acceptance ({consent_evidence_id})"
        if consent_evidence_id else
        "the supplier's acceptance (no explicit acceptance event recorded yet)"
    )
    return Plan(
        id="plan-b",
        name="Plan B — supplier move plus ops deferral",
        description=(
            "Move the USD 4,500 supplier payment to 6 November 2026 "
            "(counterparty acceptance required) and defer USD 500 of the "
            "1,500 operations payment to 6 November 2026 (owner approval "
            "required), keeping USD 1,000 on 28 October."
        ),
        amendments=[
            OutflowAmendment(
                "supplier-001",
                new_date=date(2026, 11, 6),
                consent_evidence_id=consent_evidence_id,
            ),
            OutflowAmendment(
                "ops-001",
                splits=[(date(2026, 10, 28), 100_000), (date(2026, 11, 6), 50_000)],
                approval_evidence_id="ev-ops-budget",
            ),
        ],
        conditional_labels=[
            f"Conditional on {consent_note} "
            "and the owner's approval of the ops deferral; owner approval "
            "alone cannot change the supplier's terms.",
            CONDITIONAL_ON_COLLECTION,
        ],
    )


def build_advance_plan(max_advance_minor: int,
                       receivable_id: str = "inv-client-001",
                       advance_date: date = date(2026, 10, 22)) -> Plan:
    """A client advance capped at max_advance_minor; nothing else changes."""
    return Plan(
        id="plan-advance-capped",
        name=f"Capped advance of {max_advance_minor // 100}",
        description=(
            f"Client advances at most {max_advance_minor // 100} "
            f"on {advance_date.isoformat()}; no payment dates change."
        ),
        projected_receipts=[
            ProjectedReceipt(receivable_id, max_advance_minor, advance_date,
                             "conditional on client paying the advance"),
        ],
        conditional_labels=[CONDITIONAL_ON_COLLECTION],
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

def _plan_metrics(ledger, plan: Plan) -> tuple[int, int, int]:
    altered = len(plan.amendments)
    direct_cost = 0  # no discounts or fees in the fixture plans
    shift_days = 0
    for amd in plan.amendments:
        out = ledger.outflows[amd.outflow_id]
        if amd.new_date is not None:
            shift_days += abs((amd.new_date - out.date).days)
        elif amd.splits is not None:
            for d, m in amd.splits:
                # Amount-weighted shift in whole days; integer arithmetic only.
                shift_days += abs((d - out.date).days) * m // out.amount_minor
    return altered, direct_cost, int(shift_days)


def evaluate_plan(ledger, plan: Plan,
                  constraints: OwnerConstraints | None = None) -> PlanResult:
    """Evaluate a plan against hard constraints and per-currency floors.

    Never mutates the ledger. A plan that moves a protected obligation or
    lacks required consent/authority is infeasible with an explicit reason —
    feasibility is never manufactured.
    """
    constraints = constraints or OwnerConstraints()
    reasons: list[str] = []
    altered, direct_cost, shift_days = _plan_metrics(ledger, plan)

    # --- hard-constraint screening (before any arithmetic) ---
    for amd in plan.amendments:
        out = ledger.outflows.get(amd.outflow_id)
        if out is None:
            return PlanResult(plan, False, [f"unknown outflow {amd.outflow_id!r}"],
                              None, altered_obligations=altered,
                              direct_cost_minor=direct_cost,
                              total_date_shift_days=shift_days)
        if out.protected and (
            (amd.new_date is not None and amd.new_date != out.date) or amd.splits
        ):
            return PlanResult(
                plan, False,
                [f"Changing the protected date of {out.id} invalidates the plan."],
                None, altered_obligations=altered,
                direct_cost_minor=direct_cost, total_date_shift_days=shift_days)
        if out.id == "supplier-001" and not constraints.allow_supplier_date_change:
            return PlanResult(
                plan, False,
                ["Supplier date changes are not allowed under the current constraints."],
                None, altered_obligations=altered,
                direct_cost_minor=direct_cost, total_date_shift_days=shift_days)
        if out.id == "ops-001" and amd.splits and not constraints.allow_ops_deferral:
            return PlanResult(
                plan, False,
                ["Operations deferral is not allowed under the current constraints."],
                None, altered_obligations=altered,
                direct_cost_minor=direct_cost, total_date_shift_days=shift_days)

    for pr in plan.projected_receipts:
        if constraints.max_advance_minor is not None and \
                pr.amount_minor > constraints.max_advance_minor:
            return PlanResult(
                plan, False,
                [f"Projected advance {pr.amount_minor} exceeds the maximum "
                 f"allowed advance {constraints.max_advance_minor}."],
                None, altered_obligations=altered,
                direct_cost_minor=direct_cost, total_date_shift_days=shift_days)

    # --- arithmetic evaluation (catches consent/authority failures) ---
    try:
        fc = forecast(ledger, ledger.as_of, ledger.horizon_days, plan=plan)
    except (ProtectedObligationError, MissingConsentError, ValueError) as exc:
        return PlanResult(plan, False, [str(exc)], None,
                          altered_obligations=altered,
                          direct_cost_minor=direct_cost,
                          total_date_shift_days=shift_days)

    # --- floor feasibility, per currency ---
    feasible = True
    for ccy, cf in fc.currencies.items():
        if cf.first_breach_date is not None:
            feasible = False
            reasons.append(
                f"{ccy}: floor {cf.floor_minor} breached first on "
                f"{cf.first_breach_date.isoformat()} "
                f"(min {cf.min_balance_minor}, shortfall {cf.worst_shortfall_minor})"
            )
    usd = fc.currencies.get(ledger.base_currency)
    return PlanResult(
        plan=plan,
        feasible=feasible,
        reasons=reasons or ["All per-currency floors respected; no protected "
                            "obligation moved; required consents present."],
        forecast=fc,
        currency=ledger.base_currency,
        min_balance_minor=usd.min_balance_minor if usd else 0,
        closing_minor=usd.closing_minor if usd else 0,
        altered_obligations=altered,
        direct_cost_minor=direct_cost,
        total_date_shift_days=shift_days,
    )


def rank_plans(results: list[PlanResult]) -> list[PlanResult]:
    """Rank feasible plans: fewer altered obligations, lower direct cost,
    then smaller date changes."""
    return sorted(results, key=lambda r: r.rank_key())


def generate_candidate_plans(ledger,
                             constraints: OwnerConstraints | None = None
                             ) -> tuple[list[PlanResult], list[str]]:
    """Deterministically generate and evaluate candidate plans.

    Returns (ranked feasible results, notes). When nothing is feasible the
    notes explain why; no loan, transfer, consent or extra payment is ever
    invented to manufacture feasibility.
    """
    constraints = constraints or OwnerConstraints()
    notes: list[str] = []
    candidates: list[Plan] = []

    remaining = ledger.receivables["inv-client-001"].remaining_minor
    advance_cap = constraints.max_advance_minor if constraints.max_advance_minor is not None else remaining
    if advance_cap >= 500_000:
        candidates.append(build_plan_a())
    elif advance_cap > 0:
        candidates.append(build_advance_plan(advance_cap))
    else:
        notes.append("No advance allowed (max_advance_minor is 0).")

    if constraints.allow_supplier_date_change:
        consent_id = ledger.consent_evidence_id("supplier-001")
        candidates.append(build_plan_b_supplier_only(consent_id))
        if constraints.allow_ops_deferral:
            candidates.append(build_plan_b_full(consent_id))
    else:
        notes.append("Supplier date changes are not allowed by the constraints.")
    if not constraints.allow_ops_deferral:
        notes.append("Operations deferral is not allowed by the constraints.")

    feasible: list[PlanResult] = []
    for plan in candidates:
        result = evaluate_plan(ledger, plan, constraints)
        if result.feasible:
            feasible.append(result)
        else:
            notes.append(f"{plan.name}: infeasible — {'; '.join(result.reasons)}")

    if not feasible:
        notes.append(
            "No feasible plan within the allowed actions. No loan, transfer, "
            "supplier consent, or extra client payment was invented to "
            "manufacture feasibility."
        )
    return rank_plans(feasible), notes
