import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from deals.snapshots import SnapshotPayloadError
from inventory.store import InventoryStore, ResearchSnapshotNotFoundError


def payload(title="Camera"):
    return {
        "schema_version": 1,
        "opportunity": {"title": title},
        "source_facts": {},
        "assumptions": {},
        "comparables": [],
        "derived": {},
    }


def save(store, *, token="a" * 32, title="Camera"):
    return store.save_research_snapshot(
        opportunity_identity="manual:working-id",
        source="estate-sale",
        title=title,
        category="electronics",
        currency="USD",
        asking_price=Decimal("20.250"),
        expected_resale=Decimal("80"),
        expected_profit=Decimal("50"),
        payload=payload(title),
        save_token=token,
    )


def test_snapshot_save_is_exact_authoritative_and_retry_idempotent(tmp_path):
    store = InventoryStore(
        tmp_path / "inventory.db",
        clock=lambda: datetime(2026, 9, 17, 15, 30, tzinfo=timezone.utc),
    )
    first = save(store)
    retry = save(store)
    second = save(store, token="b" * 32, title="Camera later")

    assert retry.snapshot_id == first.snapshot_id
    assert second.snapshot_id != first.snapshot_id
    assert first.saved_at == "2026-09-17T15:30:00Z"
    assert first.asking_price == Decimal("20.250")
    assert [row.snapshot_id for row in store.list_research_snapshots()] == [
        second.snapshot_id,
        first.snapshot_id,
    ]


def test_snapshot_payload_is_a_copy_and_inventory_link_is_optional(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    original = payload()
    record = store.save_research_snapshot(
        opportunity_identity="ebay:item-1",
        source="ebay",
        title="Camera",
        category="electronics",
        currency="USD",
        asking_price=Decimal("10"),
        expected_resale=None,
        expected_profit=None,
        payload=original,
        save_token="c" * 32,
    )
    original["opportunity"]["title"] = "Changed working object"
    item = store.add(
        title="Acquired camera",
        source="estate",
        acquired_at="2026-09-17",
        acquisition_cost="9.00",
    )
    linked = store.link_research_snapshot(record.snapshot_id, item.inventory_id)

    assert (
        store.get_research_snapshot(record.snapshot_id).payload["opportunity"]["title"] == "Camera"
    )
    assert linked.inventory_id == item.inventory_id
    assert store.get(item.inventory_id).acquisition_cost == Decimal("9.00")


def test_malformed_identifier_payload_and_database_failure_are_safe(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    with pytest.raises(ResearchSnapshotNotFoundError):
        store.get_research_snapshot("../bad")
    with pytest.raises(ValueError, match="unsupported"):
        store.save_research_snapshot(
            opportunity_identity="manual:x",
            source="local",
            title="Bad",
            category="electronics",
            currency="USD",
            asking_price=Decimal("1"),
            expected_resale=None,
            expected_profit=None,
            payload=payload() | {"schema_version": 99},
            save_token="d" * 32,
        )
    store.initialize()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_snapshot BEFORE INSERT ON research_snapshots "
            "BEGIN SELECT RAISE(ABORT, 'rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        save(store, token="e" * 32)
    assert store.list_research_snapshots() == []


def test_corrupt_payload_is_rejected_on_read_without_executing_content(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    record = save(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE research_snapshots SET payload_json = ? WHERE snapshot_id = ?",
            ('{"schema_version":999}', record.snapshot_id),
        )
    with pytest.raises(SnapshotPayloadError):
        store.get_research_snapshot(record.snapshot_id)
    assert store.list_research_snapshots()[0].snapshot_id == record.snapshot_id
