from datetime import date, datetime, timezone
from decimal import Decimal

from deals.outcomes import build_decision_outcome
from inventory.store import InventoryStore


def _payload(*, landed="60.00", resale="180.00", profit="74.00", roi="0.58"):
    def money(amount):
        return None if amount is None else {"amount": amount, "currency": "USD"}

    return {
        "schema_version": 1,
        "opportunity": {"title": "Camera"},
        "source_facts": {},
        "assumptions": {
            "expected_resale": {"money": money(resale)},
        },
        "comparables": [],
        "time_to_sale": {"minimum_days": 15, "maximum_days": 45},
        "derived": {
            "landed_cost": money(landed),
            "expected_net_proceeds": money(None if resale is None else str(Decimal(resale) - 10)),
            "expected_net_profit": money(profit),
            "roi": roi,
        },
    }


def _snapshot(store, *, payload=None):
    return store.save_research_snapshot(
        opportunity_identity="manual:camera",
        source="estate-sale",
        title="Camera",
        category="electronics",
        currency="USD",
        asking_price=Decimal("50.00"),
        expected_resale=Decimal("180.00"),
        expected_profit=Decimal("74.00"),
        payload=payload or _payload(),
        save_token="a" * 32,
    )


def test_unlinked_and_linked_unsold_outcome_states(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    snapshot = _snapshot(store)
    unlinked = build_decision_outcome(store, snapshot, today=date(2026, 9, 17))
    assert unlinked.state == "not_linked"
    assert unlinked.resale.actual is None

    item = store.add(
        title="Camera", source="estate", acquired_at="2026-09-01", acquisition_cost="50.00"
    )
    snapshot = store.link_research_snapshot(snapshot.snapshot_id, item.inventory_id)
    linked = build_decision_outcome(store, snapshot, today=date(2026, 9, 17))
    assert linked.state == "in_progress"
    assert linked.acquisition_cost.actual == Decimal("50")
    assert linked.acquisition_cost.difference == Decimal("-10.00")
    assert linked.actual_holding_days is None
    assert linked.profit.actual is linked.roi.actual is None


def test_sold_outcome_reuses_accounting_exactly_and_holding_range(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    snapshot = _snapshot(store)
    item = store.add(
        title="Camera",
        source="estate",
        acquired_at="2026-08-01",
        acquisition_cost="50.00",
        marketplace="ebay",
        marketplace_sku="Q0001",
    )
    store.transition_status(item.inventory_id, "listed")
    snapshot = store.link_research_snapshot(snapshot.snapshot_id, item.inventory_id)
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="ebay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku=item.inventory_id,
        quantity=1,
        gross_amount=Decimal("165.00"),
        currency="USD",
        sold_at=datetime(2026, 9, 2, 23, tzinfo=timezone.utc),
    )
    store.add_sale_cost(sale.sale_id, category="marketplace_fee", amount=Decimal("20.00"))
    store.add_sale_cost(sale.sale_id, category="shipping_cost", amount=Decimal("14.00"))
    credit = store.add_sale_cost(
        sale.sale_id, category="other_adjustment", amount=Decimal("2.00"), effect="increase"
    )
    outcome = build_decision_outcome(store, snapshot, today=date(2026, 9, 17))

    assert credit.effect == "increase"
    assert outcome.state == "realized"
    assert outcome.resale.actual == Decimal("165")
    assert outcome.resale.difference == Decimal("-15.00")
    assert outcome.profit.actual == Decimal("83.00")
    assert outcome.profit.difference == Decimal("9.00")
    assert outcome.roi.actual == Decimal("1.66")
    assert outcome.roi.difference == Decimal("1.08")
    assert outcome.actual_holding_days == 32
    assert outcome.holding_range_position == "within range"


def test_missing_and_zero_expectations_remain_distinct_and_snapshot_is_historical(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    captured = _payload(landed="0", resale=None, profit="0", roi=None)
    snapshot = _snapshot(store, payload=captured)
    captured["derived"]["landed_cost"]["amount"] = "999"
    item = store.add(
        title="Camera", source="estate", acquired_at="2026-09-01", acquisition_cost="0"
    )
    snapshot = store.link_research_snapshot(snapshot.snapshot_id, item.inventory_id)
    outcome = build_decision_outcome(store, snapshot, today=date(2026, 9, 17))

    assert outcome.acquisition_cost.expected == Decimal("0")
    assert outcome.acquisition_cost.actual == Decimal("0")
    assert outcome.acquisition_cost.difference == Decimal("0")
    assert outcome.resale.expected is outcome.resale.actual is None
    assert outcome.profit.expected == Decimal("0")
    assert (
        store.get_research_snapshot(snapshot.snapshot_id).payload["derived"]["landed_cost"][
            "amount"
        ]
        == "0"
    )
    assert "trip" not in outcome.__dataclass_fields__
