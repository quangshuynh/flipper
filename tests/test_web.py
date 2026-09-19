from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from keyring.errors import KeyringError

from inventory.store import InventoryStore
from inventory.attachments import AttachmentService
from ebay.listings import EbayActiveListing
from ebay.orders import EbayOrder, EbayOrderLineItem, Money, OrderPricingSummary
import web.app as web_app
from web.app import app


def _client(monkeypatch, tmp_path):
    database = tmp_path / "web.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    return TestClient(app), InventoryStore(database)


def _sold(
    store,
    number,
    *,
    gross="100.00",
    currency="USD",
    item_revenue=None,
    buyer_shipping=None,
    marketplace_tax=None,
):
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
        item_revenue=Decimal(item_revenue) if item_revenue is not None else None,
        buyer_shipping=Decimal(buyer_shipping) if buyer_shipping is not None else None,
        marketplace_tax=Decimal(marketplace_tax) if marketplace_tax is not None else None,
    )
    return item, sale


def _ebay_order(*lines, order_id="order-1"):
    return EbayOrder(
        order_id=order_id,
        creation_date=datetime(2026, 9, 19, 1, tzinfo=timezone.utc),
        last_modified_date=None,
        fulfillment_status="FULFILLED",
        payment_status="PAID",
        cancellation_status="NONE_REQUESTED",
        line_items=tuple(lines),
        pricing=OrderPricingSummary(
            shipping=Money(Decimal("8.07"), "USD"),
            tax=Money(Decimal("3.98"), "USD"),
            total=Money(Decimal("87.05"), "USD"),
        ),
    )


def _order_line(line_id="line-1", sku="Q0001", amount="75.00"):
    return EbayOrderLineItem(
        line_item_id=line_id,
        legacy_item_id="item-1",
        title="Olympus recorder",
        sku=sku,
        quantity=1,
        line_item_cost=Money(Decimal(amount), "USD") if amount is not None else None,
    )


def _mock_live_orders(monkeypatch, orders):
    monkeypatch.setattr(web_app.SellerOAuthConfig, "from_environment", lambda: object())
    monkeypatch.setattr(web_app, "SellerOAuthClient", lambda config: object())
    monkeypatch.setattr(
        web_app,
        "FulfillmentClient",
        lambda oauth: type("C", (), {"get_orders": lambda self, start, end: orders})(),
    )


