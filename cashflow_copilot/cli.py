"""Command line: seed / reset / demo / serve.

    python -m cashflow_copilot seed --db data/state.sqlite
    python -m cashflow_copilot reset --db data/state.sqlite
    python -m cashflow_copilot demo
    python -m cashflow_copilot serve --db data/state.sqlite --port 8765
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .forecast import forecast
from .ledger import Ledger
from .models import OwnerConstraints
from .money import format_amount
from .plans import (
    build_advance_plan,
    build_plan_a,
    build_plan_b_full,
    build_plan_b_supplier_only,
    evaluate_plan,
    generate_candidate_plans,
    rank_plans,
)
from .store import SQLiteStore

INFEASIBLE_CONSTRAINTS = OwnerConstraints(
    max_advance_minor=400_000,
    allow_supplier_date_change=False,
    allow_ops_deferral=False,
)


def cmd_seed(db_path: str) -> None:
    if Path(db_path).exists():
        current = SQLiteStore(db_path)
        try:
            existing = current.load_ledger()
        finally:
            current.close()
        from .authz import owner_context_for
        from .recovery import create_backup
        create_backup(db_path, 'Automatic before seed/reset replacement', ctx=owner_context_for(existing.business_id))
    ledger = Ledger.seed()
    store = SQLiteStore(db_path)
    try:
        store.save_ledger(ledger,commit=False)
        # The four parts of discarded agent history must reset atomically with
        # its ledger. The automatic recovery copy retains the prior snapshot.
        tables={r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ('agent_runs','agent_requests','agent_run_metadata','agent_request_policies'):
            if table in tables:store.conn.execute('DELETE FROM '+table)
        store.conn.commit()
    except BaseException:
        store.conn.rollback();raise
    finally:store.close()
    print(f"seeded {db_path} from fixture_v1.json "
          f"(as_of={ledger.as_of.isoformat()})")


def cmd_reset(db_path: str) -> None:
    cmd_seed(db_path)
    print("reset complete")


def _show(title: str, fc, ccy: str = "USD") -> None:
    cf = fc.currencies[ccy]
    print(f"--- {title} ---")
    print(f"min balance: {format_amount(cf.min_balance_minor, ccy)} "
          f"on {cf.min_balance_date.isoformat()}")
    print(f"closing:     {format_amount(cf.closing_minor, ccy)}")
    print(f"first breach of {format_amount(cf.floor_minor, ccy)} floor: "
          f"{cf.first_breach_date}")
    print(f"worst shortfall vs floor: {format_amount(cf.worst_shortfall_minor, ccy)}")


def cmd_demo() -> None:
    ledger = Ledger.seed()
    as_of = ledger.as_of
    horizon = ledger.horizon_days

    base = forecast(ledger, as_of, horizon)
    _show("baseline (no delay)", base)
    eur = base.currencies["EUR"]
    print(f"EUR stays at: {format_amount(eur.min_balance_minor, 'EUR')} "
          f"(min) / {format_amount(eur.closing_minor, 'EUR')} (closing)")

    ledger.apply_event("evt-delay-001")
    delayed = forecast(ledger, as_of, horizon)
    _show("after client delay to 2026-11-05", delayed)

    plan_a = evaluate_plan(ledger, build_plan_a())
    print(f"--- {plan_a.plan.name} ---")
    print(f"feasible={plan_a.feasible} "
          f"min={format_amount(plan_a.min_balance_minor, 'USD')} "
          f"closing={format_amount(plan_a.closing_minor, 'USD')}")
    print("reasons:", "; ".join(plan_a.reasons))

    b_only = evaluate_plan(ledger, build_plan_b_supplier_only())
    print(f"--- {b_only.plan.name} (no consent event yet) ---")
    print(f"feasible={b_only.feasible}")
    print("reasons:", "; ".join(b_only.reasons))

    # Consent is never pre-granted: record the explicit acceptance event first.
    from datetime import date
    consent_id = ledger.record_counterparty_acceptance(
        "supplier-001", date(2026, 11, 6), "act-demo-supplier", "owner")
    print(f"explicit consent event recorded: {consent_id}")

    b_only2 = evaluate_plan(ledger, build_plan_b_supplier_only(consent_id))
    print(f"--- {b_only2.plan.name} (after explicit consent) ---")
    print(f"feasible={b_only2.feasible} "
          f"min={format_amount(b_only2.min_balance_minor, 'USD')}")
    print("reasons:", "; ".join(b_only2.reasons))

    b_full = evaluate_plan(ledger, build_plan_b_full(consent_id))
    print(f"--- {b_full.plan.name} ---")
    print(f"feasible={b_full.feasible} "
          f"min={format_amount(b_full.min_balance_minor, 'USD')} "
          f"closing={format_amount(b_full.closing_minor, 'USD')}")
    print("reasons:", "; ".join(b_full.reasons))

    ranked = rank_plans([r for r in (plan_a, b_full) if r.feasible])
    print("ranking:", " > ".join(r.plan.id for r in ranked))

    infeas = evaluate_plan(ledger, build_advance_plan(400_000),
                           INFEASIBLE_CONSTRAINTS)
    print("--- infeasible constraint set (max 4,000 advance, no date changes) ---")
    print(f"feasible={infeas.feasible} "
          f"min={format_amount(infeas.min_balance_minor, 'USD')} "
          f"shortfall={format_amount(infeas.forecast.currencies['USD'].worst_shortfall_minor, 'USD')}")
    feas, notes = generate_candidate_plans(ledger, INFEASIBLE_CONSTRAINTS)
    print(f"generated feasible plans: {len(feas)}")
    for n in notes:
        print("note:", n)


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="cashflow-copilot")
    sub = parser.add_subparsers(dest="command", required=True)
    p_seed = sub.add_parser("seed", help="validate the fixture and seed a database")
    p_seed.add_argument("--db", default="data/state.sqlite")
    p_reset = sub.add_parser("reset", help="back up the existing ledger and reseed")
    p_reset.add_argument("--db", default="data/state.sqlite")
    sub.add_parser("demo", help="print the Phase 1 scenario summary")
    p_serve = sub.add_parser("serve", help="run the Fluxora web workspace (localhost only)")
    p_serve.add_argument("--db", default="data/state.sqlite")
    p_serve.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if args.command == "seed":
        cmd_seed(args.db)
    elif args.command == "reset":
        cmd_reset(args.db)
    elif args.command == "demo":
        cmd_demo()
    elif args.command == "serve":
        from .web import run_server
        run_server(args.db, args.port)


if __name__ == "__main__":
    main()
