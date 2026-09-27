import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from backup.fingerprint import FINGERPRINT_ALGORITHM, database_state
from inventory.store import InventoryStore

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def base(tmp_path):
    store = InventoryStore(tmp_path / "base.db", clock=lambda: NOW)
    store.add(title="Zero", source="curb", acquired_at="2026-09-01", acquisition_cost="0.00")
    store.add(
        title="Sold",
        source="thrift",
        acquired_at="2026-09-02",
        acquisition_cost="12.34",
        marketplace="ebay",
        marketplace_item_id="137744631273",
        marketplace_sku="Q0002",
    )
    store.transition_status("Q0002", "listed")
    store.adopt_ebay_listing(
        "Q0003",
        title="Unknown",
        source=None,
        acquired_at=None,
        acquisition_cost=None,
        marketplace_item_id="item-3",
        marketplace_sku="Q0003",
    )
    sale, _ = store.import_sale(
        inventory_id="Q0002",
        marketplace="ebay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="Q0002",
        quantity=1,
        gross_amount=Decimal("83.07"),
        currency="USD",
        sold_at=datetime(2026, 9, 15, 12, 34, 56, tzinfo=timezone.utc),
    )
    store.add_sale_cost(sale.sale_id, category="marketplace_fee", amount=Decimal("12.24"))
    store.set_reconciliation_confirmation(sale.sale_id, "fees", confirmed=True)
    store.save_research_snapshot(
        opportunity_identity="manual:camera",
        source="estate-sale",
        title="Camera",
        category="electronics",
        currency="USD",
        asking_price=Decimal("20.250"),
        expected_resale=None,
        expected_profit=None,
        payload={
            "schema_version": 1,
            "opportunity": {"title": "Camera"},
            "source_facts": {},
            "assumptions": {},
            "comparables": [],
            "derived": {},
        },
        save_token="a" * 32,
    )
    return store.path


def variant(base, tmp_path, *statements, name="variant.db"):
    """Exact copy of the base database with the given SQL applied."""
    target = tmp_path / name
    with sqlite3.connect(base) as source, sqlite3.connect(target) as copy:
        source.backup(copy)
    with sqlite3.connect(target) as connection:
        for statement in statements:
            connection.execute(statement)
    return target


def fingerprint(path):
    return database_state(path).fingerprint


def test_fingerprint_is_deterministic_and_ignores_physical_layout(base, tmp_path):
    vacuumed = tmp_path / "vacuumed.db"
    padded = variant(
        base,
        tmp_path,
        "CREATE TABLE scratch (value BLOB)",
        "INSERT INTO scratch VALUES (zeroblob(200000))",
        "DROP TABLE scratch",
        name="padded.db",
    )
    with sqlite3.connect(base) as connection:
        connection.execute(f"VACUUM INTO '{vacuumed.as_posix()}'")

    assert padded.read_bytes() != base.read_bytes()
    assert fingerprint(base) == fingerprint(base)
    assert fingerprint(padded) == fingerprint(base)
    assert fingerprint(vacuumed) == fingerprint(base)
    state = database_state(base)
    assert state.counters == {"q_number": 3, "s_number": 1, "c_number": 1}
    assert state.table_row_counts["inventory_items"] == 3
    assert FINGERPRINT_ALGORITHM == "flipper-logical-v1"


@pytest.mark.parametrize(
    "statement",
    [
        # NULL (unknown) versus known zero.
        "UPDATE inventory_items SET acquisition_cost_cents = 0 "
        "WHERE acquisition_cost_cents IS NULL",
        # Same decimal value stored at a different scale is a different stored fact.
        "UPDATE sales SET gross_amount_minor = 83070, gross_amount_scale = 3",
        "UPDATE sales SET gross_amount_minor = 8308",
        # Timestamps are compared exactly.
        "UPDATE sales SET sold_at = '2026-09-15T12:34:57Z'",
        # Q/S/C counters.
        "UPDATE inventory_id_sequence SET last_value = last_value + 1",
        "UPDATE sale_id_sequence SET last_value = last_value + 1",
        "UPDATE sale_cost_id_sequence SET last_value = last_value + 1",
        # Historical research snapshots, compared as stored JSON text.
        "UPDATE research_snapshots SET payload_json = replace(payload_json, 'Camera', 'Lens')",
        "UPDATE research_snapshots SET payload_json = replace(payload_json, ',', ', ')",
        # External marketplace identities.
        "UPDATE sales SET external_order_id = 'order-2'",
        "UPDATE inventory_items SET marketplace_item_id = '137744631274' "
        "WHERE marketplace_item_id = '137744631273'",
        # Reconciliation state.
        "DELETE FROM sale_reconciliation_confirmations",
        # Notes and migration history.
        "UPDATE inventory_items SET notes = 'x' WHERE internal_id = 1",
        "UPDATE schema_migrations SET applied_at = '2000-01-01 00:00:00' WHERE version = 1",
        # Storage class is part of the value: identical bytes as BLOB are not TEXT.
        "UPDATE inventory_items SET title = CAST(title AS BLOB) WHERE internal_id = 1",
        # Schema objects.
        "CREATE INDEX extra_index ON sales (currency)",
    ],
)
def test_every_authoritative_difference_changes_the_fingerprint(base, tmp_path, statement):
    assert fingerprint(variant(base, tmp_path, statement)) != fingerprint(base)


def test_row_insertion_order_does_not_affect_fingerprint(tmp_path):
    rows = [
        (3, "Q0003", "Three", None),
        (1, "Q0001", "One", 0),
        (2, "Q0002", "Two", 1234),
    ]
    paths = []
    for name, ordered in (("forward.db", rows), ("reverse.db", list(reversed(rows)))):
        path = tmp_path / name
        store = InventoryStore(path)
        store.initialize()
        with sqlite3.connect(path) as connection:
            for internal_id, inventory_id, title, cost in ordered:
                connection.execute(
                    "INSERT INTO inventory_items (internal_id, inventory_id, title, "
                    "acquisition_cost_cents, quantity, status, created_at) "
                    "VALUES (?, ?, ?, ?, 1, 'acquired', '2026-09-01 00:00:00')",
                    (internal_id, inventory_id, title, cost),
                )
            connection.execute("UPDATE schema_migrations SET applied_at = '2026-09-01 00:00:00'")
        paths.append(path)

    assert fingerprint(paths[0]) == fingerprint(paths[1])


def test_fingerprint_reads_without_modifying_or_creating(base, tmp_path):
    before = base.read_bytes()
    fingerprint(base)
    assert base.read_bytes() == before
    with pytest.raises(FileNotFoundError):
        fingerprint(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()


def test_non_flipper_database_is_rejected(tmp_path):
    path = tmp_path / "other.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(ValueError, match="not a Flipper database"):
        fingerprint(path)
