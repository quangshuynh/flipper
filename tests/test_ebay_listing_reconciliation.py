from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ebay.listing_reconciliation import ListingReconciliationState, reconcile_active_listings
from ebay.listings import EbayActiveListing
from ebay.orders import Money
from inventory.store import InventoryStore, InventoryValidationError


def listing(sku="Q0001", item_id="123", title="Exact title does not matter"):
    return EbayActiveListing(item_id, sku, title, "Active", Money(Decimal("75"), "USD"), 1)


def add(store, **overrides):
    values = dict(title="Local", source="actual", acquired_at="2026-03-01", acquisition_cost="10")
    values.update(overrides)
    return store.add(**values)


def test_all_states_and_no_fuzzy_matching(tmp_path):
    store = InventoryStore(tmp_path / "i.db")
    add(store)
    rows = reconcile_active_listings(
        [listing(), listing("Q0002", "2", "Local"), listing(None, "3"), listing("Q0000", "4")],
        store.list(),
    )
    assert [row.state for row in rows] == [
        ListingReconciliationState.MATCHED,
        ListingReconciliationState.MISSING_LOCAL,
        ListingReconciliationState.MISSING_SKU,
        ListingReconciliationState.INVALID_SKU,
    ]


@pytest.mark.parametrize("status", ["sold", "archived"])
def test_historical_lifecycle_is_conflict(tmp_path, status):
    store = InventoryStore(tmp_path / f"{status}.db")
    add(store, marketplace="ebay", marketplace_item_id="123", marketplace_sku="Q0001")
    store.transition_status("Q0001", "listed")
    if status == "sold":
        store.transition_status("Q0001", "sold")
    else:
        store.transition_status("Q0001", "archived")
    assert (
        reconcile_active_listings([listing()], store.list())[0].state
        is ListingReconciliationState.CONFLICT
    )


def test_sync_is_explicit_atomic_and_idempotent(tmp_path):
    store = InventoryStore(
        tmp_path / "i.db", clock=lambda: datetime(2026, 9, 16, tzinfo=timezone.utc)
    )
    add(store)
    before = store.get("Q0001")
    assert before.status == "acquired"  # discovery itself did not mutate
    first, changed = store.sync_ebay_listing(
        "Q0001", marketplace_item_id="123", marketplace_sku="Q0001"
    )
    second, changed_again = store.sync_ebay_listing(
        "Q0001", marketplace_item_id="123", marketplace_sku="Q0001"
    )
    assert first.status == second.status == "listed"
    assert changed is True and changed_again is False


def test_adopt_high_q_advances_allocator_and_reimport_conflicts(tmp_path):
    store = InventoryStore(tmp_path / "i.db")
    adopted, created = store.adopt_ebay_listing(
        "Q0007",
        title="Listing",
        source="gift",
        acquired_at="2026-03-01",
        acquisition_cost="0.00",
        marketplace_item_id="123",
        marketplace_sku="Q0007",
    )
    assert adopted.status == "listed" and adopted.acquisition_cost == Decimal("0") and created
    _, repeated = store.adopt_ebay_listing(
        "Q0007",
        title="Listing",
        source="gift",
        acquired_at="2026-03-01",
        acquisition_cost="0.00",
        marketplace_item_id="123",
        marketplace_sku="Q0007",
    )
    assert repeated is False
    assert add(store).inventory_id == "Q0008"
    with pytest.raises(InventoryValidationError, match="conflicts"):
        store.adopt_ebay_listing(
            "Q0007",
            title="Listing",
            source="invented",
            acquired_at="2026-03-01",
            acquisition_cost="75",
            marketplace_item_id="123",
            marketplace_sku="Q0007",
        )


def test_adopt_listing_preserves_unknown_and_known_zero_acquisition(tmp_path):
    store = InventoryStore(tmp_path / "i.db")
    unknown, _ = store.adopt_ebay_listing(
        "Q0002",
        title="Unknown history",
        source=None,
        acquired_at=None,
        acquisition_cost=None,
        marketplace_item_id="item-2",
        marketplace_sku="Q0002",
    )
    free, _ = store.adopt_ebay_listing(
        "Q0003",
        title="Known free",
        source="gift",
        acquired_at="2026-09-01",
        acquisition_cost="0.00",
        marketplace_item_id="item-3",
        marketplace_sku="Q0003",
    )
    assert unknown.acquisition_cost is None
    assert unknown.source is None and unknown.acquired_at is None
    assert free.acquisition_cost == Decimal(0)
    assert free.acquisition_cost_cents == 0
    noted = store.update("Q0002", notes="history unavailable")
    assert noted.notes == "history unavailable"
    assert (noted.source, noted.acquired_at, noted.acquisition_cost) == (None, None, None)
