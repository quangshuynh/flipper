from decimal import Decimal

import pytest

from deals.manual import USER_SOURCE_PROVENANCE, create_manual_opportunity
from deals.models import AmountStatus, ProvenanceKind, SourceIdentity
from deals.research import EphemeralResearchStore, MAX_MANUAL_OPPORTUNITIES_PER_SESSION


def _fields(**changes):
    values = {
        "source": "facebook-marketplace",
        "title": "Tested camera kit",
        "category": "electronics",
        "base_price": "12.340",
        "currency": "usd",
        "inbound_shipping": "4.50",
        "condition": "used_tested",
        "source_url": "https://example.test/listing/1",
        "location_text": "Rochester, NY",
        "notes": "Includes charger",
    }
    values.update(changes)
    return values


def test_manual_opportunity_preserves_exact_values_and_user_source_provenance():
    stored = create_manual_opportunity(_fields())

    assert stored.opportunity.source is SourceIdentity.FACEBOOK_MARKETPLACE
    assert stored.opportunity.base_price.amount == Decimal("12.340")
    assert stored.inbound_shipping.money.amount == Decimal("4.50")
    assert stored.opportunity.base_price_provenance == USER_SOURCE_PROVENANCE
    assert stored.opportunity.category_provenance.kind is ProvenanceKind.USER_SOURCE_FACT
    assert stored.notes == "Includes charger"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("source", "invented", "source"),
        ("category", "invented", "category"),
        ("condition", "mint-ish", "condition"),
        ("base_price", "twelve", "asking price"),
        ("source_url", "http://example.test", "HTTPS"),
        ("source_url", "https://user:pass@example.test", "credentials"),
        ("location_text", "x" * 201, "location"),
        ("notes", "x" * 2001, "notes"),
    ],
)
def test_manual_opportunity_rejects_invalid_input(field, value, message):
    with pytest.raises(ValueError, match=message):
        create_manual_opportunity(_fields(**{field: value}))


def test_blank_shipping_is_unknown_and_url_creation_does_not_fetch(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("manual URL must never be requested")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    stored = create_manual_opportunity(_fields(inbound_shipping=""))
    assert stored.inbound_shipping.status is AmountStatus.UNKNOWN


def test_manual_workspace_is_isolated_bounded_and_evicts_oldest():
    store = EphemeralResearchStore()
    first = None
    for index in range(MAX_MANUAL_OPPORTUNITIES_PER_SESSION + 1):
        identity = store.add_opportunity(
            "a" * 32, create_manual_opportunity(_fields(title=f"Item {index}"))
        )
        first = first or identity

    assert len(store.list_opportunities("a" * 32)) == MAX_MANUAL_OPPORTUNITIES_PER_SESSION
    with pytest.raises(LookupError):
        store.get_opportunity("a" * 32, first)
    assert store.list_opportunities("b" * 32) == ()
