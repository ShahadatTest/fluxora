"""Cashflow Action Copilot: deterministic calculations and a local sandbox agent.

No binary floating point is used for money anywhere in this package.
All amounts are integer minor units (e.g. cents); Decimal is used only
for display formatting and for parsing FX rate strings.
"""

from .money import format_amount, minor_digits
from .errors import (
    CashflowError,
    FixtureValidationError,
    IdempotencyConflict,
    OverpaymentError,
    ProtectedObligationError,
    MissingConsentError,
    ApprovalInvalidatedError,
    UntrustedDocumentError,
    InvalidTransitionError,
)

__all__ = [
    "format_amount",
    "minor_digits",
    "CashflowError",
    "FixtureValidationError",
    "IdempotencyConflict",
    "OverpaymentError",
    "ProtectedObligationError",
    "MissingConsentError",
    "ApprovalInvalidatedError",
    "UntrustedDocumentError",
    "InvalidTransitionError",
]
