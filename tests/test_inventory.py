import sqlite3
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from inventory.store import (
    CURRENT_SCHEMA_VERSION,
    InventoryNotFoundError,
    InventoryStore,
    InventoryValidationError,
)


def values(**overrides):
    result = {
        "title": "Olympus recorder",
        "source": "estate sale",
        "acquired_at": "2026-09-01",
        "acquisition_cost": "12.34",
    }
    result.update(overrides)
    return result


def test_empty_database_initializes_versioned_schema(tmp_path):
    database = tmp_path / "nested" / "inventory.db"
    store = InventoryStore(database)
    store.initialize()

    assert store.list() == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version FROM schema_migrations").fetchall() == [
            (CURRENT_SCHEMA_VERSION,)
        ]


def test_ids_increment_and_deleted_ids_are_not_reused(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    assert store.add(**values()).inventory_id == "Q0001"
    assert store.add(**values(title="Second")).inventory_id == "Q0002"
    with sqlite3.connect(store.path) as connection:
        connection.execute("DELETE FROM inventory_items WHERE inventory_id = 'Q0002'")
    assert store.add(**values(title="Third")).inventory_id == "Q0003"


def test_failed_insert_rolls_back_sequence(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_bad BEFORE INSERT ON inventory_items "
            "WHEN NEW.title = 'reject' BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.add(**values(title="reject"))
    assert store.add(**values()).inventory_id == "Q0001"


def test_money_and_optional_marketplace_round_trip_exactly(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    plain = store.add(**values(acquisition_cost=Decimal("10.01")))
    linked = store.add(
        **values(
            title="Listed item",
            marketplace="eBay",
            marketplace_item_id="137744631273",
            marketplace_sku="Q0001",
            status="listed",
        )
    )
    assert plain.acquisition_cost == Decimal("10.01")
    assert plain.marketplace is None
    assert linked.marketplace_item_id == "137744631273"
    assert linked.marketplace_sku == "Q0001"


@pytest.mark.parametrize("cost", ["-1", "1.001", "nan", "not-money"])
def test_invalid_money_is_rejected(tmp_path, cost):
    with pytest.raises(InventoryValidationError):
        InventoryStore(tmp_path / "inventory.db").add(**values(acquisition_cost=cost))


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
def test_invalid_quantity_is_rejected(tmp_path, quantity):
    with pytest.raises(InventoryValidationError):
        InventoryStore(tmp_path / "inventory.db").add(**values(quantity=quantity))


@pytest.mark.parametrize(
    "override", [{"title": " "}, {"source": ""}, {"acquired_at": "09/01/2026"}]
)
def test_required_fields_are_enforced(tmp_path, override):
    with pytest.raises(InventoryValidationError):
        InventoryStore(tmp_path / "inventory.db").add(**values(**override))


def test_status_validation_lookup_and_ordering(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = store.add(**values(status="archived"))
    second = store.add(**values(title="Second", status="sold"))
    assert store.get("q0001") == first
    assert [item.inventory_id for item in store.list()] == [
        first.inventory_id,
        second.inventory_id,
    ]
    with pytest.raises(InventoryValidationError):
        store.add(**values(status="missing"))
    with pytest.raises(InventoryNotFoundError):
        store.get("Q9999")


def test_concurrent_allocation_produces_unique_sequential_ids(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.initialize()

    def create(number):
        return store.add(**values(title=f"Item {number}")).inventory_id

    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(create, range(20)))
    assert sorted(identifiers) == [f"Q{number:04d}" for number in range(1, 21)]


def test_temporary_databases_are_isolated(tmp_path):
    first = InventoryStore(tmp_path / "one.db")
    second = InventoryStore(tmp_path / "two.db")
    first.add(**values())
    assert second.list() == []


def test_single_field_update_preserves_unspecified_fields_and_identity(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    original = store.add(**values(notes="old note"))

    updated = store.update("Q0001", status="listed")

    assert updated.status == "listed"
    assert updated.title == original.title
    assert updated.notes == "old note"
    assert updated.inventory_id == original.inventory_id
    assert updated.internal_id == original.internal_id


def test_multiple_fields_and_exact_money_can_be_updated(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values())
    updated = store.update(
        "Q0001",
        title="Updated recorder",
        source="auction",
        acquired_at="2026-09-02",
        acquisition_cost="19.99",
        quantity=2,
        status="listed",
        notes="tested",
    )
    assert updated.title == "Updated recorder"
    assert updated.source == "auction"
    assert updated.acquired_at == "2026-09-02"
    assert updated.acquisition_cost == Decimal("19.99")
    assert updated.quantity == 2
    assert updated.status == "listed"
    assert updated.notes == "tested"


@pytest.mark.parametrize(
    "changes",
    [
        {"acquisition_cost": "-1"},
        {"acquisition_cost": "1.001"},
        {"quantity": 0},
        {"status": "invalid"},
        {"acquired_at": "September 1"},
    ],
)
def test_invalid_update_rolls_back_without_partial_mutation(tmp_path, changes):
    store = InventoryStore(tmp_path / "inventory.db")
    original = store.add(**values())
    changes["title"] = "must not persist"
    with pytest.raises(InventoryValidationError):
        store.update("Q0001", **changes)
    assert store.get("Q0001") == original


def test_unknown_item_cannot_be_updated(tmp_path):
    with pytest.raises(InventoryNotFoundError, match="Q9999"):
        InventoryStore(tmp_path / "inventory.db").update("Q9999", status="listed")


def test_marketplace_fields_can_be_added_changed_and_cleared(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values())
    linked = store.update(
        "Q0001", marketplace="eBay", marketplace_item_id="123", marketplace_sku="Q0001"
    )
    assert (linked.marketplace, linked.marketplace_item_id, linked.marketplace_sku) == (
        "eBay",
        "123",
        "Q0001",
    )
    changed = store.update("Q0001", marketplace_item_id="456", marketplace_sku="stock-1")
    assert changed.marketplace_item_id == "456"
    assert changed.marketplace_sku == "stock-1"
    cleared = store.update("Q0001", marketplace=None, marketplace_item_id="", marketplace_sku=None)
    assert (cleared.marketplace, cleared.marketplace_item_id, cleared.marketplace_sku) == (
        None,
        None,
        None,
    )


def test_update_database_failure_rolls_back_all_fields(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    original = store.add(**values())
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_update BEFORE UPDATE ON inventory_items "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.update("Q0001", title="changed", notes="changed")
    assert store.get("Q0001") == original


def test_updates_do_not_affect_allocation_sequence(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values())
    store.update("Q0001", title="changed")
    assert store.add(**values(title="Second")).inventory_id == "Q0002"
