"""Money handling.

Non-negotiable rule: integer minor units (or exact Decimal) for money.
Binary floating point is never used for monetary amounts in this package.
"""

from decimal import Decimal, ROUND_HALF_UP

# ISO 4217 minor-unit digits for currencies we may encounter.
MINOR_DIGITS = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "BDT": 2,
    "CNY": 2,
    "JPY": 0,
}


def minor_digits(currency: str) -> int:
    return MINOR_DIGITS.get(currency.upper(), 2)


def format_amount(minor: int, currency: str) -> str:
    """Format integer minor units for display. Never used for arithmetic."""
    if not isinstance(minor, int):
        raise TypeError(
            f"money must be integer minor units, got {type(minor).__name__}"
        )
    digits = minor_digits(currency)
    quantum = Decimal(10) ** -digits
    value = (Decimal(minor) * quantum).quantize(quantum, rounding=ROUND_HALF_UP)
    return f"{currency} {value:,.{digits}f}"


def parse_rate(rate_str: str) -> Decimal:
    """Parse an FX rate from its string form. Rejects floats explicitly."""
    if isinstance(rate_str, float):
        raise TypeError("FX rates must be decimal strings, never binary floats")
    rate = Decimal(str(rate_str))
    if rate <= 0:
        raise ValueError(f"FX rate must be positive, got {rate_str!r}")
    return rate
