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
