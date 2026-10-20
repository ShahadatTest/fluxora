"""Regression tests: authorization happens BEFORE the idempotency
short-circuit, and idempotency keys are scoped to (business_id, operation).

Covers the Phase 3 workstream-A hardening:
- unknown/empty actor rejected, even for an already-recorded (duplicate)
  operation;
- wrong business_id rejected, including cross-business receipt-id reuse;
- conflicting content on the same key still raises IdempotencyConflict;
- an authorized retry returns the original result WITHOUT re-executing
  (single allocation, single audit entry);
- a new execution without a valid approval is still rejected;
- threaded duplicate POSTs through the real HTTP server produce exactly
  one receipt/allocation.
"""

import re
import sqlite3
import threading
import urllib.parse
import urllib.request
import urllib.error
from datetime import date

import pytest

from cashflow_copilot import actions as actions_mod
from cashflow_copilot.authz import RequestContext, ensure, require_access
from cashflow_copilot.errors import (
    ApprovalInvalidatedError,
    AuthorizationError,
    IdempotencyConflict,
)
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.models import Receipt

OCT_22 = date(2026, 10, 22)
OCT_28 = date(2026, 10, 28)
BIZ = "demo-agency"


def _ctx(actor="owner", role="owner", business_id=BIZ):
    return RequestContext(actor=actor, role=role, business_id=business_id)


def _receipt(rid="rcpt-t1", amount=500_000, business_id=""):
    return Receipt(id=rid, receivable_id="inv-client-001",
                   amount_minor=amount, date_received=OCT_22,
                   state="observed", evidence_id="ev-client-invoice",
                   actor="owner", business_id=business_id)


def _approved_action(ledger, action_id="act-t1"):
    act = actions_mod.create_action(
        action_id, "request_early_payment",
        {"receivable_id": "inv-client-001", "amount_minor": 500_000,
         "date": "2026-10-22"},
        actor="owner", ctx=_ctx(), business_id=ledger.business_id)
    actions_mod.approve(act, actor="owner", ctx=_ctx())
    actions_mod.send(act, actor="owner", ctx=_ctx())
    actions_mod.counterparty_respond(act, True, "ev-test-accept",
                                    actor="sandbox_counterparty", ctx=_ctx())
    return act


# ---------------------------------------------------------------------------
# Caller authentication comes before the idempotency short-circuit
# ---------------------------------------------------------------------------

def test_empty_actor_rejected_even_for_duplicate():
    led = Ledger.seed()
    led.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    # The retry is byte-identical — but the caller is not authenticated, so
    # it must be rejected instead of returning the duplicate no-op.
    with pytest.raises(AuthorizationError):
        led.record_receipt(_receipt(), actor="owner",
                           ctx=_ctx(actor=""))
    with pytest.raises(AuthorizationError):
        led.record_receipt(_receipt(), actor="owner",
                           ctx=_ctx(actor="   "))
    # And the failed attempt recorded nothing new.
    assert len(led.receipts) == 1


def test_unknown_role_rejected():
    led = Ledger.seed()
    with pytest.raises(AuthorizationError):
        led.record_receipt(_receipt(), actor="owner",
                           ctx=_ctx(role="superuser"))
    with pytest.raises(AuthorizationError):
        led.apply_event("evt-delay-001", ctx=_ctx(role="admin"))


def test_viewer_cannot_mutate():
    led = Ledger.seed()
    viewer = _ctx(role="viewer")
    with pytest.raises(AuthorizationError):
        led.record_receipt(_receipt(), actor="owner", ctx=viewer)
    with pytest.raises(AuthorizationError):
        led.apply_event("evt-delay-001", ctx=viewer)
    with pytest.raises(AuthorizationError):
        led.mark_paid("ops-001", OCT_28, "owner", "ev-ops-budget", ctx=viewer)
    # Nothing changed.
    assert led.receipts == {}
    assert led.applied_event_ids == set()


def test_agent_can_create_draft_but_cannot_mutate():
    led = Ledger.seed()
    agent = _ctx(actor="agent-7", role="agent")
    act = actions_mod.create_action(
        "act-agent-1", "request_early_payment", {"x": 1},
        actor="agent-7", ctx=agent, business_id=led.business_id)
    assert act.state == "draft"
    # But the agent cannot move the draft or touch the ledger.
    with pytest.raises(AuthorizationError):
        actions_mod.approve(act, actor="agent-7", ctx=agent)
    with pytest.raises(AuthorizationError):
        led.record_receipt(_receipt(), actor="agent-7", ctx=agent)
    with pytest.raises(AuthorizationError):
        led.apply_event("evt-delay-001", ctx=agent)


