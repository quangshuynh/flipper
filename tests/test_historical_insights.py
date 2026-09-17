from datetime import date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from inventory.store import RECONCILIATION_CATEGORIES, InventoryStore
from reports.service import historical_insights
import web.app as web_app
from web.app import app


def _sell(
    store: InventoryStore,
    number: int,
    *,
    source: str = "estate sale",
    acquired_at: str = "2026-08-01",
    sold_at: str = "2026-09-01T12:00:00Z",
    cost: str = "20",
    gross: str = "100",
    currency: str = "USD",
):
    item = store.add(
        title=f"Item {number}",
        source=source,
        acquired_at=acquired_at,
        acquisition_cost=cost,
        marketplace="eBay",
        marketplace_sku=f"SKU-{number}",
    )
    store.transition_status(item.inventory_id, "listed")
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id=f"order-{number}",
        external_line_item_id=f"line-{number}",
        marketplace_sku=f"SKU-{number}",
        quantity=1,
        gross_amount=Decimal(gross),
        currency=currency,
        sold_at=datetime.fromisoformat(sold_at.replace("Z", "+00:00")),
    )
    return item, sale


def _complete(store: InventoryStore, sale_id: str) -> None:
    for category in RECONCILIATION_CATEGORIES:
        store.set_reconciliation_confirmation(sale_id, category, confirmed=True)


def _link_category(store: InventoryStore, inventory_id: str, category: str, token: str) -> None:
    snapshot = store.save_research_snapshot(
        opportunity_identity=f"manual:{token}",
        source="manual",
        title="Research item",
        category=category,
        currency="USD",
        asking_price=Decimal("10"),
        expected_resale=None,
        expected_profit=None,
        payload={
            "schema_version": 1,
            "opportunity": {"title": "Research item"},
            "source_facts": {},
            "assumptions": {},
            "comparables": [],
            "derived": {},
        },
        save_token=token * 32,
    )
    store.link_research_snapshot(snapshot.snapshot_id, inventory_id)


def test_empty_historical_insights_are_explicit(tmp_path):
    result = historical_insights(InventoryStore(tmp_path / "inventory.db"))

    assert result.overall.completed_count == 0
    assert result.overall.revenue_by_currency == {}
    assert result.overall.total_realized_profit_usd is None
    assert result.overall.median_realized_roi is None
    assert result.by_source == ()
    assert result.by_category == ()


def test_exact_medians_completeness_and_actual_travel_once(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first_item, first = _sell(store, 1, gross="100.00")
    _, second = _sell(
        store,
        2,
        acquired_at="2026-08-03",
        sold_at="2026-09-03T23:59:59Z",
        gross="80.00",
    )
    _sell(store, 3, gross="50.00")
    unsold = store.add(
        title="Unsold", source="estate sale", acquired_at="2026-08-01", acquisition_cost="999"
    )
    store.add_sale_cost(first.sale_id, category="marketplace_fee", amount=Decimal("10"))
    store.add_sale_cost(
        first.sale_id, category="other_adjustment", amount=Decimal("2"), effect="increase"
    )
    store.set_sourcing_travel(first_item.inventory_id, fuel_cost="5", additional_expense="0")
    _complete(store, first.sale_id)
    _complete(store, second.sale_id)

    result = historical_insights(store).overall

    assert unsold.status == "acquired"
    assert result.completed_count == 3
    assert result.revenue_by_currency == {"USD": Decimal("230")}
    assert result.realized_profit_complete_count == 2
    assert result.incomplete_accounting_count == 1
    assert result.total_realized_profit_usd == Decimal("127")
    assert result.median_realized_profit_usd == Decimal("63.5")
    assert result.median_realized_roi == (Decimal(67) / Decimal(33) + Decimal(3)) / 2
    assert result.realized_roi_count == 2
    assert result.median_holding_days == Decimal("31")
    assert result.holding_days_count == 3


def test_zero_values_and_invalid_roi_basis_are_not_missing(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    _, sale = _sell(store, 1, cost="0", gross="0")
    _complete(store, sale.sale_id)

    result = historical_insights(store).overall

    assert result.total_realized_profit_usd == Decimal(0)
    assert result.median_realized_profit_usd == Decimal(0)
    assert result.realized_profit_complete_count == 1
    assert result.median_realized_roi is None
    assert result.realized_roi_count == 0


def test_fully_reconciled_non_usd_is_currency_unavailable_not_incomplete(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    _, sale = _sell(store, 1, gross="40", currency="EUR")
    _complete(store, sale.sale_id)

    result = historical_insights(store).overall

    assert result.revenue_by_currency == {"EUR": Decimal("40")}
    assert result.realized_profit_complete_count == 0
    assert result.incomplete_accounting_count == 0
    assert result.unavailable_profit_count == 1


def test_sale_date_filter_is_inclusive_and_grouping_uses_facts_only(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first_item, first = _sell(store, 1, source="auction", sold_at="2026-09-01T00:00:00Z")
    _, second = _sell(store, 2, source="gift", sold_at="2026-09-30T23:59:59Z")
    _, outside = _sell(store, 3, source="auction", sold_at="2026-10-01T00:00:00Z")
    for sale in (first, second, outside):
        _complete(store, sale.sale_id)
    _link_category(store, first_item.inventory_id, "electronics", "a")

    result = historical_insights(store, start=date(2026, 9, 1), end=date(2026, 9, 30))

    assert result.overall.completed_count == 2
    assert [(group.name, group.completed_count) for group in result.by_source] == [
        ("auction", 1),
        ("gift", 1),
    ]
    assert [(group.name, group.completed_count) for group in result.by_category] == [
        ("Electronics", 1),
        ("Unknown", 1),
    ]


def test_insights_web_empty_partial_complete_and_safe_ranges(monkeypatch, tmp_path):
    database = tmp_path / "web.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    monkeypatch.setattr(web_app, "_today", lambda: date(2026, 9, 17))
    client = TestClient(app)
    store = InventoryStore(database)

    empty = client.get("/insights")
    assert empty.status_code == 200
    assert "Historical realized data" in empty.text
    assert "No historical outcomes in this period" in empty.text
    assert 'href="/inventory"' in empty.text
    assert "No completed sales in this period" in empty.text

    _, complete = _sell(store, 1, sold_at="2026-09-17T00:00:00Z")
    _sell(store, 2, sold_at="2026-06-01T00:00:00Z")
    _complete(store, complete.sale_id)
    response = client.get("/insights?range=30")
    invalid = client.get("/insights?range=not-a-range")

    assert response.status_code == 200
    assert "2026-08-19 through 2026-09-17" in response.text
    assert "1 fully reconciled USD item" in response.text
    assert "By source" in response.text and "By category" in response.text
    assert "Small sample" in response.text
    assert 'option value="all" selected' in invalid.text
