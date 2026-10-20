"""Action workflow lifecycle: explicit states, validated transitions,
no duplicate actions from reminder retries."""

import pytest

from cashflow_copilot import actions
from cashflow_copilot.errors import ApprovalInvalidatedError, InvalidTransitionError


def _payment_action():
    return actions.create_action(
        "act-1", "request_early_payment",
        {"receivable_id": "inv-client-001", "amount_minor": 500_000,
         "date": "2026-10-22"},
        actor="owner")


def test_full_lifecycle_to_reconciled():
    action = _payment_action()
    assert action.state == "draft"
    actions.approve(action, actor="owner")
    assert action.state == "owner_approved"
    outbox = actions.send(action, actor="owner")
    assert action.state == "sent"
    assert outbox["simulated"] is True
    resp = actions.counterparty_respond(action, True, "ev-supplier-accept")
    assert action.state == "counterparty_accepted"
    assert resp["simulated"] is True
    actions.observe_payment(action, "rcpt-001", actor="owner")
    assert action.state == "payment_observed"
    actions.reconcile(action, actor="owner")
    assert action.state == "reconciled"


def test_rejected_counterparty_path():
    action = _payment_action()
    actions.approve(action, actor="owner")
    actions.send(action, actor="owner")
    actions.counterparty_respond(action, False, "ev-supplier-reject")
    assert action.state == "counterparty_rejected"


def test_invalid_transitions_raise():
    action = _payment_action()
    with pytest.raises(InvalidTransitionError):
        actions.reconcile(action, actor="owner")  # draft -> reconciled
    with pytest.raises(InvalidTransitionError):
        actions.send(action, actor="owner")  # draft -> sent skips approval


def test_reminder_retry_creates_no_duplicate_action():
    action = _payment_action()
    actions.approve(action, actor="owner")
    actions.send(action, actor="owner")
    result = actions.retry_reminder(action, actor="owner")
    assert action.state == "sent"
    assert result["duplicate_created"] is False
    assert any(e["event"] == "reminder_retry" for e in action.audit)


def test_mutation_authorized_only_while_approval_current():
    action = _payment_action()
    with pytest.raises(ApprovalInvalidatedError):
        actions.authorize_mutation(action, [], actor="owner")  # still draft
    actions.approve(action, actor="owner")
    actions.authorize_mutation(action, [], actor="owner")  # no error


def test_audit_trail_records_every_transition():
    action = _payment_action()
    actions.approve(action, actor="owner")
    actions.send(action, actor="owner")
    events = [e["event"] for e in action.audit]
    assert events == ["created", "owner_approved", "sent_to_sandbox_outbox"]
    assert all("at" in e and "actor" in e for e in action.audit)
