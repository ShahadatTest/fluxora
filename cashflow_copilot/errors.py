"""Exception hierarchy for the calculation engine."""


class CashflowError(Exception):
    """Base class for all engine errors."""


class FixtureValidationError(CashflowError):
    """Raised when the synthetic fixture fails validation.

    Carries the full list of problems found so a reviewer can fix them all
    at once instead of one at a time.
    """

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__("fixture validation failed:\n- " + "\n- ".join(self.problems))


class IdempotencyConflict(CashflowError):
    """Same stable ID reused with different payload — never silently merged."""


class OverpaymentError(CashflowError):
    """A receipt allocation exceeded the invoice's remaining balance.

    Overpayments are refused explicitly; they are never silently truncated
    or spread across other invoices. A caller that wants to handle an
    overpayment (e.g. hold the excess as an unallocated credit) must do so
    through a separate, explicit code path.
    """


class ProtectedObligationError(CashflowError):
    """A plan tried to move a protected (hard) obligation."""


class MissingConsentError(CashflowError):
    """A change that needs counterparty consent was proposed without it."""


class ApprovalInvalidatedError(CashflowError):
    """An approval was used after its terms changed."""


class UntrustedDocumentError(CashflowError):
    """A document failed the authorization-boundary scan.

    Documents are evidence, never authorization. A document whose text
    contains instruction-like phrases must not authorize a financial
    mutation.
    """


class InvalidTransitionError(CashflowError):
    """An action state transition that the workflow does not allow."""


class AuthorizationError(CashflowError):
    """Caller authentication or business authorization failed.

    Raised at the mutation boundary, before any idempotency short-circuit,
    when the request context has an empty/unknown actor, an unknown role, a
    role that may not perform the operation, or a business_id that does not
    match the ledger being mutated.
    """
