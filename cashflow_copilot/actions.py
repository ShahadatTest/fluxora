"""Action workflow, approvals, and the mutation authorization boundary.

Explicit persisted states:
    draft -> owner_approved -> sent -> counterparty_accepted / counterparty_rejected
    -> payment_observed -> reconciled

Rules enforced here:
- A changed amount, recipient, due date or policy invalidates an earlier
  approval (version bump, back to draft).
- A reminder retry never creates a duplicate action.
- Owner approval alone cannot amend a supplier's terms (consent still
  required at plan evaluation).
- Documents are evidence, never authorization: text inside a document is
  scanned at the mutation boundary and instruction-like content refuses
  the mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import authz as authz_mod
from .errors import (
    ApprovalInvalidatedError,
    InvalidTransitionError,
    UntrustedDocumentError,
)
from .models import (
    ACTION_TRANSITIONS,
    MUTATION_AUTHORIZING_STATES,
    Action,
    Document,
)

# Deterministic heuristic scan. This is a boundary control, not a security
# proof: anything matching is refused, and documents can never authorize a
# mutation on their own regardless of content.
INJECTION_PATTERNS = (
    "ignore rules",
    "ignore the rules",
    "disregard instructions",
    "disregard the rules",
    "override approval",
    "bypass approval",
    "pay this address",
    "do not follow",
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _audit(action: Action, event: str, actor: str, detail: str = "") -> None:
    action.audit.append(
        {"at": _utcnow_iso(), "event": event, "actor": actor, "detail": detail,
         "state": action.state, "version": action.version}
    )


class _BusinessScope:
    """Minimal business scope for authorizing before the target object
    exists (e.g. draft creation)."""

    def __init__(self, business_id: str):
        self.business_id = business_id


def _authorize(ctx, action: Action) -> None:
    """First step of every mutating action transition: authenticate the
    caller and authorize them for the action's business. Owner role only —
    agents may create drafts but not move them."""
    authz_mod.ensure(ctx, action, operation=authz_mod.OP_MUTATE)


def scan_document(doc: Document) -> list[str]:
    """Return the injection patterns found in the document text."""
    lowered = doc.content.lower()
    return [p for p in INJECTION_PATTERNS if p in lowered]


def require_trustworthy(documents: list[Document]) -> None:
    """Refuse the mutation if any document looks like an instruction.

    Raises UntrustedDocumentError naming the offending document and pattern.
    """
    for doc in documents:
        hits = scan_document(doc)
        if hits:
            raise UntrustedDocumentError(
                f"document {doc.id!r} cannot authorize a financial mutation: "
                f"it contains instruction-like text ({', '.join(hits)}). "
                "Documents are evidence, never authorization."
            )


def create_action(action_id: str, kind: str, terms: dict, actor: str = "owner",
                  ctx=None, business_id: str = "") -> Action:
    """Create a draft action. Draft creation is allowed for owner and agent
    roles (agent = read + draft-creation only); the caller is authorized
    against the action's business before anything is created."""
    authz_mod.ensure(ctx, _BusinessScope(business_id), operation=authz_mod.OP_DRAFT)
    action = Action(id=action_id, kind=kind, terms=dict(terms), actor=actor,
                    business_id=business_id)
    _audit(action, "created", actor, f"kind={kind}")
    return action


def _transition(action: Action, to_state: str, actor: str, event: str,
                detail: str = "") -> None:
    if to_state not in ACTION_TRANSITIONS.get(action.state, set()):
        raise InvalidTransitionError(
            f"cannot move action {action.id} from {action.state} to {to_state}"
        )
    action.state = to_state
    _audit(action, event, actor, detail)


def approve(action: Action, actor: str = "owner", ctx=None) -> None:
    """Owner approval pins the current terms version."""
    _authorize(ctx, action)
    _transition(action, "owner_approved", actor, "owner_approved",
                f"terms version {action.version}")
    action.approved_version = action.version


def cancel_draft(action: Action, actor: str = 'owner', ctx=None) -> None:
    """Owner may discard an unapproved draft; retain its audit and identifier."""
    _authorize(ctx, action)
    _transition(action, 'cancelled', actor, 'draft_cancelled', 'Unapproved draft discarded by owner; no financial effect.')


def update_terms(action: Action, new_terms: dict, actor: str, ctx=None) -> None:
    """Any change to amount, recipient, due date or policy invalidates the
    earlier approval: version bumps and the action returns to draft."""
    _authorize(ctx, action)
    action.terms = dict(new_terms)
    action.version += 1
    action.approved_version = None
    action.state = "draft"
    _audit(action, "terms_updated", actor,
            f"approval invalidated; now terms version {action.version}")


def send(action: Action, actor: str = "owner", ctx=None) -> dict:
    """Place the action in the sandbox outbox. Explicitly labelled simulated."""
    _authorize(ctx, action)
    _transition(action, "sent", actor, "sent_to_sandbox_outbox")
    return {"action_id": action.id, "simulated": True,
            "note": "sandbox outbox: nothing was really sent"}


def retry_reminder(action: Action, actor: str = "owner", ctx=None) -> dict:
    """A reminder retry is recorded on the same action — it never creates a
    duplicate action."""
    _authorize(ctx, action)
    _transition(action, "sent", actor, "reminder_retry",
                "no new action created")
    return {"action_id": action.id, "simulated": True, "duplicate_created": False}


def counterparty_respond(action: Action, accepted: bool, evidence_id: str,
                         actor: str = "sandbox_counterparty", ctx=None) -> dict:
    """Record a clearly labelled simulated counterparty response."""
    _authorize(ctx, action)
    to_state = "counterparty_accepted" if accepted else "counterparty_rejected"
    _transition(action, to_state, actor,
                "counterparty_response_simulated",
                f"accepted={accepted} evidence={evidence_id}")
    action.counterparty_response = {
        "accepted": accepted,
        "evidence_id": evidence_id,
        "simulated": True,
    }
    return {"action_id": action.id, "accepted": accepted, "simulated": True}


def observe_payment(action: Action, receipt_id: str, actor: str = "owner",
                    ctx=None) -> None:
    _authorize(ctx, action)
    _transition(action, "payment_observed", actor, "payment_observed",
                f"receipt={receipt_id}")


def reconcile(action: Action, actor: str = "owner", ctx=None) -> None:
    _authorize(ctx, action)
    _transition(action, "reconciled", actor, "reconciled")


def authorize_mutation(action: Action, documents: list[Document],
                       actor: str) -> None:
    """The mutation boundary.

    - The action must currently authorize mutations (approved and not
      superseded by a terms change).
    - Every cited document is scanned; instruction-like content refuses the
      mutation before anything changes.
    """
    require_trustworthy(documents)
    if action.state not in MUTATION_AUTHORIZING_STATES:
        raise ApprovalInvalidatedError(
            f"action {action.id} is in state {action.state!r}; only "
            f"{sorted(MUTATION_AUTHORIZING_STATES)} authorize a mutation"
        )
    if action.approved_version != action.version:
        raise ApprovalInvalidatedError(
            f"action {action.id}: terms changed after approval "
            f"(approved v{action.approved_version}, current v{action.version}); "
            "re-approval is required"
        )
    _audit(action, "mutation_authorized", actor,
            f"cited documents: {[d.id for d in documents]}")