def test_apply_event_auth_before_idempotency():
    led = Ledger.seed()
    assert led.apply_event("evt-delay-001", ctx=_ctx())["changed"] is True
    # Re-applying is idempotent for an authorized caller...
    assert led.apply_event("evt-delay-001", ctx=_ctx())["already_applied"] is True
    # ...but an unauthenticated caller is rejected before the no-op.
    with pytest.raises(AuthorizationError):
        led.apply_event("evt-delay-001", ctx=_ctx(actor=""))


def test_mark_paid_auth_before_idempotency():
    led = Ledger.seed()
    r1 = led.mark_paid("ops-001", OCT_28, "owner", "ev-ops-budget", ctx=_ctx())
    assert r1["changed"] is True
    r2 = led.mark_paid("ops-001", OCT_28, "owner", "ev-ops-budget", ctx=_ctx())
    assert r2["duplicate"] is True
    with pytest.raises(AuthorizationError):
        led.mark_paid("ops-001", OCT_28, "owner", "ev-ops-budget",
                      ctx=_ctx(actor=""))


def test_action_transition_rejects_bad_context_first():
    led = Ledger.seed()
    act = actions_mod.create_action("act-x", "request_early_payment", {},
                                    actor="owner", ctx=_ctx(),
                                    business_id=led.business_id)
    with pytest.raises(AuthorizationError):
        actions_mod.approve(act, actor="owner", ctx=_ctx(actor=""))
    assert act.state == "draft"  # unchanged


# ---------------------------------------------------------------------------
# Business scoping of idempotency keys
# ---------------------------------------------------------------------------

def test_wrong_business_id_rejected():
    other = Ledger.seed()
    other.business_id = "other-biz"
    other_ctx = _ctx(business_id="other-biz")
    foreign_ctx = _ctx(business_id=BIZ)  # right actor, wrong business for `other`
    with pytest.raises(AuthorizationError):
        other.record_receipt(_receipt(), actor="owner", ctx=foreign_ctx)
    with pytest.raises(AuthorizationError):
        other.apply_event("evt-delay-001", ctx=foreign_ctx)
    with pytest.raises(AuthorizationError):
        other.mark_paid("ops-001", OCT_28, "owner", "ev-ops-budget",
                        ctx=foreign_ctx)
    # The correctly-scoped context works fine.
    assert other.record_receipt(_receipt(), actor="owner",
                                ctx=other_ctx)["changed"] is True
    assert other.receipts["rcpt-t1"].business_id == "other-biz"


def test_cross_business_receipt_id_reuse_rejected():
    led = Ledger.seed()
    led.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    other = Ledger.seed()
    other.business_id = "other-biz"
    # Same receipt id, but the receipt is stamped for a different business
    # than the ledger it is presented to -> refused.
    with pytest.raises(AuthorizationError):
        other.record_receipt(_receipt(business_id="demo-agency"), actor="owner",
                             ctx=_ctx(business_id="other-biz"))
    # Same receipt id with no preset business is stamped to `other` and is a
    # distinct (business_id, id) key — allowed, no cross-contamination.
    out = other.record_receipt(_receipt(), actor="owner",
                               ctx=_ctx(business_id="other-biz"))
    assert out["changed"] is True
    assert other.receipts["rcpt-t1"].business_id == "other-biz"
    assert led.receipts["rcpt-t1"].business_id == BIZ


def test_conflicting_content_same_key_rejected():
    led = Ledger.seed()
    led.record_receipt(_receipt(amount=500_000), actor="owner", ctx=_ctx())
    with pytest.raises(IdempotencyConflict):
        led.record_receipt(_receipt(amount=400_000), actor="owner", ctx=_ctx())


# ---------------------------------------------------------------------------
# Authorized retry returns the original result without re-executing
# ---------------------------------------------------------------------------

def test_authorized_retry_returns_original_without_reexecution():
    led = Ledger.seed()
    first = led.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    assert first["changed"] is True and first["duplicate"] is False
    audit_before = len(led.audit)

    second = led.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    assert second == {"receipt_id": "rcpt-t1", "duplicate": True,
                      "changed": False}
    # No re-execution: exactly one allocation and one audit entry.
    allocs = [a for a in led.receivables["inv-client-001"].allocations
              if a.receipt_id == "rcpt-t1"]
    assert len(allocs) == 1
    assert len(led.audit) == audit_before
    recorded = [e for e in led.audit if e["event"] == "receipt_recorded"]
    assert len(recorded) == 1
    assert led.receivables["inv-client-001"].remaining_minor == 300_000


