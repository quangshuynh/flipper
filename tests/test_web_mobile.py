"""Structural guarantees for phone use of the server-rendered web UI (Hosting Interval D)."""

import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from inventory.store import InventoryStore
import web.app as web_app
from web.app import app
from tests.web_client import local_client

WEB = Path(web_app.__file__).parent
APP_CSS = (WEB / "static" / "app.css").read_text(encoding="utf-8")
PRIMARY_DESTINATIONS = (
    "/",
    "/deals",
    "/deals/history",
    "/inventory",
    "/ebay/listings",
    "/sales",
    "/insights",
    "/settings",
)


def _client(monkeypatch, tmp_path):
    database = tmp_path / "mobile.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    return local_client(app), InventoryStore(database)


def _seed(store):
    item = store.add(
        title="Lenovo ThinkPad X1 Carbon Gen 11 " * 4,
        source="Goodwill",
        acquired_at="2026-08-01",
        acquisition_cost="25.00",
        marketplace="eBay",
        marketplace_sku="Q0001",
    )
    store.set_sourcing_travel(item.inventory_id, round_trip_miles="10", fuel_cost="1.50")
    store.transition_status(item.inventory_id, "listed")
    sale, _ = store.import_sale(
        inventory_id=item.inventory_id,
        marketplace="eBay",
        external_order_id="order-1",
        external_line_item_id="line-1",
        marketplace_sku="Q0001",
        quantity=1,
        gross_amount=Decimal("100.00"),
        currency="USD",
        sold_at=datetime(2026, 9, 1, 12, tzinfo=timezone.utc),
    )
    return item, sale


def _mobile_rules():
    """CSS text of the phone/touch media blocks."""
    return re.findall(r"@media\(max-width:900px\),\(pointer:coarse\)\{(.*?\})\}", APP_CSS)


def test_viewport_keeps_pinch_zoom_on_every_layout():
    for name in ("base.html", "login.html"):
        template = (WEB / "templates" / name).read_text(encoding="utf-8")
        viewport = re.search(r'<meta name="viewport" content="([^"]+)">', template).group(1)
        assert viewport == "width=device-width, initial-scale=1"
    for css in (WEB / "static").glob("*.css"):
        text = css.read_text(encoding="utf-8")
        assert "user-scalable" not in text and "maximum-scale" not in text
        assert "touch-action:none" not in text.replace(" ", "")


def test_phone_form_controls_use_at_least_16px_text():
    rules = "".join(_mobile_rules())
    font_rule = re.search(r"([^{}]*)\{font-size:max\(1rem,16px\)\}", rules)
    assert font_rule is not None
    selectors = {selector.strip() for selector in font_rule.group(1).split(",")}
    # The compact comparable form sets its own textarea font, so it is listed explicitly.
    assert {"input", "select", "textarea", ".compact-form textarea"} <= selectors
    assert "min-height:44px" in rules
    # The Interval C login rule stays at 16px on every viewport.
    assert ".login-panel input{width:100%;box-sizing:border-box;font-size:16px}" in APP_CSS


