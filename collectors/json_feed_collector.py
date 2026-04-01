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
from pathlib import Path
from typing import Any, List, Optional

from models import Listing


DEFAULT_JSON_PATH = Path("data/listings.json")


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


def _parse_price(raw_price: Any) -> float:
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
        return 0.0

    if isinstance(raw_price, (int, float)):
        return float(raw_price)

    cleaned = str(raw_price).strip().replace("$", "").replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


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
    listing_id = str(
        item.get("listing_id")
        or item.get("id")
        or f"json_{index}"
    )

    source = str(item.get("source") or "json_feed")
    title = str(item.get("title") or "Untitled Listing")
    description = str(item.get("description") or "")
    price = _parse_price(item.get("price"))
    url = str(item.get("url") or item.get("link") or "")

    latitude = _safe_float(item.get("latitude", item.get("lat")))
    longitude = _safe_float(
        item.get("longitude", item.get("lon", item.get("lng")))
    )

    location_text = str(
        item.get("location_text")
        or item.get("location")
        or item.get("city")
        or ""
    )

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

    :param json_path: Path to JSON file.
    :returns: List of Listing objects.
    """
    path = Path(json_path)

    if not path.exists():
        raise FileNotFoundError(f"JSON feed not found: {path}")

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if isinstance(raw, dict):
        raw_listings = raw.get("listings", [])
    elif isinstance(raw, list):
        raw_listings = raw
    else:
        raise ValueError("JSON file must contain a list or a dict with 'listings'.")

    listings: List[Listing] = []
    for i, item in enumerate(raw_listings):
        if not isinstance(item, dict):
            continue
        listings.append(_build_listing(item, i))

    return listings