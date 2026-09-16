from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient

from inventory.store import InventoryStore
from web.app import app


def _client(monkeypatch, tmp_path):
    database = tmp_path / "web.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    return TestClient(app), InventoryStore(database)


def _sold(store, number, *, gross="100.00", currency="USD"):
    item = store.add(
        title=f"Laptop {number}",
        source="test source",
        acquired_at="2026-08-01",
        acquisition_cost="25.00",
        marketplace="eBay",
        marketplace_sku=f"SKU-{number}",
        notes="Tested and clean",
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
        sold_at=datetime(2026, 9, number, 12, tzinfo=timezone.utc),
    )
    return item, sale


def test_web_application_starts_and_empty_states(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    dashboard = client.get("/")
    inventory = client.get("/inventory")
    sales = client.get("/sales")
    analytics = client.get("/analytics")

    assert dashboard.status_code == 200
    assert "Active inventory" in dashboard.text
    assert "No sales yet" in dashboard.text
    assert "No matching inventory" in inventory.text
    assert "No sales found" in sales.text
    assert "No sales data available" in analytics.text


def test_populated_dashboard_inventory_and_details(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    store.add(
        title="Active workstation",
        source="estate sale",
        acquired_at="2026-09-01",
        acquisition_cost="1,234.56".replace(",", ""),
        marketplace="eBay",
        marketplace_sku="ACTIVE-1",
    )
    item, sale = _sold(store, 1)
    store.add_sale_cost(sale.sale_id, category="marketplace_fee", amount=Decimal("10.00"))

    dashboard = client.get("/")
    listing = client.get("/inventory?search=ACTIVE-1&sort=cost_desc")
    detail = client.get(f"/inventory/{item.inventory_id}")

    assert "USD 1,234.56 tied up" in dashboard.text
    assert "USD 100.00" in dashboard.text
    assert "USD 65.00" in dashboard.text
    assert "1 incomplete" in dashboard.text
    assert "Active workstation" in listing.text
    assert "Laptop 1" not in listing.text
    assert sale.sale_id in detail.text
    assert "Tested and clean" in detail.text


def test_sales_states_components_missing_categories_and_analytics(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    _, incomplete = _sold(store, 1)
    store.add_sale_cost(
        incomplete.sale_id,
        category="shipping_cost",
        amount=Decimal("8.50"),
        note="label",
    )
    _, complete = _sold(store, 2, gross="50.00")
    for category in ("fees", "shipping", "refunds", "adjustments"):
        store.set_reconciliation_confirmation(complete.sale_id, category, confirmed=True)

    listing = client.get("/sales")
    detail = client.get(f"/sales/{incomplete.sale_id}")
    analytics = client.get("/analytics")

    assert "incomplete" in listing.text
    assert "fully reconciled" in listing.text
    assert "USD 66.50" in detail.text
    assert "fees, shipping, refunds, adjustments" in detail.text
    assert "manual" in detail.text
    assert "label" in detail.text
    assert "2026-09" in analytics.text
    assert "USD 150.00" in analytics.text


def test_mixed_currency_metrics_are_separate_and_profit_unavailable(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    _sold(store, 1, gross="80.00", currency="EUR")
    _sold(store, 2, gross="90.00", currency="USD")

    dashboard = client.get("/")
    sales = client.get("/sales")

    assert "EUR 80.00" in dashboard.text
    assert "USD 90.00" in dashboard.text
    assert "Unavailable" in dashboard.text
    assert "No currency conversion" in sales.text


def test_settings_exposes_status_but_never_secrets(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    secrets = {
        "EBAY_SELLER_CLIENT_ID": "client-id-secret-value",
        "EBAY_SELLER_CLIENT_SECRET": "client-secret-value",
        "EBAY_SELLER_RUNAME": "runame-secret-value",
        "EBAY_ACCOUNT_DELETION_TOKEN": "deletion-secret-value",
        "EBAY_BUYER_NAME": "private-buyer-sentinel",
        "EBAY_BUYER_ADDRESS": "private-address-sentinel",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("web.app.keyring.get_password", lambda *_: "refresh-token-secret-value")

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Connected" in response.text
    assert "Available" in response.text
    for value in (*secrets.values(), "refresh-token-secret-value"):
        assert value not in response.text


def test_missing_records_are_safe_html_errors(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    inventory = client.get("/inventory/Q9999")
    sale = client.get("/sales/S999999")

    assert inventory.status_code == 404
    assert sale.status_code == 404
    assert "Record not found" in inventory.text
    assert "Traceback" not in inventory.text
    assert "Traceback" not in sale.text


def test_compliance_route_coexists_with_dashboard(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    token = "test_verification_token_32_chars_minimum"
    endpoint = "https://flipper.example.com/api/ebay/account-deletion"
    monkeypatch.setenv("EBAY_ACCOUNT_DELETION_TOKEN", token)
    monkeypatch.setenv("EBAY_ACCOUNT_DELETION_ENDPOINT", endpoint)

    response = client.get(
        "/api/ebay/account-deletion", params={"challenge_code": "dashboard-regression"}
    )

    assert response.status_code == 200
    assert len(response.json()["challengeResponse"]) == 64
