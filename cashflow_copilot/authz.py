"""Server-side caller authentication and business authorization.

Every mutating engine entry point (ledger methods and action state
transitions) calls `require_access` — or `ensure`, which resolves a
missing context to the legacy implicit-owner path — as its FIRST step,
before any idempotency short-circuit. A retry of an already-recorded
operation still returns the stored result without re-executing, but only
for an authenticated caller whose business matches the ledger's.

Roles:
- "owner": full access (subject to the existing workflow rules).
- "agent": read + draft-creation only (the agent tool layer relies on this).
- "viewer": read-only.

RequestContext objects are constructed server-side (web.py builds them
from the loaded ledger, never from client-supplied form values).
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import AuthorizationError

OWNER = "owner"
AGENT = "agent"
VIEWER = "viewer"

KNOWN_ROLES = frozenset({OWNER, AGENT, VIEWER})

# Operations checked by require_access.
OP_READ = "read"
OP_DRAFT = "draft"      # draft-creation: owner + agent
OP_MUTATE = "mutate"    # any other state change: owner only


@dataclass(frozen=True)
class RequestContext:
    """Who is calling, in what capacity, against which business.

    Frozen so a handler cannot accidentally mutate it after the check.
    """

    actor: str
    role: str  # one of "owner" | "agent" | "viewer"
    business_id: str


def require_access(ctx: RequestContext | None, scope,
                   operation: str = OP_MUTATE) -> RequestContext:
    """Authenticate the caller and authorize the operation. First step of
    every mutating entry point — called before any idempotency check.

    `scope` is the business being mutated: a Ledger, or any object exposing
    `business_id` (e.g. an Action). An empty scope business_id means a
    legacy/unscoped object, for which the business check is vacuous; actor
    and role are still validated.

    Raises AuthorizationError on: missing context, empty actor, unknown
    role, business mismatch, or a role that may not perform `operation`.
    """
    if ctx is None:
        raise AuthorizationError(
            "a request context is required at the mutation boundary")
    if not isinstance(ctx, RequestContext):
        raise AuthorizationError(
            f"request context must be a RequestContext, got {type(ctx).__name__}")
    if not ctx.actor or not str(ctx.actor).strip():
        raise AuthorizationError(
            "empty actor: the caller must be identified server-side")
    if ctx.role not in KNOWN_ROLES:
        raise AuthorizationError(
            f"unknown role {ctx.role!r}: expected one of {sorted(KNOWN_ROLES)}")
    scope_business = getattr(scope, "business_id", "") or ""
    if scope_business and ctx.business_id != scope_business:
        raise AuthorizationError(
            f"business mismatch: context is for business {ctx.business_id!r} "
            f"but the ledger belongs to {scope_business!r}")
    if operation == OP_MUTATE and ctx.role != OWNER:
        raise AuthorizationError(
            f"role {ctx.role!r} may not perform mutating operations")
    if operation == OP_DRAFT and ctx.role not in (OWNER, AGENT):
        raise AuthorizationError(
            f"role {ctx.role!r} may not create drafts")
    if operation not in (OP_READ, OP_DRAFT, OP_MUTATE):
        raise AuthorizationError(f"unknown operation {operation!r}")
    return ctx


def ensure(ctx: RequestContext | None, scope,
           operation: str = OP_MUTATE) -> RequestContext:
    """Resolve a possibly-missing context, then require_access.

    `ctx=None` is the legacy/test path: a direct engine caller with no
    context is treated as the trusted owner of the scope's own business.
    The web layer never relies on this default — every HTTP mutation
    handler builds an explicit server-side RequestContext and passes it
    through.
    """
    if ctx is None:
        ctx = RequestContext(
            actor=OWNER, role=OWNER,
            business_id=getattr(scope, "business_id", "") or "")
    return require_access(ctx, scope, operation=operation)


def owner_context_for(business_id: str) -> RequestContext:
    """Build the server-side owner context for a business. Never built
    from client-supplied values."""
    return RequestContext(actor=OWNER, role=OWNER, business_id=business_id)
