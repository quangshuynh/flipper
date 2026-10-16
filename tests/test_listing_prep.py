"""Quick Listing Prep: drafts are user-authored notes, never marketplace or accounting facts."""

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from inventory.listing_prep import (
    default_title,
    description_template,
    estimate_proceeds,
    pricing_evidence,
)
from inventory.store import InventoryStore, ListingDraftValidationError
from tests.web_client import local_client
from web.app import app


def _store(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    store.initialize()
    return store


def _item(store, **overrides):
    values = {
        "title": "Dell OptiPlex 7050 Micro i5-7500T 8GB 128GB",
        "source": "Estate sale",
        "acquired_at": "2026-09-02",
        "acquisition_cost": "35.00",
        "notes": "Paid cash to a neighbor; private detail",
    }
    values.update(overrides)
    return store.add(**values)


def test_migration_adds_only_the_draft_table_and_preserves_existing_rows(tmp_path):
    store = _store(tmp_path)
    item = _item(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE listing_drafts")
        connection.execute("DELETE FROM schema_migrations WHERE version = 15")

    store.initialize()

    assert store.get(item.inventory_id) == item
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 15"
        ).fetchone() == (15,)
        assert connection.execute("SELECT COUNT(*) FROM listing_drafts").fetchone() == (0,)
    assert store.get_listing_draft(item.inventory_id) is None


def test_draft_round_trips_exact_cents_and_keeps_unknown_distinct_from_zero(tmp_path):
    store = _store(tmp_path)
    item = _item(store)

    draft = store.save_listing_draft(
        item.inventory_id,
        title="  Dell   OptiPlex 7050 Micro  ",
        description="Line one\r\nLine two",
        condition="used",
        defects="Scuff on lid",
        asking_price="119.99",
        minimum_offer="95.00",
        fee_rate_percent="13.25",
        fixed_fee="0",
        shipping_cost="",
    )

    assert draft.title == "Dell OptiPlex 7050 Micro"
    assert draft.description == "Line one\nLine two"
    assert draft.asking_price_cents == 11999
    assert draft.minimum_offer_cents == 9500
    assert draft.fee_rate_bps == 1325
    assert draft.fixed_fee_cents == 0  # explicit zero is known
    assert draft.shipping_cost_cents is None  # blank stays unknown
    assert store.get_listing_draft(item.inventory_id) == draft


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"title": "x" * 81}, "80 characters"),
        ({"asking_price": "50.00", "minimum_offer": "50.01"}, "cannot exceed"),
        ({"condition": "like_new"}, "recognized"),
        ({"fee_rate_percent": "100.01"}, "between 0 and 100"),
        ({"fee_rate_percent": "13.255"}, "two decimal places"),
        ({"asking_price": "-1"}, "nonnegative"),
        ({"asking_price": "1.001"}, "two decimal places"),
        ({"shipping_cost": "abc"}, "USD amount"),
        ({"description": "x" * 5001}, "5000 characters"),
    ],
)
def test_invalid_drafts_are_rejected_without_writing(tmp_path, overrides, message):
    store = _store(tmp_path)
    item = _item(store)

    with pytest.raises(ListingDraftValidationError, match=message):
        store.save_listing_draft(item.inventory_id, **overrides)

    assert store.get_listing_draft(item.inventory_id) is None


@pytest.mark.parametrize("status", ["sold", "archived"])
def test_drafts_are_only_editable_for_active_items(tmp_path, status):
    store = _store(tmp_path)
    item = _item(store, marketplace="eBay", marketplace_sku="Q0001")
    if status == "sold":
        store.transition_status(item.inventory_id, "listed")
        store.transition_status(item.inventory_id, "sold")
    else:
        store.transition_status(item.inventory_id, "archived")

    with pytest.raises(ListingDraftValidationError, match="acquired or listed"):
        store.save_listing_draft(item.inventory_id, title="Anything")


def test_saving_a_draft_never_changes_authoritative_records(tmp_path):
    store = _store(tmp_path)
    item = store.acquire_with_valuation(
        title="Lenovo ThinkPad T480",
        source="Goodwill",
        acquired_at="2026-08-14",
        acquisition_cost="42.00",
        analyzed_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
        currency="USD",
        estimated_market_value=Decimal("185"),
        expected_resale_value=Decimal("175"),
        asking_price=Decimal("189.99"),
        ideal_buy_price=Decimal("60"),
        estimated_gross_profit=Decimal("108.50"),
        estimated_roi=Decimal("2.58"),
        deal_score=8,
    )
    valuations = store.list_valuation_snapshots(item.inventory_id)

    store.save_listing_draft(item.inventory_id, title="T480", asking_price="150.00")

    assert store.get(item.inventory_id) == item
    assert store.list_valuation_snapshots(item.inventory_id) == valuations
    assert store.list_sales() == []


def test_proceeds_estimate_uses_exact_cents_and_reports_missing_inputs(tmp_path):
    store = _store(tmp_path)
    item = _item(store)
    draft = store.save_listing_draft(
        item.inventory_id,
        asking_price="119.99",
        fee_rate_percent="13.25",
        fixed_fee="0.30",
        shipping_cost="12.00",
    )

    estimate = estimate_proceeds(draft, item.acquisition_cost_cents)

    # 11999 * 13.25% = 1589.8675 cents, rounded half-up to 1590, plus the 30-cent fixed fee.
    assert estimate.fees_cents == 1620
    assert estimate.proceeds_cents == 11999 - 1620 - 1200
    assert estimate.net_after_cost_cents == 11999 - 1620 - 1200 - 3500
    assert estimate.missing == ()

    partial = store.save_listing_draft(item.inventory_id, asking_price="119.99")
    unknown = estimate_proceeds(partial, item.acquisition_cost_cents)
    assert (unknown.fees_cents, unknown.proceeds_cents, unknown.net_after_cost_cents) == (
        None,
        None,
        None,
    )
    assert unknown.missing == ("fee rate", "fixed fee", "shipping")
    assert estimate_proceeds(None, 3500).proceeds_cents is None


