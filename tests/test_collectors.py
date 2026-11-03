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


def test_json_collector_reports_empty_object_clearly(tmp_path):
    """
    an empty top-level object names the problem instead of an empty key list
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    import pytest

    path = tmp_path / "listings.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError) as error:
        fetch_listings(path)

    message = str(error.value)
    assert "empty object" in message
    assert "got keys: )" not in message
    assert str(path) in message
    assert '{"listings": [...]}' in message


def test_json_collector_missing_key_error_lists_found_keys_and_expected_shape(tmp_path):
    """
    a missing-key error lists the keys that were found and the supported shapes
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    import pytest

    path = tmp_path / "listings.json"
    path.write_text(json.dumps({"items": [], "data": []}), encoding="utf-8")

    with pytest.raises(ValueError) as error:
        fetch_listings(path)

    message = str(error.value)
    assert "'listings'" in message
    assert "data, items" in message
    assert "top-level list" in message


def test_json_collector_rejects_null_listings_with_json_type_name(tmp_path):
    """
    a null or scalar 'listings' value is rejected with its JSON type
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    import pytest

    for value, type_name in ((None, "null"), ("x", "string"), (3, "number"), ({}, "object")):
        path = tmp_path / "listings.json"
        path.write_text(json.dumps({"listings": value}), encoding="utf-8")
        with pytest.raises(ValueError, match=f"'listings' is {type_name}; expected a list"):
            fetch_listings(path)


def test_json_collector_rejects_scalar_top_level(tmp_path):
    """
    a top-level string, number, or null is rejected with the supported shapes
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    import pytest

    for value, type_name in (("x", "string"), (5, "number"), (None, "null"), (True, "boolean")):
        path = tmp_path / "listings.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError, match=f"got {type_name}"):
            fetch_listings(path)


def test_json_collector_reports_invalid_json_with_path_and_position(tmp_path):
    """
    unparseable JSON names the file and the position of the syntax error
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    import pytest

    path = tmp_path / "listings.json"
    path.write_text('{"listings": [', encoding="utf-8")

    with pytest.raises(ValueError) as error:
        fetch_listings(path)

    message = str(error.value)
    assert str(path) in message
    assert "not valid JSON" in message
    assert "line 1" in message


def test_json_collector_accepts_empty_top_level_list(tmp_path):
    """
    `[]` remains a valid empty feed
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text("[]", encoding="utf-8")

    assert fetch_listings(path) == []


def test_json_collector_preserves_entry_parsing_for_valid_object_feed(tmp_path):
    """
    shape validation leaves per-entry parsing and skipping unchanged
    :param tmp_path: pytest temporary directory fixture
    :returns: None
    """
    path = tmp_path / "listings.json"
    path.write_text(
        json.dumps(
            {
                "listings": [
                    "not an object",
                    {"id": "bad", "price": "unknown"},
                    {"listing_id": "ok", "price": "$450.00", "lat": "43.1", "lng": -77.6},
                ],
                "exported_at": "2026-11-01",
            }
        ),
        encoding="utf-8",
    )

    listings = fetch_listings(path)

    assert [listing.listing_id for listing in listings] == ["ok"]
    assert listings[0].price == 450.0
    assert listings[0].latitude == 43.1
    assert listings[0].longitude == -77.6