def test_duplicate_retry_does_not_reauthorize_moved_action():
    """The original recording passed the trust scan + approval; the retry
    must not re-run authorize_mutation (the action has since moved on)."""
    led = Ledger.seed()
    act = _approved_action(led)
    led.record_receipt(_receipt(), actor="owner", ctx=_ctx(),
                       authorizing_action=act)
    actions_mod.observe_payment(act, "rcpt-t1", actor="owner", ctx=_ctx())
    assert act.state == "payment_observed"  # no longer an authorizing state
    # Retry with the moved-on action: still a clean duplicate no-op, not an
    # ApprovalInvalidatedError.
    out = led.record_receipt(_receipt(), actor="owner", ctx=_ctx(),
                             authorizing_action=act)
    assert out["duplicate"] is True


# ---------------------------------------------------------------------------
# New executions still need a current valid approval
# ---------------------------------------------------------------------------

def test_new_execution_without_valid_approval_rejected():
    led = Ledger.seed()
    draft = actions_mod.create_action("act-d", "request_early_payment",
                                      {"receivable_id": "inv-client-001"},
                                      actor="owner", ctx=_ctx(),
                                      business_id=led.business_id)
    with pytest.raises(ApprovalInvalidatedError):
        led.record_receipt(_receipt("rcpt-new"), actor="owner", ctx=_ctx(),
                           authorizing_action=draft)
    assert "rcpt-new" not in led.receipts

    approved = _approved_action(led, action_id="act-a2")
    actions_mod.update_terms(approved, {"amount_minor": 1}, actor="owner",
                             ctx=_ctx())  # invalidates the approval
    with pytest.raises(ApprovalInvalidatedError):
        led.record_receipt(_receipt("rcpt-new2"), actor="owner", ctx=_ctx(),
                           authorizing_action=approved)
    assert "rcpt-new2" not in led.receipts


# ---------------------------------------------------------------------------
# Persistence: business-scoped idempotency survives restarts; old DBs migrate
# ---------------------------------------------------------------------------

def test_receipt_business_scoping_persists(tmp_path):
    from cashflow_copilot.store import SQLiteStore
    db = str(tmp_path / "scoped.sqlite")
    store = SQLiteStore(db)
    led = Ledger.seed()
    led.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    store.save_ledger(led)
    store.close()

    store2 = SQLiteStore(db)
    revived = store2.load_ledger()
    assert revived.receipts["rcpt-t1"].business_id == BIZ
    # The composite key is real in SQLite.
    cols = store2.conn.execute("PRAGMA table_info(receipts)").fetchall()
    assert "business_id" in [c[1] for c in cols]
    pk = store2.conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='receipts'").fetchone()[0]
    assert "PRIMARY KEY (business_id, id)" in pk
    # Idempotency holds after the restart.
    out = revived.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    assert out["duplicate"] is True
    store2.close()


def _downgrade_to_pre_scoping_layout(db):
    """Rewrite the receipts/actions tables to their pre-scoping layout
    (no business_id column), simulating an old database file."""
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE receipts RENAME TO receipts_new")
    conn.execute(
        "CREATE TABLE receipts (id TEXT PRIMARY KEY, "
        "receivable_id TEXT NOT NULL, amount_minor INTEGER NOT NULL, "
        "date_received TEXT NOT NULL, state TEXT NOT NULL, "
        "evidence_id TEXT NOT NULL, actor TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO receipts (id, receivable_id, amount_minor, "
        "date_received, state, evidence_id, actor) SELECT id, receivable_id, "
        "amount_minor, date_received, state, evidence_id, actor "
        "FROM receipts_new")
    conn.execute("DROP TABLE receipts_new")
    conn.execute("ALTER TABLE actions RENAME TO actions_new")
    conn.execute(
        "CREATE TABLE actions (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
        "terms_json TEXT NOT NULL, actor TEXT NOT NULL, version INTEGER NOT NULL, "
        "state TEXT NOT NULL, approved_version INTEGER, counterparty_json TEXT, "
        "audit_json TEXT NOT NULL)")
    conn.execute(
        "INSERT INTO actions (id, kind, terms_json, actor, version, state, "
        "approved_version, counterparty_json, audit_json) SELECT id, kind, "
        "terms_json, actor, version, state, approved_version, "
        "counterparty_json, audit_json FROM actions_new")
    conn.execute("DROP TABLE actions_new")
    conn.commit()
    conn.close()


