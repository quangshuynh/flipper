import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
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
            (version,) for version in range(1, CURRENT_SCHEMA_VERSION + 1)
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
    first = store.add(**values())
    second = store.add(**values(title="Second"))
    assert store.get("q0001") == first
    assert [item.inventory_id for item in store.list()] == [
        first.inventory_id,
        second.inventory_id,
    ]
    with pytest.raises(InventoryValidationError, match="must start as acquired"):
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

    updated = store.update("Q0001", notes="new note")

    assert updated.status == "acquired"
    assert updated.title == original.title
    assert updated.notes == "new note"
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
        notes="tested",
    )
    assert updated.title == "Updated recorder"
    assert updated.source == "auction"
    assert updated.acquired_at == "2026-09-02"
    assert updated.acquisition_cost == Decimal("19.99")
    assert updated.quantity == 2
    assert updated.status == "acquired"
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
        InventoryStore(tmp_path / "inventory.db").update("Q9999", notes="missing")


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


def test_v1_database_migrates_without_changing_existing_record(tmp_path):
    database = tmp_path / "inventory.db"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """CREATE TABLE schema_migrations (
                   version INTEGER PRIMARY KEY,
                   applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               );
               INSERT INTO schema_migrations (version) VALUES (1);
               CREATE TABLE inventory_id_sequence (
                   singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                   last_value INTEGER NOT NULL CHECK (last_value >= 0)
               );
               INSERT INTO inventory_id_sequence VALUES (1, 1);
               CREATE TABLE inventory_items (
                   internal_id INTEGER PRIMARY KEY,
                   inventory_id TEXT NOT NULL UNIQUE,
                   title TEXT NOT NULL,
                   source TEXT NOT NULL,
                   acquired_at TEXT NOT NULL,
                   acquisition_cost_cents INTEGER NOT NULL,
                   quantity INTEGER NOT NULL,
                   notes TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL,
                   marketplace TEXT,
                   marketplace_item_id TEXT,
                   marketplace_sku TEXT,
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
               );
               INSERT INTO inventory_items (
                   internal_id, inventory_id, title, source, acquired_at,
                   acquisition_cost_cents, quantity, notes, status, marketplace,
                   marketplace_item_id, marketplace_sku
               ) VALUES (7, 'Q0001', 'Existing', 'sale', '2026-08-01', 1234, 1,
                         'kept', 'listed', 'eBay', '123', 'Q0001');
            """
        )

    record = InventoryStore(database).get("Q0001")

    assert (record.internal_id, record.inventory_id, record.title, record.status) == (
        7,
        "Q0001",
        "Existing",
        "listed",
    )
    assert record.listed_at is None
    assert record.sold_at is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [
            (1,),
            (2,),
            (3,),
            (4,),
            (5,),
            (6,),
            (7,),
            (8,),
            (9,),
            (10,),
            (11,),
            (12,),
        ]


def test_marketplace_sku_is_unique_with_case_insensitive_marketplace(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values(marketplace="eBay", marketplace_sku="Q0001"))

    with pytest.raises(sqlite3.IntegrityError):
        store.add(**values(title="Duplicate", marketplace="EBAY", marketplace_sku="Q0001"))

    assert (
        store.add(
            **values(title="Case distinct", marketplace="ebay", marketplace_sku="q0001")
        ).inventory_id
        == "Q0002"
    )


def test_v2_migration_rejects_duplicates_without_modifying_data(tmp_path):
    database = tmp_path / "inventory.db"
    store = InventoryStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER sale_cost_currency_matches_sale")
        connection.execute("DROP TRIGGER sale_cost_external_identity_insert")
        connection.execute("DROP TRIGGER sale_cost_external_identity_update")
        connection.execute("DROP TRIGGER sale_cost_relationship_insert")
        connection.execute("DROP INDEX sale_costs_external_identity")
        connection.execute("DROP INDEX sale_costs_one_external_reversal")
        connection.execute("DROP TABLE sale_reconciliation_confirmations")
        connection.execute("DROP TABLE sale_costs")
        connection.execute("DROP TABLE sale_cost_id_sequence")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.execute("DELETE FROM schema_migrations WHERE version = 7")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute("DROP TABLE sales")
        connection.execute("DROP TABLE sale_id_sequence")
        connection.execute("DELETE FROM schema_migrations WHERE version = 3")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute("DELETE FROM schema_migrations WHERE version = 9")
        connection.execute("DROP TABLE inventory_attachments")
        connection.execute("DROP TRIGGER valuation_snapshots_immutable_update")
        connection.execute("DROP TRIGGER valuation_snapshots_immutable_delete")
        connection.execute("DROP TABLE valuation_snapshots")
        connection.execute("DROP INDEX inventory_marketplace_sku")
        connection.execute(
            "CREATE INDEX inventory_marketplace_sku "
            "ON inventory_items (marketplace, marketplace_sku)"
        )
        connection.execute(
            """INSERT INTO inventory_items (
                inventory_id, title, source, acquired_at, acquisition_cost_cents,
                quantity, notes, status, marketplace, marketplace_sku
            ) VALUES ('Q0001', 'One', 'sale', '2026-09-01', 100, 1, '', 'acquired',
                      'eBay', 'same'),
                     ('Q0002', 'Two', 'sale', '2026-09-01', 100, 1, '', 'acquired',
                      'EBAY', 'same')"""
        )

    with pytest.raises(RuntimeError, match="duplicate marketplace \\+ SKU"):
        store.initialize()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_items").fetchone() == (2,)
        assert connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall() == [(1,), (2,), (10,), (11,), (12,)]