def test_unknown_acquisition_cost_leaves_net_unavailable(tmp_path):
    store = _store(tmp_path)
    item = _item(store)
    draft = store.save_listing_draft(
        item.inventory_id,
        asking_price="10.00",
        fee_rate_percent="0",
        fixed_fee="0",
        shipping_cost="0",
    )

    estimate = estimate_proceeds(draft, None)

    assert estimate.proceeds_cents == 1000
    assert estimate.net_after_cost_cents is None


def test_description_template_uses_known_facts_and_visible_placeholders(tmp_path):
    store = _store(tmp_path)
    item = _item(store, title="Dell   OptiPlex " + "x" * 90)

    text = description_template(item, None, "", 0)

    assert text.splitlines()[0] == default_title(item)
    assert len(default_title(item)) <= 80
    assert "Condition: [confirm before listing]" in text
    assert "Known defects: None recorded [confirm before listing]" in text
    assert "[add photos of the exact item]" in text
    # Private inventory facts never enter buyer-facing text.
    assert "Paid cash" not in text and "Estate sale" not in text and "35.00" not in text

    chosen = description_template(item, "for_parts", "No power", 3)
    assert "Condition: For parts or not working" in chosen
    assert "Known defects: No power" in chosen
    assert "See 3 photos for the exact item." in chosen


def test_pricing_evidence_lists_only_existing_dated_research(tmp_path):
    store = _store(tmp_path)
    item = _item(store)

    assert pricing_evidence(store.baseline_valuation(item.inventory_id), []) == []


def _web(monkeypatch, tmp_path):
    database = tmp_path / "web.db"
    monkeypatch.setenv("FLIPPER_INVENTORY_DB", str(database))
    store = InventoryStore(database)
    store.initialize()
    return local_client(app), store


def test_inventory_detail_links_to_listing_prep_for_active_items(monkeypatch, tmp_path):
    client, store = _web(monkeypatch, tmp_path)
    item = _item(store)

    detail = client.get(f"/inventory/{item.inventory_id}").text

    assert f'href="/inventory/{item.inventory_id}/listing">Prepare listing</a>' in detail
    assert f'href="/inventory/{item.inventory_id}/listing"' in client.get("/inventory").text


def test_listing_page_states_limits_and_never_invents_a_price(monkeypatch, tmp_path):
    client, store = _web(monkeypatch, tmp_path)
    item = _item(store)

    response = client.get(f"/inventory/{item.inventory_id}/listing")

    assert response.status_code == 200
    page = response.text
    assert "No pricing evidence on record." in page
    assert "Flipper does not publish to eBay" in page
    assert "Flipper never infers condition" in page
    assert '<option value="">Choose a condition</option>' in page
    assert 'name="asking_price" value="" inputmode="decimal"' in page
    assert 'type="number"' not in page
    assert "<script" not in page.lower()
    assert '<textarea name="copy_title" readonly' in page
    assert "Paid cash" not in page.split('name="description"')[1].split("</textarea>")[0]
    assert client.get("/inventory/Q9999/listing").status_code == 404


def test_saving_and_rebuilding_through_the_web(monkeypatch, tmp_path):
    client, store = _web(monkeypatch, tmp_path)
    item = _item(store)
    path = f"/inventory/{item.inventory_id}/listing"
    form = {
        "title": "Dell OptiPlex 7050 Micro",
        "description": "Tested to BIOS.",
        "condition": "used",
        "defects": "Small scuff",
        "asking_price": "119.99",
        "minimum_offer": "",
        "fee_rate_percent": "13.25",
        "fixed_fee": "0.30",
        "shipping_cost": "12.00",
    }

    rebuilt = client.post(path, data={**form, "action": "template"})
    assert rebuilt.status_code == 200
    assert "Condition: Used" in rebuilt.text and "Known defects: Small scuff" in rebuilt.text
    assert store.get_listing_draft(item.inventory_id) is None  # rebuilding never saves

    saved = client.post(path, data={**form, "action": "save"}, follow_redirects=False)
    assert saved.status_code == 303
    assert store.get_listing_draft(item.inventory_id).asking_price_cents == 11999
    page = client.get(saved.headers["location"]).text
    assert "Listing draft saved." in page
    assert "USD 91.79" in page  # 119.99 - 16.20 fees - 12.00 shipping
    assert store.get(item.inventory_id) == item


def test_invalid_web_submission_keeps_the_typed_values(monkeypatch, tmp_path):
    client, store = _web(monkeypatch, tmp_path)
    item = _item(store)

    response = client.post(
        f"/inventory/{item.inventory_id}/listing",
        data={"title": "Keep this title", "asking_price": "10.00", "minimum_offer": "20.00"},
    )

    assert response.status_code == 400
    assert "minimum acceptable offer cannot exceed the asking price" in response.text
    assert 'value="Keep this title"' in response.text
    assert store.get_listing_draft(item.inventory_id) is None


def test_inactive_items_render_read_only(monkeypatch, tmp_path):
    client, store = _web(monkeypatch, tmp_path)
    item = _item(store)
    store.transition_status(item.inventory_id, "archived")

    page = client.get(f"/inventory/{item.inventory_id}/listing").text

    assert "Read-only." in page
    assert 'action="/inventory/' not in page.split("Listing draft")[1].split("</section>")[0]