def test_primary_navigation_keeps_every_destination_and_current_page(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    _seed(store)

    page = client.get("/inventory").text

    nav = re.search(r'<nav aria-label="Primary">(.*?)</nav>', page, re.S).group(1)
    assert re.findall(r'href="([^"]+)"', nav) == list(PRIMARY_DESTINATIONS)
    assert re.findall(r'aria-current="page"', nav) == ['aria-current="page"']
    assert 'href="/inventory" aria-current="page">Inventory</a>' in nav
    # Mobile navigation is plain wrapped links: no hidden menu, script, or swipe-only strip.
    assert ".sidebar nav{display:grid;grid-template-columns:repeat(4,minmax(0,1fr))" in APP_CSS
    assert "<details" not in nav


def test_sign_out_remains_a_post_form():
    base = (WEB / "templates" / "base.html").read_text(encoding="utf-8")
    assert re.search(
        r'<form class="sidebar-signout" method="post" action="/logout">'
        r'<button class="button secondary" type="submit">Sign out</button></form>',
        base,
    )
    assert 'href="/logout"' not in base


def test_templates_add_no_inline_script_or_event_handler():
    for template in (WEB / "templates").rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "<script" not in text.lower(), template.name
        assert not re.search(r"\son[a-z]+\s*=", text, re.I), template.name
        assert 'style="' not in text, template.name


def test_rendered_pages_have_no_inline_script_or_page_overflow_sources(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item, sale = _seed(store)

    for path in (
        "/",
        "/inventory",
        f"/inventory/{item.inventory_id}",
        "/sales",
        f"/sales/{sale.sale_id}?edit_accounting=true",
        "/deals/history",
        "/insights",
        "/analytics",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "<script" not in response.text.lower(), path
        assert not re.search(r"\son[a-z]+\s*=", response.text, re.I), path


def test_numeric_fields_hint_the_right_phone_keyboard(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item, _ = _seed(store)
    web_app.research_store.clear()
    created = client.post(
        "/deals/opportunities",
        data={
            "source": "estate-sale",
            "title": "Estate camera",
            "category": "electronics",
            "base_price": "20.25",
            "currency": "USD",
            "condition": "used_untested",
        },
        follow_redirects=False,
    )
    detail = client.get(created.headers["location"].split("?", 1)[0]).text
    new_form = client.get("/deals/opportunities/new").text
    inventory = client.get(f"/inventory/{item.inventory_id}").text

    # Integer day counts get the numeric keypad; money keeps a decimal keypad.
    assert 'name="minimum_sale_days" value="" inputmode="numeric"' in detail
    assert 'name="maximum_sale_days" value="" inputmode="numeric"' in detail
    assert 'name="expected_resale" value="" inputmode="decimal"' in detail
    assert 'name="acquisition_cost" required inputmode="decimal"' in detail
    assert 'name="base_price" required inputmode="decimal"' in new_form
    assert 'name="travel_minutes" inputmode="numeric"' in inventory
    assert 'name="fuel_cost" inputmode="decimal"' in inventory
    # Browser hints never switch money to type="number" (no float semantics in the browser).
    for page in (detail, new_form, inventory):
        assert 'type="number"' not in page
    assert 'name="currency" value="USD" required minlength="3" maxlength="3" ' in new_form
    assert 'autocapitalize="characters"' in new_form


def test_dense_tables_scroll_inside_named_focusable_regions(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    _, sale = _seed(store)

    for path, label in (
        ("/inventory", "Inventory table"),
        ("/sales", "Sales table"),
        (f"/sales/{sale.sale_id}", "Components and provenance"),
    ):
        page = client.get(path).text
        assert f'class="panel table-wrap" tabindex="0" aria-label="{label}"' in page, path
    assert '<td class="nowrap">2026-08-01</td>' in client.get("/inventory").text
    assert '<td class="nowrap">2026-09-01</td>' in client.get("/sales").text
    assert ".table-wrap{overflow-x:auto;padding:0}" in APP_CSS
    assert ".table-wrap :is(th,td):first-child{position:sticky;left:0" in APP_CSS
    assert ".table-wrap:focus-visible{outline:3px solid" in APP_CSS


def test_long_values_wrap_instead_of_widening_the_page():
    wrap_rule = re.search(r"([^{}]*)\{overflow-wrap:anywhere\}", APP_CSS.split("/* Mobile")[1])
    selectors = set(wrap_rule.group(1).split(","))
    for selector in (
        ".attachment-grid h3",
        ".economics-grid strong",
        ".note-content",
        ".deal-card h2",  # long unbroken listing titles on Deals cards and deal detail
        ".deal-hero h2",
    ):
        assert selector in selectors
    assert "main{overflow-wrap:break-word}" in APP_CSS
    assert "main :is(.two-col,.detail-grid,.deal-layout){grid-template-columns:minmax(0,1fr)}" in (
        APP_CSS
    )
    assert ".attachment-grid img{display:block;max-width:100%;height:auto" in APP_CSS


def test_destructive_actions_are_separated_and_keep_confirmation(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    item, _ = _seed(store)

    page = client.get(f"/inventory/{item.inventory_id}").text

    assert (
        f'<form class="separated-action" method="post" '
        f'action="/inventory/{item.inventory_id}/sourcing-travel/clear">'
        '<input type="hidden" name="confirm" value="1">'
        '<button class="button secondary" type="submit">Clear actual travel</button>'
    ) in page
    detail = (WEB / "templates" / "deal_detail.html").read_text(encoding="utf-8")
    remove_form = (
        '<form class="separated-action" method="post" action="{{ detail_path }}/comparables/'
    )
    assert remove_form in detail
