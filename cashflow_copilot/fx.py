"""Display-only FX conversion.

A base-currency display conversion is not an executed FX transaction:
converting for display never creates funds and never covers a
per-currency cash gap. Missing or stale inputs are visible in the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import FxQuote
from .money import minor_digits


@dataclass
class ConversionResult:
    value: Decimal | None  # display value in the target currency
    status: str  # 'ok' | 'stale' | 'missing' | 'same_currency'
    note: str


def _quantize(value: Decimal, currency: str) -> Decimal:
    digits = minor_digits(currency)
    return value.quantize(Decimal(10) ** -digits)


def display_convert(amount_minor: int, from_ccy: str, to_ccy: str,
                   quotes: list[FxQuote], as_of: date) -> ConversionResult:
    """Convert for display only. Pure function: it cannot touch balances."""
    if from_ccy == to_ccy:
        return ConversionResult(
            _quantize(Decimal(amount_minor) * Decimal(10) ** -minor_digits(from_ccy),
                      to_ccy),
            "same_currency", "No conversion needed.")
    quote = next((q for q in quotes
                  if q.from_ccy == from_ccy and q.to_ccy == to_ccy), None)
    rate = None
    if quote is None:
        inv = next((q for q in quotes
                    if q.from_ccy == to_ccy and q.to_ccy == from_ccy), None)
        if inv is not None:
            quote = inv
            rate = Decimal(1) / inv.rate
    else:
        rate = quote.rate
    if quote is None:
        return ConversionResult(
            None, "missing",
            f"No FX quote for {from_ccy}->{to_ccy}; value unknown, "
            "never assumed. Display only — no funds created.")
    amount = Decimal(amount_minor) * Decimal(10) ** -minor_digits(from_ccy)
    value = _quantize(amount * rate, to_ccy)
    if quote.as_of < as_of:
        return ConversionResult(
            value, "stale",
            f"Quote dated {quote.as_of.isoformat()} is older than the "
            f"{as_of.isoformat()} snapshot; treated as stale. Display only — "
            "no funds created.")
    return ConversionResult(value, "ok", f"Quote {quote.source} as of "
                                         f"{quote.as_of.isoformat()}. Display only.")