def test_live_ebay_listings_page_is_isolated_and_renders_state(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    store.add(
        title="Local recorder",
        source="gift",
        acquired_at="2026-03-01",
        acquisition_cost="0",
    )
    listing = EbayActiveListing(
        "137744631273",
        "Q0001",
        "Recorder",
        "Active",
        Money(Decimal("75.00"), "USD"),
        1,
    )
    monkeypatch.setattr(web_app.SellerOAuthConfig, "from_environment", lambda: object())
    monkeypatch.setattr(web_app, "SellerOAuthClient", lambda config: object())
    monkeypatch.setattr(
        web_app,
        "ActiveListingsClient",
        lambda oauth: type("C", (), {"get_active_listings": lambda self: [listing]})(),
    )

    response = client.get("/ebay/listings")

    assert response.status_code == 200
    assert "Recorder" in response.text
    assert "MATCHED" in response.text
    assert "buyer" not in response.text.lower()


def test_ebay_sales_review_discovers_exact_match_without_mutating_local_state(
    monkeypatch, tmp_path
):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(
        title="Olympus recorder",
        source="gift",
        acquired_at="2026-09-01",
        acquisition_cost="0",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    store.transition_status(item.inventory_id, "listed")
    _mock_live_orders(
        monkeypatch,
        [_ebay_order(_order_line(), _order_line("line-2", None), _order_line("line-3", "Q9999"))],
    )

    response = client.get("/ebay/orders")

    assert response.status_code == 200
    assert "Olympus recorder" in response.text
    assert "USD 75.00" in response.text
    assert "Unknown for this line" in response.text
    assert "USD 3.98" in response.text
    assert "matched" in response.text
    assert "missing sku" in response.text
    assert "unmatched" in response.text
    assert response.text.count("Import matched sale") == 1
    assert store.get("Q0001").status == "listed"
    assert store.list_sales() == []


def test_explicit_ebay_sale_import_is_idempotent_and_starts_incomplete(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(
        title="Olympus recorder",
        source="gift",
        acquired_at="2026-09-01",
        acquisition_cost="0",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    assert item.status == "acquired"
    _mock_live_orders(monkeypatch, [_ebay_order(_order_line())])
    data = {"order_id": "order-1", "line_item_id": "line-1"}

    rejected = client.post(
        "/ebay/orders/import",
        data=data,
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    first = client.post("/ebay/orders/import", data=data, follow_redirects=False)
    second = client.post("/ebay/orders/import", data=data, follow_redirects=False)

    assert rejected.status_code == 403
    assert first.status_code == second.status_code == 303
    assert first.headers["location"].startswith("/sales/S000001")
    assert "already+imported" in second.headers["location"]
    assert len(store.list_sales()) == 1
    sale = store.list_sales()[0]
    assert sale.item_revenue == Decimal("75.00")
    assert sale.buyer_shipping == Decimal("8.07")
    assert sale.gross_amount == Decimal("83.07")
    assert sale.marketplace_tax == Decimal("3.98")
    assert store.get("Q0001").status == "sold"
    assert store.list_sale_costs(sale.sale_id) == []
    assert store.reconciliation_status(sale.sale_id)[0] == "incomplete"


def test_ebay_order_failure_and_stale_selection_leave_authoritative_state_unchanged(
    monkeypatch, tmp_path
):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(
        title="Recorder",
        source="gift",
        acquired_at="2026-09-01",
        acquisition_cost="0",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    store.transition_status(item.inventory_id, "listed")
    _mock_live_orders(monkeypatch, [])

    stale = client.post(
        "/ebay/orders/import",
        data={"order_id": "gone", "line_item_id": "gone"},
        follow_redirects=False,
    )

    assert stale.status_code == 303
    assert "no+longer+available" in stale.headers["location"]
    assert store.get("Q0001").status == "listed"
    assert store.list_sales() == []

    monkeypatch.setattr(
        web_app,
        "_live_order_review",
        lambda store: (_ for _ in ()).throw(web_app.FulfillmentApiError("secret raw payload")),
    )
    failed = client.post(
        "/ebay/orders/import",
        data={"order_id": "order-1", "line_item_id": "line-1"},
        follow_redirects=False,
    )
    assert failed.status_code == 303
    assert "temporarily+unavailable" in failed.headers["location"]
    assert "secret" not in failed.headers["location"]
    assert store.get("Q0001").status == "listed"
    assert store.list_sales() == []


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


def test_shared_shell_brand_favicon_navigation_and_active_state(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    response = client.get("/inventory")
    favicon = client.get("/static/flipper-logo2.png")

    assert response.status_code == 200
    assert favicon.status_code == 200
    assert favicon.headers["content-type"] == "image/png"
    assert (
        'rel="icon" type="image/png" href="http://testserver/static/flipper-logo2.png"'
        in response.text
    )
    assert '<img src="http://testserver/static/flipper-logo2.png"' in response.text
    assert 'aria-label="Primary"' in response.text
    assert 'class="active" href="/inventory" aria-current="page"' in response.text
    for path in (
        "/",
        "/deals",
        "/deals/history",
        "/inventory",
        "/ebay/listings",
        "/sales",
        "/insights",
        "/settings",
    ):
        assert f'href="{path}"' in response.text
    assert "Deals" in response.text
    assert 'href="/analytics"' not in response.text
    assert 'href="/analyze"' not in response.text

    history = client.get("/deals/history")
    assert 'class="active" href="/deals/history" aria-current="page"' in history.text
    assert 'class="active" href="/deals"' not in history.text


def test_all_local_primary_pages_render_shared_shell(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    for path in (
        "/",
        "/inventory",
        "/sales",
        "/analytics",
        "/insights",
        "/analyze",
        "/settings",
    ):
        response = client.get(path)
        assert response.status_code == 200
        assert "Flipper dashboard" in response.text
        assert "Skip to content" in response.text


def test_statuses_have_text_not_color_alone(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(
        title="Textual state",
        source="test",
        acquired_at="2026-09-01",
        acquisition_cost="10.00",
    )

    response = client.get("/inventory")

    assert response.status_code == 200
    assert f'href="/inventory/{item.inventory_id}"' in response.text
    assert ">acquired</span>" in response.text


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
    assert "Workflow" in dashboard.text
    assert 'href="/deals/history"' in dashboard.text
    assert 'href="/insights"' in dashboard.text


def test_inventory_page_does_not_load_sale_only_reporting_data(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    store.add(
        title="Inventory only",
        source="test",
        acquired_at="2026-09-19",
        acquisition_cost="10.00",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("inventory page loaded sale-only reporting data")

    monkeypatch.setattr(InventoryStore, "list_all_sale_costs", forbidden)
    monkeypatch.setattr(InventoryStore, "list_reconciliation_confirmations", forbidden)
    monkeypatch.setattr(InventoryStore, "list_sourcing_travel", forbidden)

    response = client.get("/inventory")

    assert response.status_code == 200
    assert "Inventory only" in response.text


def test_inventory_actual_sourcing_travel_web_flow_and_origin(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(
        title="Travel item", source="estate", acquired_at="2026-09-01", acquisition_cost="20"
    )
    assert (
        "No actual sourcing travel recorded" in client.get(f"/inventory/{item.inventory_id}").text
    )
    rejected = client.post(
        f"/inventory/{item.inventory_id}/sourcing-travel",
        data={"fuel_cost": "3"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert rejected.status_code == 403
    saved = client.post(
        f"/inventory/{item.inventory_id}/sourcing-travel",
        data={
            "round_trip_miles": "14.5",
            "fuel_cost": "3.25",
            "additional_expense": "0",
            "travel_minutes": "25",
            "note": "actual receipt",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    detail = client.get(saved.headers["location"])
    assert "USD 3.25" in detail.text
    assert "actual receipt" in detail.text
    assert store.get_sourcing_travel(item.inventory_id).total_expense == Decimal("3.25")
    invalid = client.post(
        f"/inventory/{item.inventory_id}/sourcing-travel",
        data={"fuel_cost": "-1"},
        follow_redirects=False,
    )
    assert "error_message=" in invalid.headers["location"]
    assert store.get_sourcing_travel(item.inventory_id).fuel_cost == Decimal("3.25")
    assert client.get(f"/inventory/{item.inventory_id}/sourcing-travel").status_code == 405
    cleared = client.post(
        f"/inventory/{item.inventory_id}/sourcing-travel/clear",
        data={"confirm": "1"},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert store.get_sourcing_travel(item.inventory_id) is None


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
    complete_detail = client.get(f"/sales/{complete.sale_id}")
    analytics = client.get("/analytics")

    assert "incomplete" in listing.text
    assert "fully reconciled" in listing.text
    assert "USD 66.50" in detail.text
    assert "fees, shipping, refunds, adjustments" in detail.text
    assert "manual" in detail.text
    assert "label" in detail.text
    assert "Accounting incomplete" in detail.text
    assert "Recorded profit (provisional)" in detail.text
    assert "Recorded margin (provisional)" in detail.text
    assert "Final realized profit, margin, and ROI are unavailable" in detail.text
    assert "Realized profit" in complete_detail.text
    assert "Realized margin" in complete_detail.text
    assert "Recorded profit (provisional)" not in complete_detail.text
    assert "2026-09" in analytics.text
    assert "USD 150.00" in analytics.text


def test_sale_detail_accounting_editor_preserves_component_semantics(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item, sale = _sold(
        store,
        1,
        gross="83.07",
        item_revenue="75",
        buyer_shipping="8.07",
        marketplace_tax="3.98",
    )

    detail = client.get(f"/sales/{sale.sale_id}")
    assert '<span class="value-primary">USD 3.98</span>' in detail.text
    assert (
        '<small class="value-note">Excluded from seller revenue and profit</small>' in detail.text
    )
    assert "USD 3.98Excluded" not in detail.text
    assert '<span class="value-primary">−USD 0.00</span>' in detail.text
    assert '<small class="value-note">Missing categories remain unknown</small>' in detail.text
    assert "USD 0.00Missing categories" not in detail.text
    assert "Complete reconciliation" in detail.text

    editor = client.get(f"/sales/{sale.sale_id}?edit_accounting=true")
    assert editor.status_code == 200
    assert "Marketplace fees" in editor.text
    assert "Seller-paid shipping expense" in editor.text
    assert "Buyer-paid shipping is revenue" in editor.text
    assert 'value="marketplace_tax"' not in editor.text
    assert 'value="buyer_shipping"' not in editor.text

    rejected = client.post(
        f"/sales/{sale.sale_id}/accounting",
        data={"category": "fees", "action": "record", "amount": "12.24"},
        headers={"Origin": "https://attacker.example"},
    )
    assert rejected.status_code == 403
    assert store.list_sale_costs(sale.sale_id) == []

    fee = client.post(
        f"/sales/{sale.sale_id}/accounting",
        data={"category": "fees", "action": "record", "amount": "12.24"},
        follow_redirects=False,
    )
    shipping = client.post(
        f"/sales/{sale.sale_id}/accounting",
        data={"category": "shipping", "action": "record", "amount": "5.00"},
        follow_redirects=False,
    )
    assert store.reconciliation_status(sale.sale_id) == (
        "partially_reconciled",
        ("refunds", "adjustments"),
    )
    refunds = client.post(
        f"/sales/{sale.sale_id}/accounting",
        data={"category": "refunds", "action": "confirm_zero"},
        follow_redirects=False,
    )
    assert fee.status_code == shipping.status_code == refunds.status_code == 303
    costs = store.list_sale_costs(sale.sale_id)
    assert [(cost.category, cost.amount) for cost in costs] == [
        ("marketplace_fee", Decimal("12.24")),
        ("shipping_cost", Decimal("5")),
    ]
    assert "Confirm reviewed" in client.get(f"/sales/{sale.sale_id}?edit_accounting=true").text
    assert store.reconciliation_status(sale.sale_id) == (
        "partially_reconciled",
        ("adjustments",),
    )
    partial = client.get(f"/sales/{sale.sale_id}")
    assert "Recorded profit (provisional)" in partial.text
    assert "Final realized profit, margin, and ROI are unavailable" in partial.text
    assert "USD 40.83" in partial.text

    invalid_tax = client.post(
        f"/sales/{sale.sale_id}/accounting",
        data={"category": "marketplace_tax", "action": "record", "amount": "3.98"},
        follow_redirects=False,
    )
    assert "error_message=" in invalid_tax.headers["location"]
    assert len(store.list_sale_costs(sale.sale_id)) == 2

    adjustments = client.post(
        f"/sales/{sale.sale_id}/accounting",
        data={"category": "adjustments", "action": "confirm_zero"},
        follow_redirects=False,
    )
    assert adjustments.status_code == 303
    assert store.reconciliation_status(sale.sale_id) == ("fully_reconciled", ())
    complete = client.get(adjustments.headers["location"])
    assert "Realized profit" in complete.text
    assert "Recorded profit (provisional)" not in complete.text
    assert "Final realized profit, margin, and ROI are unavailable" not in complete.text
    assert "USD 40.83" in complete.text


def test_zero_cost_incomplete_sale_is_visibly_provisional_without_changing_math(
    monkeypatch, tmp_path
):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(
        title="Olympus recorder",
        source="gift",
        acquired_at="2026-09-01",
        acquisition_cost="0",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="Q0001",
        quantity=1,
        gross_amount=Decimal("75.00"),
        currency="USD",
        sold_at=datetime(2026, 9, 19, tzinfo=timezone.utc),
    )

    detail = client.get(f"/sales/{sale.sale_id}")
    listing = client.get("/sales")
    inventory = client.get(f"/inventory/{item.inventory_id}")
    dashboard = client.get("/")

    assert sale.gross_amount == Decimal("75.00")
    assert store.get(item.inventory_id).acquisition_cost == Decimal("0")
    assert store.list_sale_costs(sale.sale_id) == []
    assert store.reconciliation_status(sale.sale_id) == (
        "incomplete",
        ("fees", "shipping", "refunds", "adjustments"),
    )
    assert "USD 75.00" in detail.text
    assert "USD 0.00" in detail.text
    assert "Recorded profit (provisional)" in detail.text
    assert "Recorded margin (provisional)" in detail.text
    assert "Missing categories remain unknown" in detail.text
    assert "Final realized profit, margin, and ROI are unavailable" in detail.text
    assert "provisional" in listing.text and "100.0% margin" in listing.text
    assert "missing categories unknown" in listing.text
    assert "Recorded profit (provisional)" in inventory.text
    assert "Final realized profit and ROI are unavailable" in inventory.text
    assert "Recorded profit (includes provisional)" in dashboard.text
    assert "Fully reconciled realized profit" in dashboard.text


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
    monkeypatch.setattr(
        "ebay.seller_oauth.keyring.get_password", lambda *_: "refresh-token-secret-value"
    )

    response = client.get("/settings")

    assert response.status_code == 200
    assert "Production" in response.text
    assert "Connected" in response.text
    assert "Complete" in response.text
    for value in (*secrets.values(), "refresh-token-secret-value"):
        assert value not in response.text


def test_settings_displays_sandbox_and_disconnected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("EBAY_SELLER_ENV", "sandbox")
    monkeypatch.setenv("EBAY_SELLER_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_SELLER_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_SELLER_RUNAME", "runame")
    monkeypatch.setattr("ebay.seller_oauth.keyring.get_password", lambda *_: None)

    response = client.get("/settings")

    assert "Sandbox" in response.text
    assert "Complete" in response.text
    assert "Disconnected" in response.text


def test_settings_handles_incomplete_configuration(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("EBAY_SELLER_ENV", "production")
    monkeypatch.setenv("EBAY_SELLER_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_SELLER_CLIENT_SECRET", "secret-not-rendered")
    monkeypatch.delenv("EBAY_SELLER_RUNAME", raising=False)

    response = client.get("/settings")

    assert "Production" in response.text
    assert "Incomplete — missing seller client configuration" in response.text
    assert "secret-not-rendered" not in response.text


def test_settings_handles_credential_store_failure(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("EBAY_SELLER_ENV", "production")
    monkeypatch.setenv("EBAY_SELLER_CLIENT_ID", "client")
    monkeypatch.setenv("EBAY_SELLER_CLIENT_SECRET", "secret")
    monkeypatch.setenv("EBAY_SELLER_RUNAME", "runame")

    def fail(*_):
        raise KeyringError("synthetic secret error")

    monkeypatch.setattr("ebay.seller_oauth.keyring.get_password", fail)

    response = client.get("/settings")

    assert "Authorization unavailable" in response.text
    assert "synthetic secret error" not in response.text


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


def test_inventory_detail_and_controlled_attachment_serving(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    first = store.add(
        title="Attached item",
        source="sale",
        acquired_at="2026-09-01",
        acquisition_cost="10.00",
    )
    second = store.add(
        title="Other item",
        source="sale",
        acquired_at="2026-09-02",
        acquisition_cost="12.00",
    )
    photo = tmp_path / "private receipt photo.jpg"
    photo.write_bytes(b"\xff\xd8\xffsynthetic-web-image")
    attachment = AttachmentService(store).add(first.inventory_id, photo, category="receipt")

    detail = client.get(f"/inventory/{first.inventory_id}")
    served = client.get(f"/inventory/{first.inventory_id}/attachments/{attachment.attachment_id}")
    wrong_owner = client.get(
        f"/inventory/{second.inventory_id}/attachments/{attachment.attachment_id}"
    )
    unknown = client.get(f"/inventory/{first.inventory_id}/attachments/unknown")

    assert detail.status_code == 200
    assert "Attachments" in detail.text
    assert "private receipt photo.jpg" in detail.text
    assert str(tmp_path) not in detail.text
    assert attachment.stored_filename not in detail.text
    assert served.status_code == 200
    assert served.content == photo.read_bytes()
    assert served.headers["content-type"] == "image/jpeg"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert wrong_owner.status_code == 404
    assert unknown.status_code == 404


def _mock_live_listings(monkeypatch, listings):
    monkeypatch.setattr(web_app.SellerOAuthConfig, "from_environment", lambda: object())
    monkeypatch.setattr(web_app, "SellerOAuthClient", lambda config: object())
    monkeypatch.setattr(
        web_app,
        "ActiveListingsClient",
        lambda oauth: type("Client", (), {"get_active_listings": lambda self: listings})(),
    )


def test_listing_get_is_read_only_and_renders_all_attention_states(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    local = store.add(
        title="Local",
        source="test",
        acquired_at="2026-09-01",
        acquisition_cost="10.00",
    )
    conflict = store.add(
        title="Sold local",
        source="test",
        acquired_at="2026-08-01",
        acquisition_cost="5.00",
    )
    store.transition_status(conflict.inventory_id, "listed")
    store.transition_status(conflict.inventory_id, "sold")
    listings = [
        EbayActiveListing("1", local.inventory_id, "Matched", "Active", None, 1),
        EbayActiveListing("2", "Q0007", "Missing local", "Active", Money(Decimal("75"), "USD"), 1),
        EbayActiveListing("3", None, "Missing SKU", "Active", None, 1),
        EbayActiveListing("4", "bad", "Invalid SKU", "Active", None, 1),
        EbayActiveListing("5", conflict.inventory_id, "Conflict", "Active", None, 1),
    ]
    _mock_live_listings(monkeypatch, listings)

    response = client.get("/ebay/listings")

    assert response.status_code == 200
    assert store.get(local.inventory_id).status == "acquired"
    assert "Linked to Q0001" in response.text
    assert "Import into Flipper" in response.text
    assert "no Flipper Q-number SKU" in response.text
    assert "not a canonical Flipper Q-number" in response.text
    assert "Review manually" in response.text
    assert "eBay asking price: USD 75.00" in response.text
    assert 'value="75' not in response.text


def test_web_sync_updates_local_state_idempotently_and_redirects(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item = store.add(title="Item", source="test", acquired_at="2026-09-01", acquisition_cost="1")
    listing = EbayActiveListing("item-1", item.inventory_id, "Item", "Active", None, 1)
    _mock_live_listings(monkeypatch, [listing])

    first = client.post("/ebay/listings/sync", data={"confirm": "1"}, follow_redirects=False)
    second = client.post("/ebay/listings/sync", data={"confirm": "1"}, follow_redirects=False)

    assert first.status_code == second.status_code == 303
    assert store.get(item.inventory_id).status == "listed"
    assert store.get(item.inventory_id).marketplace_item_id == "item-1"
    assert "updated" in first.headers["location"]
    assert "already" in second.headers["location"]


def test_web_import_allows_unknown_acquisition_and_is_idempotent(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    listing = EbayActiveListing(
        "item-7", "Q0007", "Imported", "Active", Money(Decimal("75"), "USD"), 1
    )
    _mock_live_listings(monkeypatch, [listing])

    missing = client.post(
        "/ebay/listings/import",
        data={
            "item_id": "item-7",
            "sku": "Q0007",
            "source": "",
            "acquired_at": "",
            "acquisition_cost": "",
        },
        follow_redirects=False,
    )
    first = missing
    second = client.post(
        "/ebay/listings/import",
        data={"item_id": "item-7", "sku": "Q0007"},
        follow_redirects=False,
    )

    assert missing.status_code == first.status_code == second.status_code == 303
    record = store.get("Q0007")
    assert record.acquisition_cost is None
    assert record.acquisition_cost_cents is None
    assert record.acquired_at is None
    assert record.source is None
    assert record.status == "listed"
    assert record.marketplace_item_id == "item-7"
    assert record.marketplace_sku == "Q0007"
    assert len(store.list()) == 1
    assert (
        store.add(
            title="Next", source="test", acquired_at="2026-09-01", acquisition_cost="0"
        ).inventory_id
        == "Q0008"
    )
    detail = client.get("/inventory/Q0007")
    assert detail.status_code == 200
    assert detail.text.count("Unknown") >= 3
    assert "USD 75.00" not in detail.text


def test_inventory_notes_can_be_edited_cleared_and_reject_cross_origin(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    original = store.add(
        title="Recorder",
        source="estate",
        acquired_at="2026-09-01",
        acquisition_cost="12.34",
        notes="old note",
    )
    read = client.get(f"/inventory/{original.inventory_id}")
    edit = client.get(f"/inventory/{original.inventory_id}?edit_notes=true")
    assert "old note" in read.text
    assert 'name="notes"' not in read.text
    assert ">Edit</a>" in read.text
    assert '<textarea name="notes"' in edit.text
    assert "old note</textarea>" in edit.text
    assert f'href="/inventory/{original.inventory_id}">Cancel</a>' in edit.text
    assert store.get(original.inventory_id).notes == "old note"

    rejected = client.post(
        f"/inventory/{original.inventory_id}/notes",
        data={"notes": "attacker"},
        headers={"origin": "https://attacker.example"},
    )
    edited = client.post(
        f"/inventory/{original.inventory_id}/notes",
        data={"notes": "new note"},
        follow_redirects=False,
    )
    after_edit = store.get(original.inventory_id)
    cleared = client.post(
        f"/inventory/{original.inventory_id}/notes",
        data={"notes": ""},
        follow_redirects=False,
    )
    after_clear = store.get(original.inventory_id)

    assert rejected.status_code == 403
    assert edited.status_code == cleared.status_code == 303
    assert after_edit.notes == "new note"
    assert after_clear.notes == ""
    assert after_edit.title == after_clear.title == original.title
    assert after_edit.source == after_clear.source == original.source
    assert after_edit.acquired_at == after_clear.acquired_at == original.acquired_at
    assert after_edit.acquisition_cost_cents == after_clear.acquisition_cost_cents == 1234
    assert after_edit.status == after_clear.status == original.status
    cleared_detail = client.get(f"/inventory/{original.inventory_id}")
    assert "No notes" in cleared_detail.text
    assert ">Add note</a>" in cleared_detail.text
    assert 'name="notes"' not in cleared_detail.text


def test_ebay_errors_are_sanitized_and_dashboard_never_fetches(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    calls = 0

    class BrokenClient:
        def __init__(self, oauth):
            pass

        def get_active_listings(self):
            nonlocal calls
            calls += 1
            raise web_app.ActiveListingsApiError("raw-token-secret raw XML")

    monkeypatch.setattr(web_app.SellerOAuthConfig, "from_environment", lambda: object())
    monkeypatch.setattr(web_app, "SellerOAuthClient", lambda config: object())
    monkeypatch.setattr(web_app, "ActiveListingsClient", BrokenClient)

    dashboard = client.get("/")
    listings = client.get("/ebay/listings")

    assert dashboard.status_code == 200
    assert calls == 1
    assert listings.status_code == 503
    assert "temporarily unavailable" in listings.text
    assert "raw-token-secret" not in listings.text


def _discovery_item():
    return {
        "itemId": "v1|123|0",
        "title": "Used mirrorless camera",
        "price": {"value": "55.00", "currency": "USD"},
        "categoryId": "293",
        "condition": "Used",
        "itemWebUrl": "https://www.ebay.com/itm/123",
        "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "USD"}}],
    }


class _Discovery:
    def search(self, *args, **kwargs):
        return {"itemSummaries": [_discovery_item()]}

    def get_item(self, item_id):
        assert item_id == "v1|123|0"
        return _discovery_item()


def test_deals_workspace_search_detail_and_unknown_economics(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _Discovery)

    landing = client.get("/deals")
    results = client.get("/deals?q=camera&sort=price_asc")
    detail = client.get("/deals/ebay/v1%7C123%7C0")
    analyzed = client.get(
        "/deals/ebay/v1%7C123%7C0",
        params={
            "tax": "2",
            "travel_cost": "0",
            "other_acquisition_cost": "0",
            "expected_resale": "100",
            "selling_fees": "10",
            "outbound_shipping": "8",
            "other_selling_cost": "0",
            "minimum_sale_days": "4",
            "maximum_sale_days": "7",
        },
    )

    assert landing.status_code == results.status_code == detail.status_code == 200
    assert 'name="listing_status" value="active"' in landing.text
    assert "Research mode" in landing.text
    assert "Active listings</a>" in landing.text and "Sold comparables</a>" in landing.text
    assert "Sold listings</a>" not in landing.text
    assert "Start with a deliberate search" in landing.text
    assert "Used mirrorless camera" in results.text
    assert "Needs resale estimate" in results.text
    assert 'aria-label="Deal Score"' in results.text
    assert "Deal Score" in results.text and "Unavailable" in results.text
    assert "Needs evaluation" in results.text
    assert (
        "sold comparable evidence in the asking-price currency is unavailable" not in results.text
    )
    assert "5.0 / 10" not in results.text
    assert 'href="/deals/ebay/v1|123|0">Evaluate deal</a>' in results.text
    assert 'class="active" href="/deals"' in results.text
    assert "Profit velocity" in detail.text and "Unavailable" in detail.text
    assert "USD 20.00" in analyzed.text
    assert "USD 2.86" in analyzed.text and "USD 5.00/day" in analyzed.text
    assert "sold comparables" in analyzed.text
    assert "sold comparable evidence in the asking-price currency is unavailable" in analyzed.text


def test_deal_detail_loads_comparable_rows_once(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _Discovery)
    calls = 0
    original_list = web_app.research_store.list

    def counted_list(session_id, opportunity_key):
        nonlocal calls
        calls += 1
        return original_list(session_id, opportunity_key)

    monkeypatch.setattr(web_app.research_store, "list", counted_list)

    response = client.get("/deals/ebay/v1%7C123%7C0")

    assert response.status_code == 200
    assert calls == 1


def test_sold_comparables_mode_is_manual_and_never_calls_ebay(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    def unexpected_discovery():
        raise AssertionError("sold mode must not call Browse or seller APIs")

    monkeypatch.setattr(web_app, "_discovery", unexpected_discovery)
    response = client.get(
        "/deals",
        params={
            "listing_status": "sold",
            "q": "camera",
            "category": "electronics",
            "sort": "price_desc",
        },
    )

    assert response.status_code == 200
    assert 'href="/deals?' in response.text
    assert "listing_status=active" in response.text
    assert "q=camera" in response.text and "sort=price_desc" in response.text
    assert "Marketplace-wide sold eBay search is not available" in response.text
    assert "Add sold comparable" in response.text
    assert "sold price and currency" in response.text
    assert "sold date, when known" in response.text
    assert "Deal Score's sold-evidence requirement" in response.text
    assert "No sold search was run" in response.text
    assert "did not fall back to active results" in response.text
    assert "eBay relevance" not in response.text
    assert 'class="deal-grid"' not in response.text
    assert "Used mirrorless camera" not in response.text
    assert "ended listing is not evidence of a sale" in response.text


def test_active_mode_still_searches_and_sorts_by_asking_price(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    class MultipleResults(_Discovery):
        def search(self, *args, **kwargs):
            first = _discovery_item() | {
                "itemId": "v1|expensive|0",
                "title": "Expensive camera",
                "price": {"value": "90", "currency": "USD"},
            }
            second = _discovery_item() | {
                "itemId": "v1|affordable|0",
                "title": "Affordable camera",
                "price": {"value": "40", "currency": "USD"},
            }
            return {"itemSummaries": [first, second]}

    monkeypatch.setattr(web_app, "_discovery", MultipleResults)
    response = client.get("/deals?q=camera&sort=price_asc")

    assert response.status_code == 200
    assert "Active result sort" in response.text and "eBay relevance" in response.text
    assert response.text.index("Affordable camera") < response.text.index("Expensive camera")


def test_deal_acquisition_requires_actual_facts_and_uses_q_number(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _Discovery)
    missing = client.post(
        "/deals/ebay/v1%7C123%7C0/acquire",
        content="acquisition_cost=",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    acquired = client.post(
        "/deals/ebay/v1%7C123%7C0/acquire",
        data={
            "acquisition_cost": "47.25",
            "acquired_at": "2026-09-17",
            "acquisition_source": "eBay",
        },
        follow_redirects=False,
    )
    assert missing.status_code == acquired.status_code == 303
    assert "Required" in missing.headers["location"]
    assert acquired.headers["location"].startswith("/inventory/Q0001")
    record = store.get("Q0001")
    assert record.acquisition_cost == Decimal("47.25")
    assert record.acquired_at == "2026-09-17"
    assert record.marketplace_item_id == "v1|123|0"
    assert record.acquisition_cost != Decimal("55")


def test_deal_acquisition_rejects_cross_origin(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    response = client.post(
        "/deals/ebay/v1%7C123%7C0/acquire",
        data={"acquisition_cost": "1", "acquired_at": "2026-09-17", "acquisition_source": "x"},
        headers={"origin": "https://attacker.example"},
    )
    assert response.status_code == 403


class _ComparisonDiscovery:
    def search(self, *args, **kwargs):
        return {"itemSummaries": []}

    def get_item(self, item_id):
        number = item_id.rsplit("|", 2)[1]
        return _discovery_item() | {
            "itemId": item_id,
            "title": f"Camera {number}",
            "price": {"value": number, "currency": "USD"},
        }


def test_comparison_bounds_duplicates_refetch_and_independent_assumptions(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _ComparisonDiscovery)
    too_few = client.get("/deals/compare", params={"item_id": "v1|10|0"})
    duplicate = client.get(
        "/deals/compare", params=[("item_id", "v1|10|0"), ("item_id", "v1|10|0")]
    )
    too_many = client.get(
        "/deals/compare",
        params=[("item_id", f"v1|{value}|0") for value in range(10, 15)],
    )
    compared = client.get(
        "/deals/compare",
        params=[
            ("item_id", "v1|10|0"),
            ("item_id", "v1|20|0"),
            ("d0_expected_resale", "50"),
            ("d1_expected_resale", "100"),
            ("d0_tax", "0"),
            ("d1_tax", "0"),
            ("d0_travel_cost", "0"),
            ("d1_travel_cost", "0"),
            ("d0_other_acquisition_cost", "0"),
            ("d1_other_acquisition_cost", "0"),
            ("d0_selling_fees", "0"),
            ("d1_selling_fees", "0"),
            ("d0_outbound_shipping", "0"),
            ("d1_outbound_shipping", "0"),
            ("d0_other_selling_cost", "0"),
            ("d1_other_selling_cost", "0"),
            ("d0_minimum_sale_days", "2"),
            ("d0_maximum_sale_days", "4"),
            ("d0_asking_price", "0.01"),
        ],
    )
    assert too_few.status_code == duplicate.status_code == too_many.status_code == 400
    assert compared.status_code == 200
    assert "USD 35.00" in compared.text and "USD 75.00" in compared.text
    assert "USD 10.00" in compared.text and "USD 20.00" in compared.text
    assert "USD 0.01" not in compared.text
    assert 'aria-label="Deal comparison"' in compared.text
    assert store.list() == []


def test_comparison_isolates_disappeared_upstream_item(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    class Partial(_ComparisonDiscovery):
        def get_item(self, item_id):
            if item_id == "v1|gone|0":
                raise web_app.EbayDiscoveryError("gone")
            return super().get_item(item_id)

    monkeypatch.setattr(web_app, "_discovery", Partial)
    response = client.get(
        "/deals/compare",
        params=[("item_id", "v1|10|0"), ("item_id", "v1|gone|0")],
    )
    assert response.status_code == 200
    assert "upstream item is unavailable" in response.text


def test_comparable_add_edit_remove_prg_and_no_url_fetch(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    discovery = _Discovery()
    monkeypatch.setattr(web_app, "_discovery", lambda: discovery)
    web_app.research_store.clear()
    fields = {
        "evidence_type": "sold",
        "source": "Manual eBay research",
        "source_identity": "ebay",
        "price": "145.50",
        "currency": "USD",
        "condition": "used_tested",
        "event_date": "2026-09-10",
        "observed_date": "2026-09-17",
        "title": "Comparable camera",
        "source_url": "https://example.com/never-fetched",
    }
    added = client.post("/deals/ebay/v1%7C123%7C0/comparables", data=fields, follow_redirects=False)
    assert added.status_code == 303
    assert added.headers["location"].endswith("message=Comparable+added.")
    assert (
        "HttpOnly" in added.headers["set-cookie"]
        and "SameSite=strict" in added.headers["set-cookie"]
    )
    detail = client.get(added.headers["location"])
    assert "Sold evidence" in detail.text and "USD 145.50" in detail.text
    assert "User research" in detail.text and "Not verified by Flipper" in detail.text
    assert "Expected profit" in detail.text and "Needs assumptions" in detail.text
    rows = next(iter(web_app.research_store._sessions.values()))["ebay:v1|123|0"]
    comparable_id = rows[0].comparable_id
    edited = client.post(
        f"/deals/ebay/v1%7C123%7C0/comparables/{comparable_id}/edit",
        data=fields | {"price": "150", "evidence_type": "active_asking", "event_date": ""},
        follow_redirects=False,
    )
    assert edited.status_code == 303
    changed = client.get(edited.headers["location"])
    assert "Active asking evidence" in changed.text and "USD 150.00" in changed.text
    removed = client.post(
        f"/deals/ebay/v1%7C123%7C0/comparables/{comparable_id}/remove",
        content="confirm=1",
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert removed.status_code == 303
    assert "No sold evidence" in client.get(removed.headers["location"]).text


def test_comparable_validation_cross_origin_and_independent_comparison(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _ComparisonDiscovery)
    web_app.research_store.clear()
    base = {
        "evidence_type": "sold",
        "source": "Research",
        "price": "75",
        "currency": "USD",
        "condition": "unknown",
        "observed_date": "2026-09-17",
    }
    rejected = client.post(
        "/deals/ebay/v1%7C10%7C0/comparables",
        data=base,
        headers={"origin": "https://attacker.example"},
    )
    unsafe = client.post(
        "/deals/ebay/v1%7C10%7C0/comparables",
        data=base | {"source_url": "http://unsafe.example"},
        follow_redirects=False,
    )
    added = client.post("/deals/ebay/v1%7C10%7C0/comparables", data=base, follow_redirects=False)
    compared = client.get(
        "/deals/compare",
        params=[("item_id", "v1|10|0"), ("item_id", "v1|20|0")],
    )
    assert rejected.status_code == 403
    assert unsafe.status_code == added.status_code == 303
    assert "source+URL+must+be+an+HTTPS" in unsafe.headers["location"]
    assert compared.text.count("median USD 75.00") == 1
    assert "Comps do not establish sell-through or duration" in compared.text
    assert "winner" not in compared.text.lower()


def test_manual_opportunity_evaluate_compare_comparable_and_acquire(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _Discovery)
    web_app.research_store.clear()
    created = client.post(
        "/deals/opportunities",
        data={
            "source": "estate-sale",
            "title": "Estate camera",
            "category": "electronics",
            "base_price": "20.25",
            "currency": "USD",
            "inbound_shipping": "1.75",
            "condition": "used_untested",
            "source_url": "https://example.test/estate/1",
            "location_text": "Local hall",
            "notes": "Inspect lens",
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?", 1)[0]
    opportunity_id = path.rsplit("/", 1)[-1]

    detail = client.get(path)
    sold_mode = client.get("/deals?listing_status=sold")
    analyzed = client.get(
        path,
        params={
            "tax": "0",
            "travel_cost": "0",
            "other_acquisition_cost": "0",
            "expected_resale": "80",
            "selling_fees": "8",
            "outbound_shipping": "0",
            "other_selling_cost": "0",
        },
    )
    added = client.post(
        f"{path}/comparables",
        data={
            "evidence_type": "sold",
            "source": "User research",
            "price": "75",
            "currency": "USD",
            "condition": "used_tested",
            "observed_date": "2026-09-17",
        },
        follow_redirects=False,
    )
    compared = client.get(
        "/deals/compare",
        params=[
            ("opportunity_key", f"manual:{opportunity_id}"),
            ("opportunity_key", "ebay:v1|123|0"),
            ("d0_one_way_miles", "12"),
            ("d0_vehicle_mpg", "24"),
            ("d0_gas_price", "4"),
            ("d0_additional_travel_cost", "2"),
            ("d0_round_trip_minutes", "50"),
        ],
    )
    missing = client.post(
        f"{path}/acquire", data={"acquisition_source": "Estate sale"}, follow_redirects=False
    )
    acquired = client.post(
        f"{path}/acquire",
        data={
            "acquisition_cost": "18.00",
            "acquired_at": "2026-09-17",
            "acquisition_source": "Estate sale",
        },
        follow_redirects=False,
    )

    assert created.status_code == added.status_code == missing.status_code == 303
    assert "User-provided source fact" in detail.text
    assert "Needs assumptions" in detail.text
    assert "Opportunities that can receive sold evidence" in sold_mode.text
    assert f'href="{path}#comparable-evidence">Add sold comparable</a>' in sold_mode.text
    assert "Deal Score's sold-evidence requirement" in detail.text
    assert "USD 50.00" in analyzed.text
    assert "Estate Sale" in compared.text and "eBay" in compared.text
    assert "24 miles" in compared.text and "USD 6.00" in compared.text
    assert "50 minutes" in compared.text
    assert "actual+acquisition+cost" in missing.headers["location"]
    assert acquired.headers["location"].startswith("/inventory/Q0001")
    record = store.get("Q0001")
    assert record.acquisition_cost_cents == 1800
    assert record.marketplace == "estate-sale"
    assert "https://example.test/estate/1" in record.notes


def test_manual_opportunity_validation_and_browser_isolation(monkeypatch, tmp_path):
    first, _ = _client(monkeypatch, tmp_path)
    web_app.research_store.clear()
    rejected = first.post(
        "/deals/opportunities",
        data={
            "source": "other",
            "title": "Unsafe",
            "category": "electronics",
            "base_price": "nope",
            "currency": "USD",
            "condition": "unknown",
            "source_url": "http://localhost/private",
        },
        follow_redirects=False,
    )
    valid = first.post(
        "/deals/opportunities",
        data={
            "source": "local",
            "title": "Local item",
            "category": "electronics",
            "base_price": "10",
            "currency": "USD",
            "condition": "unknown",
        },
        follow_redirects=False,
    )
    second, _ = _client(monkeypatch, tmp_path)

    assert "asking+price" in rejected.headers["location"]
    assert first.get(valid.headers["location"]).status_code == 200
    assert second.get(valid.headers["location"]).status_code == 404


def test_discovery_failures_are_distinct_and_sanitized():
    assert "filters are invalid" in web_app._safe_discovery_error(ValueError("bad decimal"))
    assert "not configured" in web_app._safe_discovery_error(
        web_app.EbayDiscoveryError("eBay discovery is not configured")
    )
    rejected = web_app._safe_discovery_error(
        web_app.EbayDiscoveryError("authentication rejected secret-token-value")
    )
    assert "authentication was rejected" in rejected
    assert "secret-token-value" not in rejected


def test_manual_trip_economics_render_validate_and_do_not_become_actual_cost(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    web_app.research_store.clear()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("trip inputs must not cause an external request")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    created = client.post(
        "/deals/opportunities",
        data={
            "source": "facebook-marketplace",
            "title": "Local camera",
            "category": "electronics",
            "base_price": "20.25",
            "currency": "USD",
            "inbound_shipping": "1.75",
            "condition": "used_tested",
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?", 1)[0]
    analyzed = client.get(
        path,
        params={
            "tax": "0",
            "other_acquisition_cost": "0",
            "expected_resale": "80",
            "selling_fees": "8",
            "outbound_shipping": "0",
            "other_selling_cost": "0",
            "one_way_miles": "10",
            "vehicle_mpg": "20",
            "gas_price": "4",
            "additional_travel_cost": "1",
            "round_trip_minutes": "45",
        },
    )
    invalid = client.get(
        path,
        params={"one_way_miles": "10", "vehicle_mpg": "0", "gas_price": "4"},
    )
    conflict = client.get(
        path,
        params={"travel_cost": "5", "one_way_miles": "10"},
    )
    acquired = client.post(
        f"{path}/acquire",
        data={
            "acquisition_cost": "18.00",
            "acquired_at": "2026-09-17",
            "acquisition_source": "Facebook Marketplace",
        },
        follow_redirects=False,
    )

    assert analyzed.status_code == 200
    assert "20 miles" in analyzed.text
    assert "Estimated fuel used" in analyzed.text and "gallons" in analyzed.text
    assert "USD 4.00" in analyzed.text
    assert "USD 5.00" in analyzed.text
    assert "USD 27.00" in analyzed.text
    assert "USD 45.00" in analyzed.text
    assert "45 minutes" in analyzed.text
    assert invalid.status_code == conflict.status_code == 400
    assert 'value="0"' in invalid.text
    assert "vehicle MPG must be greater than 0" in invalid.text
    assert "cannot be combined" in conflict.text
    record = store.get(acquired.headers["location"].split("/inventory/", 1)[1].split("?", 1)[0])
    assert record.acquisition_cost_cents == 1800
    assert "fuel" not in record.notes.casefold()


def test_manual_research_snapshot_history_detail_immutability_and_link(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    web_app.research_store.clear()

    def forbidden(*_args, **_kwargs):
        raise AssertionError("snapshot reference URLs must never be requested")

    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    created = client.post(
        "/deals/opportunities",
        data={
            "source": "estate-sale",
            "title": "Snapshot camera",
            "category": "electronics",
            "base_price": "20.25",
            "currency": "USD",
            "inbound_shipping": "1.75",
            "condition": "used_tested",
            "source_url": "https://example.test/listing",
            "location_text": "Local hall",
            "notes": "Inspect lens",
        },
        follow_redirects=False,
    )
    path = created.headers["location"].split("?", 1)[0]
    comp = {
        "source": "Manual research",
        "source_identity": "other",
        "price": "70",
        "currency": "USD",
        "condition": "used_tested",
        "observed_date": "2026-09-17",
        "source_url": "https://example.test/comp",
    }
    client.post(f"{path}/comparables", data=comp | {"evidence_type": "sold"})
    client.post(f"{path}/comparables", data=comp | {"evidence_type": "active_asking"})
    assumptions = {
        "save_token": "1" * 32,
        "tax": "2",
        "expected_resale": "80",
        "selling_fees": "8",
        "outbound_shipping": "3",
        "other_acquisition_cost": "1",
        "other_selling_cost": "1",
        "minimum_sale_days": "5",
        "maximum_sale_days": "10",
        "one_way_miles": "10",
        "vehicle_mpg": "20",
        "gas_price": "4",
        "additional_travel_cost": "1",
        "round_trip_minutes": "45",
    }
    saved = client.post(f"{path}/snapshot", data=assumptions, follow_redirects=False)
    retry = client.post(f"{path}/snapshot", data=assumptions, follow_redirects=False)
    snapshot_path = saved.headers["location"].split("?", 1)[0]
    detail = client.get(snapshot_path)
    history = client.get("/deals/history")

    assert saved.status_code == retry.status_code == 303
    assert retry.headers["location"].split("?", 1)[0] == snapshot_path
    assert len(store.list_research_snapshots()) == 1
    assert detail.status_code == history.status_code == 200
    assert "Saved research snapshot" in detail.text
    assert "Deal Score at decision time" in detail.text
    assert "deal-score-v1" not in detail.text
    frozen_score = store.list_research_snapshots()[0]
    frozen_payload = store.get_research_snapshot(frozen_score.snapshot_id).payload
    assert frozen_payload["derived"]["deal_score"]["version"] == "deal-score-v1"
    assert frozen_payload["derived"]["deal_score"]["value"] is not None
    assert "Decision vs. Outcome" in detail.text
    assert "No actual outcome is available" in detail.text
    assert "Asking price at snapshot" in detail.text and "USD 20.25" in detail.text
    assert "Sold" in detail.text and "Active Asking" in detail.text
    assert "20 miles" in detail.text and "45 minutes" in detail.text
    assert "User-provided source fact" in detail.text
    assert "Snapshot camera" in history.text
    cards = client.get("/deals")
    assert cards.status_code == 200
    assert "From latest saved evaluation" in cards.text
    assert "Low confidence" in cards.text
    assert "Promising" in cards.text or "Exceptional potential" in cards.text
    assert 'aria-label="Deal Score"' in cards.text
    assert f'href="{path}">Evaluate deal</a>' in cards.text

    rows = next(iter(web_app.research_store._sessions.values()))
    for row in list(rows[next(iter(rows))]):
        web_app.research_store.remove(
            next(iter(web_app.research_store._sessions)), next(iter(rows)), row.comparable_id
        )
    assert "https://example.test/comp" in client.get(snapshot_path).text

    item = store.add(
        title="Camera", source="estate", acquired_at="2026-09-17", acquisition_cost="18.00"
    )
    linked = client.post(
        f"{snapshot_path}/link", data={"inventory_id": item.inventory_id}, follow_redirects=False
    )
    assert linked.status_code == 303
    linked_detail = client.get(linked.headers["location"])
    assert item.inventory_id in linked_detail.text
    assert "Outcome in progress · Acquired" in linked_detail.text
    assert store.get(item.inventory_id).acquisition_cost == Decimal("18.00")

    store.transition_status(item.inventory_id, "listed")
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="ebay",
        external_order_id="snapshot-order",
        external_line_item_id="snapshot-line",
        marketplace_sku=item.inventory_id,
        quantity=1,
        gross_amount=Decimal("70.00"),
        currency="USD",
        sold_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    store.add_sale_cost(sale.sale_id, category="marketplace_fee", amount=Decimal("7.00"))
    realized = client.get(snapshot_path)
    assert "Realized outcome" in realized.text
    assert "USD 70.00" in realized.text
    assert "Accounting incomplete" in realized.text
    assert "recorded profit (provisional)" in realized.text
    assert "final realized profit and ROI are unavailable" in realized.text
    assert "Unavailable" in realized.text
    assert f'href="/sales/{sale.sale_id}"' in realized.text
    assert (
        store.get_research_snapshot(frozen_score.snapshot_id).payload["derived"]["deal_score"]
        == frozen_payload["derived"]["deal_score"]
    )

    for category in ("fees", "shipping", "refunds", "adjustments"):
        store.set_reconciliation_confirmation(sale.sale_id, category, confirmed=True)
    completed = client.get(snapshot_path)
    assert "Net profit / realized profit" in completed.text
    assert "Accounting incomplete" not in completed.text

    inventory_detail = client.get(f"/inventory/{item.inventory_id}")
    sale_detail = client.get(f"/sales/{sale.sale_id}")
    assert f'href="{snapshot_path}"' in inventory_detail.text
    assert "Open Decision vs. Outcome" in inventory_detail.text
    assert f'href="{snapshot_path}"' in sale_detail.text
    assert "Compare explicitly linked saved research" in sale_detail.text


def test_snapshot_routes_reject_cross_origin_and_malformed_ids(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    rejected = client.post(
        "/deals/opportunities/missing/snapshot",
        data={"save_token": "a" * 32},
        headers={"origin": "https://attacker.example"},
    )
    missing = client.get("/deals/history/not-a-valid-id")
    empty = client.get("/deals/history")
    assert rejected.status_code == 403
    assert missing.status_code == 404
    assert "No saved research yet" in empty.text
    assert 'href="/deals"' in empty.text


def test_ebay_snapshot_captures_normalized_api_facts_without_raw_secrets(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app, "_discovery", _Discovery)
    web_app.research_store.clear()
    response = client.post(
        "/deals/ebay/v1%7C123%7C0/snapshot",
        data={"save_token": "2" * 32, "expected_resale": "200", "selling_fees": "20"},
        follow_redirects=False,
    )
    listed = store.list_research_snapshots()[0]
    snapshot = store.get_research_snapshot(listed.snapshot_id)
    serialized = str(snapshot.payload)

    assert response.status_code == 303
    assert snapshot.source == "ebay"
    assert snapshot.opportunity_identity == "ebay:v1|123|0"
    assert snapshot.payload["opportunity"]["source_listing_id"] == "v1|123|0"
    assert snapshot.payload["opportunity"]["base_price_provenance"]["kind"] == "source_api"
    assert "authorization" not in serialized.casefold()
    assert "oauth" not in serialized.casefold()
    assert "raw_response" not in serialized.casefold()
