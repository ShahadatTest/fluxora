"""Fixture validation: the seed must load clean, and every class of
bad input must fail loudly instead of being silently accepted."""

import copy

import pytest

from cashflow_copilot.errors import FixtureValidationError
from cashflow_copilot.fixture import seed_validated, validate_fixture


def test_seed_fixture_is_valid(raw_fixture):
    assert validate_fixture(raw_fixture) is raw_fixture
    assert seed_validated()["fixture_version"] == "1.0"


def test_missing_top_level_key(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    del data["outflows"]
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert "missing top-level key: outflows" in exc.value.problems


def test_must_be_marked_synthetic(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["synthetic"] = False
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_float_fx_rate_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["display_fx"][0]["rate"] = 1.10  # binary float: never acceptable
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert any("never a binary float" in p for p in exc.value.problems)


def test_negative_opening_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["accounts"][0]["opening_minor"] = -1
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_bool_is_not_an_integer_amount(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["accounts"][0]["opening_minor"] = True
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_receivable_currency_needs_account(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["receivables"][0]["currency"] = "JPY"
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert any("has no account" in p for p in exc.value.problems)


def test_unknown_evidence_reference_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["outflows"][0]["evidence_id"] = "ev-does-not-exist"
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert any("unknown evidence" in p for p in exc.value.problems)


def test_missing_evidence_id_on_material_input(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    del data["receivables"][0]["evidence_id"]
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert any("missing evidence_id" in p for p in exc.value.problems)


def test_duplicate_ids_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["outflows"].append(copy.deepcopy(data["outflows"][0]))
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert any("duplicate outflow id" in p for p in exc.value.problems)


def test_invalid_date_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["outflows"][0]["date"] = "2026-13-99"
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_deferrable_above_amount_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["outflows"][2]["maximum_deferrable_minor"] = 999_999_999
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_received_above_original_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["receivables"][0]["received_minor"] = 800_000 + 1
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_unknown_event_type_rejected(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    data["events"][0]["type"] = "magic_windfall"
    with pytest.raises(FixtureValidationError):
        validate_fixture(data)


def test_all_problems_reported_together(raw_fixture):
    data = copy.deepcopy(raw_fixture)
    del data["accounts"]
    data["synthetic"] = "yes"
    with pytest.raises(FixtureValidationError) as exc:
        validate_fixture(data)
    assert len(exc.value.problems) >= 2