def test_v10_migration_rejects_duplicate_ebay_item_ids(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP INDEX inventory_ebay_item_id")
        connection.execute("DELETE FROM schema_migrations WHERE version = 10")
        connection.execute(
            """INSERT INTO inventory_items (
                inventory_id, title, source, acquired_at, acquisition_cost_cents,
                quantity, notes, status, marketplace, marketplace_item_id
            ) VALUES ('Q0001', 'One', 'gift', '2026-01-01', 0, 1, '', 'acquired',
                      'eBay', 'same'),
                     ('Q0002', 'Two', 'gift', '2026-01-01', 0, 1, '', 'acquired',
                      'EBAY', 'same')"""
        )

    with pytest.raises(RuntimeError, match="unique marketplace item IDs"):
        store.initialize()
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM inventory_items").fetchone() == (2,)
        assert (
            connection.execute(
                "SELECT version FROM schema_migrations WHERE version = 10"
            ).fetchone()
            is None
        )


def test_lifecycle_transitions_set_utc_timestamps_and_preserve_identity(tmp_path):
    moments = iter(
        [
            datetime(2026, 9, 16, 14, 30, tzinfo=timezone.utc),
            datetime(2026, 9, 17, 15, 45, tzinfo=timezone.utc),
        ]
    )
    store = InventoryStore(tmp_path / "inventory.db", clock=lambda: next(moments))
    original = store.add(**values())

    listed = store.transition_status("Q0001", "listed")
    sold = store.transition_status("Q0001", "sold")

    assert listed.listed_at == "2026-09-16T14:30:00Z"
    assert listed.sold_at is None
    assert sold.listed_at == listed.listed_at
    assert sold.sold_at == "2026-09-17T15:45:00Z"
    assert sold.inventory_id == original.inventory_id
    assert sold.internal_id == original.internal_id


@pytest.mark.parametrize(
    ("start", "target"),
    [("acquired", "sold"), ("acquired", "acquired"), ("archived", "sold")],
)
def test_invalid_lifecycle_transitions_are_rejected(tmp_path, start, target):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values())
    if start == "archived":
        store.transition_status("Q0001", "archived")
    with pytest.raises(InventoryValidationError, match="cannot transition"):
        store.transition_status("Q0001", target)


def test_sold_correction_returns_to_listed_and_clears_only_sold_timestamp(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values())
    listed = store.transition_status("Q0001", "listed")
    store.transition_status("Q0001", "sold")

    corrected = store.transition_status("Q0001", "listed")

    assert corrected.listed_at == listed.listed_at
    assert corrected.sold_at is None


def test_restore_to_acquired_clears_lifecycle_timestamps(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.add(**values())
    store.transition_status("Q0001", "listed")
    restored = store.transition_status("Q0001", "acquired")
    assert (restored.status, restored.listed_at, restored.sold_at) == ("acquired", None, None)


def test_acquired_and_listed_items_can_be_archived_and_restored(tmp_path):
    acquired_store = InventoryStore(tmp_path / "acquired.db")
    acquired_store.add(**values())
    assert acquired_store.transition_status("Q0001", "archived").status == "archived"

    listed_store = InventoryStore(tmp_path / "listed.db")
    listed_store.add(**values())
    listed = listed_store.transition_status("Q0001", "listed")
    archived = listed_store.transition_status("Q0001", "archived")
    assert archived.listed_at == listed.listed_at
    restored = listed_store.transition_status("Q0001", "acquired")
    assert (restored.listed_at, restored.sold_at) == (None, None)


def test_generic_update_cannot_bypass_lifecycle(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    original = store.add(**values())
    with pytest.raises(InventoryValidationError, match="lifecycle transition"):
        store.update("Q0001", title="must roll back", status="listed")
    assert store.get("Q0001") == original


def test_failed_lifecycle_update_rolls_back_status_and_timestamps(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    original = store.add(**values())
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_transition BEFORE UPDATE ON inventory_items "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.transition_status("Q0001", "listed")
    assert store.get("Q0001") == original
