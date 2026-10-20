"""Shared fixtures for the Phase 1 acceptance tests."""

from datetime import date

import pytest

from cashflow_copilot.fixture import load_fixture
from cashflow_copilot.ledger import Ledger
from cashflow_copilot.store import SQLiteStore


@pytest.fixture
def raw_fixture():
    return load_fixture()


@pytest.fixture
def ledger():
    return Ledger.seed()


@pytest.fixture
def delayed_ledger():
    led = Ledger.seed()
    result = led.apply_event("evt-delay-001")
    assert result["changed"] is True
    return led


@pytest.fixture
def delayed_ledger_with_consent(delayed_ledger):
    """A delayed ledger with an EXPLICIT runtime consent event.

    Consent for the supplier date change is never pre-granted by the seed;
    it is recorded here as an explicit simulated counterparty-acceptance
    event, which is the only thing that can make Plan B feasible.
    """
    consent_id = delayed_ledger.record_counterparty_acceptance(
        "supplier-001", date(2026, 11, 6), "act-test-supplier", actor="owner")
    assert consent_id in delayed_ledger.runtime_evidence_ids
    delayed_ledger.consent_id = consent_id
    return delayed_ledger


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "state.sqlite")


@pytest.fixture
def seeded_store(db_path):
    store = SQLiteStore(db_path)
    store.save_ledger(Ledger.seed())
    yield store
    store.close()


def usd(cf_result):
    return cf_result.currencies["USD"]


def eur(cf_result):
    return cf_result.currencies["EUR"]


OCT_16 = date(2026, 10, 16)
OCT_20 = date(2026, 10, 20)
OCT_22 = date(2026, 10, 22)
OCT_23 = date(2026, 10, 23)
OCT_25 = date(2026, 10, 25)
OCT_28 = date(2026, 10, 28)
NOV_5 = date(2026, 11, 5)
NOV_6 = date(2026, 11, 6)