def test_old_database_migrates_additively(tmp_path):
    from cashflow_copilot.store import SQLiteStore
    db = str(tmp_path / "old.sqlite")
    store = SQLiteStore(db)
    led = Ledger.seed()
    led.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    act = _approved_action(led)
    led.actions[act.id] = act
    store.save_ledger(led)
    store.close()

    _downgrade_to_pre_scoping_layout(db)

    # Reopening must migrate additively and keep everything working.
    store2 = SQLiteStore(db)
    revived = store2.load_ledger()
    assert revived.receipts["rcpt-t1"].business_id == BIZ  # stamped on load
    assert revived.actions["act-t1"].business_id == BIZ
    out = revived.record_receipt(_receipt(), actor="owner", ctx=_ctx())
    assert out["duplicate"] is True  # idempotency intact across migration
    # New writes carry the business scope.
    out2 = revived.record_receipt(_receipt("rcpt-after", amount=300_000),
                                  actor="owner", ctx=_ctx())
    assert out2["changed"] is True
    store2.save_ledger(revived)
    store2.close()
    store3 = SQLiteStore(db)
    again = store3.load_ledger()
    assert again.receipts["rcpt-after"].business_id == BIZ
    store3.close()


# ---------------------------------------------------------------------------
# Threaded duplicate POSTs through the real HTTP server: exactly one effect
# ---------------------------------------------------------------------------

def _live_server(tmp_path, monkeypatch):
    from cashflow_copilot import web
    from cashflow_copilot.cli import cmd_seed
    db = str(tmp_path / "authz.sqlite")
    monkeypatch.setattr(web, "DB_PATH", db)
    cmd_seed(db)
    server = web.ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{port}", db


def _get(base, path):
    with urllib.request.urlopen(base + path) as r:
        return r.status, r.read().decode("utf-8")


def _post(base, path, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(base + path, data=data)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def test_threaded_duplicate_observe_posts_single_receipt(tmp_path, monkeypatch):
    from cashflow_copilot.store import SQLiteStore
    server, thread, base, db = _live_server(tmp_path, monkeypatch)
    try:
        _, html = _get(base, "/demo")
        csrf = re.search(r"name='csrf' value='([0-9a-f]+)'", html).group(1)
        F = lambda **kw: dict(csrf=csrf, **kw)

        # Drive one action to counterparty_accepted over HTTP.
        assert _post(base, "/actions/create", F(
            action_id="act-race", kind="request_early_payment",
            receivable_id="inv-client-001", amount_major="5000.00",
            date="2026-10-22"))[0] == 200
        for step in ("approve", "send"):
            assert _post(base, f"/actions/act-race/{step}", F())[0] == 200, step
        assert _post(base, "/actions/act-race/respond",
                     F(accepted="true"))[0] == 200

        # N threads POST the identical receipt concurrently.
        barrier = threading.Barrier(8)
        statuses = []

        def worker():
            barrier.wait(timeout=10)
            s, _ = _post(base, "/actions/act-race/observe", F(
                receipt_id="rcpt-thread", amount_major="5000.00",
                date_received="2026-10-22"))
            statuses.append(s)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert len(statuses) == 8
        assert all(s == 200 for s in statuses), statuses

        store = SQLiteStore(db)
        try:
            led = store.load_ledger()
        finally:
            store.close()
        assert len(led.receipts) == 1
        assert "rcpt-thread" in led.receipts
        allocs = [a for a in led.receivables["inv-client-001"].allocations
                  if a.receipt_id == "rcpt-thread"]
        assert len(allocs) == 1  # exactly one allocation
        recorded = [e for e in led.audit if e["event"] == "receipt_recorded"]
        assert len(recorded) == 1  # exactly one audit entry
        assert led.receivables["inv-client-001"].remaining_minor == 300_000
        assert led.actions["act-race"].state == "payment_observed"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_require_access_rejects_non_context():
    led = Ledger.seed()
    with pytest.raises(AuthorizationError):
        require_access(None, led)
    with pytest.raises(AuthorizationError):
        require_access("owner", led)  # not a RequestContext
    # ensure() with None resolves to the implicit owner path (legacy/tests).
    ctx = ensure(None, led)
    assert (ctx.actor, ctx.role, ctx.business_id) == ("owner", "owner", BIZ)
