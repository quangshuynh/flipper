"""
Update part prices from eBay Browse API and store them locally.

This version uses an already-generated OAuth access token from .env.

Required environment variables:
    EBAY_ENV=sandbox
    EBAY_OAUTH_TOKEN=your_access_token_here
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from statistics import median
from typing import Any

import requests


DB_PATH = os.path.join("data", "parts_prices.db")

PART_QUERIES = [
    "RTX 3060",
    "RTX 3060 Ti",
    "RTX 3070",
    "GTX 1660 Super",
    "Ryzen 5 5600X",
    "Ryzen 5 3600",
    "i7-10700K",
    "16GB DDR4",
    "32GB DDR4",
    "1TB NVMe SSD",
]


def get_browse_url() -> str:
    env = os.getenv("EBAY_ENV", "sandbox").strip().lower()

    if env == "sandbox":
        return "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search"

    return "https://api.ebay.com/buy/browse/v1/item_summary/search"


def init_price_table() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS part_prices (
            part_name TEXT PRIMARY KEY,
            market_price REAL NOT NULL,
            low_price REAL,
            high_price REAL,
            sample_size INTEGER NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def get_ebay_token() -> str:
    """
    Read a pre-generated OAuth access token from environment variables.
    """
    token = os.getenv("EBAY_OAUTH_TOKEN", "").strip()

    if not token:
        raise ValueError("EBAY_OAUTH_TOKEN missing from environment variables.")

    print(f"Using OAuth token prefix: {token[:24]}...")
    print(f"Token length: {len(token)}")
    return token


def search_ebay(query: str, token: str, limit: int = 20) -> list[dict[str, Any]]:
    browse_url = get_browse_url()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        "Accept": "application/json",
    }

    params = {
        "q": query,
        "limit": limit,
        # Start simple. Add filters back later if needed.
    }

    response = requests.get(
        browse_url,
        headers=headers,
        params=params,
        timeout=20,
    )

    print(f"\nSearch query: {query}")
    print(f"Request URL: {response.url}")
    print(f"Status: {response.status_code}")
    print(f"Response body: {response.text[:1000]}")

    if response.status_code != 200:
        raise RuntimeError(
            f"eBay search failed for '{query}': {response.status_code} - {response.text}"
        )

    data = response.json()
    return data.get("itemSummaries", [])


def extract_prices(items: list[dict[str, Any]]) -> list[float]:
    prices: list[float] = []

    for item in items:
        title = str(item.get("title", "")).lower()

        bad_words = ["parts", "repair", "broken", "read desc", "laptop"]
        if any(word in title for word in bad_words):
            continue

        price_info = item.get("price", {})
        value = price_info.get("value")

        try:
            prices.append(float(value))
        except (TypeError, ValueError):
            continue

    return prices


def robust_market_price(prices: list[float]) -> tuple[float, float, float, int]:
    if not prices:
        return 0.0, 0.0, 0.0, 0

    prices = sorted(prices)

    if len(prices) >= 6:
        trimmed = prices[1:-1]
    else:
        trimmed = prices

    market = round(median(trimmed), 2)
    low = round(min(trimmed), 2)
    high = round(max(trimmed), 2)

    return market, low, high, len(trimmed)


def save_price(
    part_name: str,
    market: float,
    low: float,
    high: float,
    sample_size: int,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO part_prices (
            part_name,
            market_price,
            low_price,
            high_price,
            sample_size,
            source,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(part_name) DO UPDATE SET
            market_price=excluded.market_price,
            low_price=excluded.low_price,
            high_price=excluded.high_price,
            sample_size=excluded.sample_size,
            source=excluded.source,
            updated_at=excluded.updated_at
        """,
        (
            part_name,
            market,
            low,
            high,
            sample_size,
            "ebay_browse",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def update_part_price(part_name: str, token: str) -> None:
    items = search_ebay(part_name, token=token, limit=20)
    prices = extract_prices(items)
    market, low, high, sample_size = robust_market_price(prices)

    if sample_size > 0:
        save_price(part_name, market, low, high, sample_size)
        print(
            f"{part_name}: market=${market}, low=${low}, high=${high}, n={sample_size}"
        )
    else:
        print(f"{part_name}: no usable comps found")


def run_price_refresh() -> None:
    init_price_table()
    token = get_ebay_token()

    for part in PART_QUERIES:
        try:
            update_part_price(part, token)
        except Exception as exc:
            print(f"{part}: failed - {exc}")


if __name__ == "__main__":
    run_price_refresh()