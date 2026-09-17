from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from deals.outcomes import build_decision_outcome
from inventory.store import (
    CURRENT_SCHEMA_VERSION,
    InventoryNotFoundError,
    InventoryStore,
    SourcingTravelValidationError,
)
from reports.service import sales_report


def _item(store):
    return store.add(
        title="Camera", source="estate", acquired_at="2026-09-01", acquisition_cost="50"
    )


def test_actual_travel_exact_partial_zero_update_and_clear(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _item(store)
    record = store.set_sourcing_travel(
        item.inventory_id,
        round_trip_miles="12.125",
        fuel_cost="0",
        additional_expense=None,
        travel_minutes="0",
        note="  receipt checked  ",
    )
    assert record.round_trip_miles == Decimal("12.125")
    assert record.fuel_cost == Decimal("0")
    assert record.additional_expense is None
    assert record.travel_minutes == 0
    assert record.total_expense is None
    assert record.recorded_expense == Decimal("0")
    assert record.note == "receipt checked"

    updated = store.set_sourcing_travel(
        item.inventory_id, fuel_cost="3.40", additional_expense="2.10"
    )
    assert updated.total_expense == Decimal("5.5")
    assert store.clear_sourcing_travel(item.inventory_id) == updated
    assert store.get_sourcing_travel(item.inventory_id) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("round_trip_miles", "-1"),
        ("fuel_cost", "wat"),
        ("additional_expense", "-0.01"),
        ("travel_minutes", "1.5"),
        ("travel_minutes", "-1"),
    ],
)
def test_actual_travel_rejects_invalid_values(tmp_path, field, value):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _item(store)
    with pytest.raises(SourcingTravelValidationError):
        store.set_sourcing_travel(item.inventory_id, **{field: value})
    assert store.get_sourcing_travel(item.inventory_id) is None


def test_actual_travel_requires_explicit_q_link_and_schema_migrates(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.initialize()
    with pytest.raises(InventoryNotFoundError):
        store.set_sourcing_travel("Q9999", fuel_cost="1")
    with store._connect() as connection:
        assert (
            connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            == CURRENT_SCHEMA_VERSION
        )
        assert connection.execute("SELECT count(*) FROM sourcing_travel").fetchone()[0] == 0


def test_actual_travel_is_counted_once_in_realized_profit_and_edit_recalculates(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = _item(store)
    store.transition_status(item.inventory_id, "listed")
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="ebay",
        external_order_id="o1",
        external_line_item_id="l1",
        marketplace_sku=item.inventory_id,
        quantity=1,
        gross_amount=Decimal("100"),
        currency="USD",
        sold_at=datetime(2026, 9, 2, tzinfo=timezone.utc),
    )
    store.add_sale_cost(sale.sale_id, category="shipping_cost", amount=Decimal("10"))
    store.set_sourcing_travel(item.inventory_id, fuel_cost="4", additional_expense="1")

    def profit():
        return (
            sales_report(
                [store.get(item.inventory_id)],
                [sale],
                store.list_sale_costs(sale.sale_id),
                store.list_reconciliation_confirmations(),
                store.list_sourcing_travel(),
            )
            .rows[0]
            .economics.recorded_profit
        )

    assert profit() == Decimal("35")
    store.set_sourcing_travel(item.inventory_id, fuel_cost="2", additional_expense="1")
    assert profit() == Decimal("37")


def test_decision_outcome_compares_expected_and_actual_travel_without_mutation(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    payload = {
        "schema_version": 1,
        "opportunity": {"title": "Camera"},
        "source_facts": {},
        "assumptions": {},
        "comparables": [],
        "derived": {},
        "trip": {
            "round_trip_miles": "20",
            "estimated_fuel_cost": {"amount": "5", "currency": "USD"},
            "additional_travel_cost": {"amount": "1", "currency": "USD"},
            "total_travel_cost": {"money": {"amount": "6", "currency": "USD"}},
            "round_trip_minutes": 30,
        },
    }
    snapshot = store.save_research_snapshot(
        opportunity_identity="manual:camera",
        source="estate",
        title="Camera",
        category="electronics",
        currency="USD",
        asking_price=Decimal("50"),
        expected_resale=None,
        expected_profit=None,
        payload=payload,
        save_token="a" * 32,
    )
    item = _item(store)
    snapshot = store.link_research_snapshot(snapshot.snapshot_id, item.inventory_id)
    store.set_sourcing_travel(
        item.inventory_id,
        round_trip_miles="22.5",
        fuel_cost="4",
        additional_expense="0",
        travel_minutes="35",
    )
    outcome = build_decision_outcome(store, snapshot, today=date(2026, 9, 17))
    assert outcome.travel_miles.difference == Decimal("2.5")
    assert outcome.travel_fuel.difference == Decimal("-1")
    assert outcome.travel_additional.actual == Decimal("0")
    assert outcome.travel_total.difference == Decimal("-2")
    assert outcome.travel_minutes.difference == Decimal("5")
    assert (
        store.get_research_snapshot(snapshot.snapshot_id).payload["trip"]["round_trip_miles"]
        == "20"
    )
