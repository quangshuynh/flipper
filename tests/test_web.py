from datetime import datetime, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from keyring.errors import KeyringError

from inventory.store import InventoryStore
from inventory.attachments import AttachmentService
from ebay.listings import EbayActiveListing
from ebay.orders import Money
import web.app as web_app
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
    assert 'class="active" href="/inventory"' in response.text
    for path in (
        "/",
        "/deals",
        "/inventory",
        "/ebay/listings",
        "/sales",
        "/analytics",
        "/analyze",
        "/settings",
    ):
        assert f'href="{path}"' in response.text
    assert "Deals" in response.text


def test_all_local_primary_pages_render_shared_shell(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)

    for path in ("/", "/inventory", "/sales", "/analytics", "/analyze", "/settings"):
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


def test_web_import_requires_actual_facts_and_is_idempotent(monkeypatch, tmp_path):
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
    payload = {
        "item_id": "item-7",
        "sku": "Q0007",
        "source": "estate sale",
        "acquired_at": "2026-08-02",
        "acquisition_cost": "12.34",
    }
    first = client.post("/ebay/listings/import", data=payload, follow_redirects=False)
    second = client.post("/ebay/listings/import", data=payload, follow_redirects=False)

    assert missing.status_code == first.status_code == second.status_code == 303
    assert "Required" in missing.headers["location"]
    record = store.get("Q0007")
    assert record.acquisition_cost == Decimal("12.34")
    assert record.acquired_at == "2026-08-02"
    assert record.source == "estate sale"
    assert record.status == "listed"
    assert len(store.list()) == 1
    assert (
        store.add(
            title="Next", source="test", acquired_at="2026-09-01", acquisition_cost="0"
        ).inventory_id
        == "Q0008"
    )


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
    assert "Start with a deliberate search" in landing.text
    assert "Used mirrorless camera" in results.text
    assert "Needs resale estimate" in results.text
    assert 'class="active" href="/deals"' in results.text
    assert "Profit velocity" in detail.text and "Unavailable" in detail.text
    assert "USD 20.00" in analyzed.text
    assert "USD 2.86" in analyzed.text and "USD 5.00/day" in analyzed.text
    assert "sold comparables" in analyzed.text


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
