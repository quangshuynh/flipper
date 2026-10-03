import json

from collectors.json_feed_collector import fetch_listings


def test_json_collector_parses_supported_shapes(tmp_path):
    """
    parse supported JSON feed shapes and price formats
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(
        json.dumps({"listings": [{"id": "1", "price": "$1,200", "title": "PC"}]}), encoding="utf-8"
    )

    listings = fetch_listings(path)

    assert listings[0].listing_id == "1"
    assert listings[0].price == 1200


def test_json_collector_skips_invalid_prices(tmp_path):
    """
    skip feed entries with invalid prices
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(
        json.dumps([{"id": "bad", "price": "unknown"}, {"id": "good", "price": 300}]),
        encoding="utf-8",
    )

    listings = fetch_listings(path)

    assert [listing.listing_id for listing in listings] == ["good"]


def test_json_collector_rejects_object_without_listings_key(tmp_path):
    """
    a top-level object missing the 'listings' key must fail loudly instead of
    silently producing an empty feed
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(json.dumps({"items": []}), encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="listings"):
        fetch_listings(path)


def test_json_collector_rejects_non_list_listings_value(tmp_path):
    """
    a top-level object whose 'listings' value is not a list must be rejected
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(json.dumps({"listings": {"nested": []}}), encoding="utf-8")

    import pytest

    with pytest.raises(ValueError, match="list"):
        fetch_listings(path)


def test_json_collector_accepts_empty_listings_object(tmp_path):
    """
    `{"listings": []}` remains a valid empty feed
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(json.dumps({"listings": []}), encoding="utf-8")

    listings = fetch_listings(path)

    assert listings == []


def test_json_collector_accepts_top_level_list(tmp_path):
    """
    a top-level list remains supported unchanged
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(json.dumps([{"id": "1", "price": 100, "title": "Item"}]), encoding="utf-8")

    listings = fetch_listings(path)

    assert [listing.listing_id for listing in listings] == ["1"]
