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
