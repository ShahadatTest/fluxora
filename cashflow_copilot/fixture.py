"""Fixture loading and validation.

The synthetic fixture (section 6 of the build brief) is embedded as a seed
file. Every material input must carry a source/evidence ID; missing values
stay unknown (validation fails loudly rather than inventing defaults).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import InvalidOperation
from pathlib import Path

from .errors import FixtureValidationError
from .money import parse_rate

FIXTURE_PATH = Path(__file__).parent / "data" / "fixture_v1.json"

REQUIRED_TOP_KEYS = {
    "fixture_version",
    "synthetic",
    "business",
    "accounts",
    "display_fx",
    "receivables",
    "outflows",
    "events",
    "evidence",
}

CURRENCY_RE = r"^[A-Z]{3}$"
import re as _re


def load_fixture(path: str | Path = FIXTURE_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _parse_date(value, what: str, problems: list[str]) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        problems.append(f"{what}: date must be an ISO string, got {value!r}")
        return None
    try:
        y, m, d = (int(p) for p in value.split("-"))
        return date(y, m, d)
    except (ValueError, TypeError):
        problems.append(f"{what}: invalid date {value!r}")
        return None


def _check_int(value, what: str, problems: list[str], minimum: int = 0) -> bool:
    # bool is a subclass of int — reject it explicitly; money is never boolean.
    if isinstance(value, bool) or not isinstance(value, int):
        problems.append(f"{what}: must be an integer, got {value!r}")
        return False
    if value < minimum:
        problems.append(f"{what}: must be >= {minimum}, got {value}")
        return False
    return True


def validate_fixture(data: dict) -> dict:
    """Validate the fixture; return it unchanged if valid.

    Raises FixtureValidationError with every problem found.
    """
    problems: list[str] = []

    if not isinstance(data, dict):
        raise FixtureValidationError(["fixture root must be a JSON object"])

    for key in REQUIRED_TOP_KEYS:
        if key not in data:
            problems.append(f"missing top-level key: {key}")

    if data.get("fixture_version") != "1.0":
        problems.append(
            f"unsupported fixture_version: {data.get('fixture_version')!r} (expected '1.0')"
        )
    if data.get("synthetic") is not True:
        problems.append("fixture must be explicitly marked synthetic: true")

    business = data.get("business") or {}
    _parse_date(business.get("as_of"), "business.as_of", problems)
    horizon = business.get("horizon_days")
    _check_int(horizon, "business.horizon_days", problems, minimum=1)
    for bkey in ("id", "name", "base_currency", "timezone"):
        if not business.get(bkey):
            problems.append(f"business.{bkey} is required")

    evidence_ids: set[str] = set()
    for i, ev in enumerate(data.get("evidence") or []):
        if not isinstance(ev, dict):
            problems.append(f"evidence[{i}] must be an object")
            continue
        eid = ev.get("id")
        if not eid:
            problems.append(f"evidence[{i}] missing id")
        elif eid in evidence_ids:
            problems.append(f"duplicate evidence id: {eid}")
        else:
            evidence_ids.add(eid)
        if not ev.get("kind"):
            problems.append(f"evidence {eid!r} missing kind")

    account_currencies: set[str] = set()
    account_ids: set[str] = set()
    for i, acc in enumerate(data.get("accounts") or []):
        what = f"accounts[{i}]"
        aid = acc.get("id")
        if not aid:
            problems.append(f"{what} missing id")
        elif aid in account_ids:
            problems.append(f"duplicate account id: {aid}")
        else:
            account_ids.add(aid)
        ccy = acc.get("currency")
        if not isinstance(ccy, str) or not _re.match(CURRENCY_RE, ccy):
            problems.append(f"{what} has invalid currency: {ccy!r}")
        else:
            account_currencies.add(ccy)
        _check_int(acc.get("opening_minor"), f"{what}.opening_minor", problems)
        _check_int(acc.get("floor_minor"), f"{what}.floor_minor", problems)

    for i, fx in enumerate(data.get("display_fx") or []):
        what = f"display_fx[{i}]"
        for ckey in ("from", "to"):
            ccy = fx.get(ckey)
            if not isinstance(ccy, str) or not _re.match(CURRENCY_RE, ccy):
                problems.append(f"{what} has invalid currency {ckey}: {ccy!r}")
        rate = fx.get("rate")
        if isinstance(rate, float):
            problems.append(
                f"{what}.rate must be a decimal string, never a binary float"
            )
        else:
            try:
                parse_rate(rate)
            except (InvalidOperation, ValueError, TypeError, AttributeError):
                problems.append(f"{what}.rate is not a valid positive decimal: {rate!r}")
        _parse_date(fx.get("as_of"), f"{what}.as_of", problems)
        if not fx.get("source"):
            problems.append(f"{what} missing source (evidence ID)")

    receivable_ids: set[str] = set()
    for i, rec in enumerate(data.get("receivables") or []):
        what = f"receivables[{i}]"
        rid = rec.get("id")
        if not rid:
            problems.append(f"{what} missing id")
        elif rid in receivable_ids:
            problems.append(f"duplicate receivable id: {rid}")
        else:
            receivable_ids.add(rid)
        ccy = rec.get("currency")
        if ccy not in account_currencies:
            problems.append(f"{what} currency {ccy!r} has no account")
        _check_int(rec.get("original_minor"), f"{what}.original_minor", problems, minimum=1)
        received = rec.get("received_minor", 0)
        if _check_int(received, f"{what}.received_minor", problems):
            if isinstance(rec.get("original_minor"), int) and received > rec["original_minor"]:
                problems.append(f"{what}.received_minor exceeds original_minor")
        _parse_date(rec.get("expected_date"), f"{what}.expected_date", problems)
        _parse_date(rec.get('due_date'), f'{what}.due_date', problems)
        for field in ['counterparty', 'invoice_reference']:
            if not isinstance(rec.get(field, ''), str) or len(rec.get(field, '')) > 200:
                problems.append(f'{what}.{field} must be text of at most 200 characters')
        evid = rec.get("evidence_id")
        if not evid:
            problems.append(f"{what} missing evidence_id (material inputs need a source)")
        elif evid not in evidence_ids:
            problems.append(f"{what} references unknown evidence {evid!r}")

    outflow_ids: set[str] = set()
    for i, out in enumerate(data.get("outflows") or []):
        what = f"outflows[{i}]"
        oid = out.get("id")
        if not oid:
            problems.append(f"{what} missing id")
        elif oid in outflow_ids:
            problems.append(f"duplicate outflow id: {oid}")
        else:
            outflow_ids.add(oid)
        if out.get("currency") not in account_currencies:
            problems.append(f"{what} currency {out.get('currency')!r} has no account")
        _check_int(out.get("amount_minor"), f"{what}.amount_minor", problems, minimum=1)
        _parse_date(out.get("date"), f"{what}.date", problems)
        for field in ['counterparty', 'invoice_reference']:
            if not isinstance(out.get(field, ''), str) or len(out.get(field, '')) > 200:
                problems.append(f'{what}.{field} must be text of at most 200 characters')
        if not isinstance(out.get("protected"), bool):
            problems.append(f"{what}.protected must be a boolean")
        maxdef = out.get("maximum_deferrable_minor", 0)
        if _check_int(maxdef, f"{what}.maximum_deferrable_minor", problems):
            if isinstance(out.get("amount_minor"), int) and maxdef > out["amount_minor"]:
                problems.append(f"{what}.maximum_deferrable_minor exceeds amount_minor")
        if not isinstance(out.get("counterparty_consent_required_for_change", False), bool):
            problems.append(f"{what}.counterparty_consent_required_for_change must be a boolean")
        evid = out.get("evidence_id")
        if not evid:
            problems.append(f"{what} missing evidence_id (material inputs need a source)")
        elif evid not in evidence_ids:
            problems.append(f"{what} references unknown evidence {evid!r}")

    event_ids: set[str] = set()
    for i, evt in enumerate(data.get("events") or []):
        what = f"events[{i}]"
        eid = evt.get("id")
        if not eid:
            problems.append(f"{what} missing id")
        elif eid in event_ids:
            problems.append(f"duplicate event id: {eid}")
        else:
            event_ids.add(eid)
        if evt.get("type") not in {"client_delay"}:
            problems.append(f"{what} has unknown type: {evt.get('type')!r}")
        if evt.get("receivable_id") not in receivable_ids:
            problems.append(f"{what} references unknown receivable {evt.get('receivable_id')!r}")
        _parse_date(evt.get("new_expected_date"), f"{what}.new_expected_date", problems)
        _parse_date(evt.get("observed_date"), f"{what}.observed_date", problems)
        evid = evt.get("evidence_id")
        if not evid:
            problems.append(f"{what} missing evidence_id (material inputs need a source)")
        elif evid not in evidence_ids:
            problems.append(f"{what} references unknown evidence {evid!r}")

    if problems:
        raise FixtureValidationError(problems)
    return data


def seed_validated(path: str | Path = FIXTURE_PATH) -> dict:
    """Load and validate the embedded seed fixture."""
    return validate_fixture(load_fixture(path))
