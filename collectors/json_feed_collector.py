"""
JSON feed collector for Flipper.

Reads listing data from a local JSON file and converts each item into a Listing.
Useful for:
- exported marketplace data
- mock API responses
- manually curated deal feeds
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List, Optional

from models import Listing


DEFAULT_JSON_PATH = Path("data/listings.json")
logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    """
    Convert a value to float when possible.

    :param value: Raw input value.
    :returns: Float or None.
    """
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_price(raw_price: Any) -> Optional[float]:
    """
    Parse a price value into a float.

    Supports:
    - 450
    - "450"
    - "$450"
    - "$450.00"

    :param raw_price: Raw price value.
    :returns: Parsed float price.
    """
    if raw_price is None:
        return None

    if isinstance(raw_price, (int, float)):
        return float(raw_price)

    cleaned = str(raw_price).strip().replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _build_listing(item: dict[str, Any], index: int) -> Listing:
    """
    Convert a raw JSON object into a Listing.

    Expected flexible keys:
    - id or listing_id
    - source
    - title
    - description
    - price
    - url or link
    - latitude / lat
    - longitude / lon / lng
    - location_text / location / city

    :param item: Raw JSON item.
    :param index: Fallback index for generated IDs.
    :returns: Listing object.
    """
    listing_id = str(item.get("listing_id") or item.get("id") or f"json_{index}")

    source = str(item.get("source") or "json_feed")
    title = str(item.get("title") or "Untitled Listing")
    description = str(item.get("description") or "")
    price = _parse_price(item.get("price"))
    if price is None or price < 0:
        raise ValueError("listing price must be a non-negative number")
    url = str(item.get("url") or item.get("link") or "")

    latitude = _safe_float(item.get("latitude", item.get("lat")))
    longitude = _safe_float(item.get("longitude", item.get("lon", item.get("lng"))))

    location_text = str(item.get("location_text") or item.get("location") or item.get("city") or "")

    return Listing(
        listing_id=listing_id,
        source=source,
        title=title,
        description=description,
        price=price,
        url=url,
        latitude=latitude,
        longitude=longitude,
        location_text=location_text,
    )


_EXPECTED_SHAPE = 'a top-level list of listings or an object like {"listings": [...]}'
_MAX_REPORTED_KEYS = 10


def _json_type_name(value: Any) -> str:
    """
    Name a decoded JSON value by its JSON type.

    :param value: Decoded JSON value.
    :returns: JSON type name.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _feed_entries(raw: Any, path: Path) -> list[Any]:
    """
    Validate the top-level feed shape and return its raw listing entries.

    :param raw: Decoded JSON document.
    :param path: Feed path, named in errors.
    :returns: Raw listing entries.
    :raises ValueError: When the document is not a supported feed shape.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        raise ValueError(
            f"JSON feed {path} must contain {_EXPECTED_SHAPE}; got {_json_type_name(raw)}"
        )
    if "listings" not in raw:
        keys = sorted(raw)
        if not keys:
            found = "found an empty object"
        else:
            shown = ", ".join(keys[:_MAX_REPORTED_KEYS])
            more = (
                f" and {len(keys) - _MAX_REPORTED_KEYS} more"
                if len(keys) > _MAX_REPORTED_KEYS
                else ""
            )
            found = f"found keys: {shown}{more}"
        raise ValueError(
            f"JSON feed {path} is an object without a 'listings' key ({found}); "
            f"expected {_EXPECTED_SHAPE}"
        )
    listings = raw["listings"]
    if not isinstance(listings, list):
        raise ValueError(
            f"JSON feed {path} 'listings' is {_json_type_name(listings)}; "
            'expected a list of listing objects, like {"listings": [...]}'
        )
    return listings


def fetch_listings(json_path: str | Path = DEFAULT_JSON_PATH) -> List[Listing]:
    """
    Read listings from a JSON file.

    Accepts either:
    1. A top-level list of listing objects
    2. A dict with a "listings" key containing a list

    Example:
    [
      {
        "id": "fb_123",
        "source": "facebook",
        "title": "Gaming PC RTX 3070",
        "description": "RTX 3070, Ryzen 5 5600X, 16GB RAM, 1TB SSD",
        "price": 650,
        "url": "https://example.com/listing/123",
        "latitude": 43.1566,
        "longitude": -77.6088,
        "location_text": "Rochester, NY"
      }
    ]

    A top-level object without a "listings" key, a non-list "listings" value, any other
    top-level JSON type, and unparseable JSON are rejected with a ValueError naming the file.
    ``{"listings": []}`` and ``[]`` are valid empty feeds. Individual entries that are not
    objects or have invalid prices are skipped as before.

    :param json_path: Path to JSON file.
    :returns: List of Listing objects.
    :raises FileNotFoundError: When the feed file does not exist.
    :raises ValueError: When the feed is not valid JSON or not a supported shape.
    """
    path = Path(json_path)

    if not path.exists():
        raise FileNotFoundError(f"JSON feed not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        try:
            raw = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"JSON feed {path} is not valid JSON: {exc.msg} "
                f"(line {exc.lineno}, column {exc.colno})"
            ) from exc
        except UnicodeDecodeError as exc:
            raise ValueError(f"JSON feed {path} is not UTF-8 text: {exc.reason}") from exc

    raw_listings = _feed_entries(raw, path)

    listings: List[Listing] = []
    for i, item in enumerate(raw_listings):
        if not isinstance(item, dict):
            continue
        try:
            listings.append(_build_listing(item, i))
        except ValueError as exc:
            logger.warning("Skipping malformed listing %s: %s", i, exc)

    return listings
