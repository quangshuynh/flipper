"""
Update part prices from eBay Browse API and store them locally.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from statistics import median
from typing import Any

import requests


DB_PATH = "parts_prices.db"  
EBAY_API_BASE = "https://api.ebay.com/buy/browse/v1/item_summary/search"


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


def init_price_table() -> None:
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
    Uses an app token you already generated and stored in .env.
    """
    token = os.getenv("EBAY_APP_TOKEN", "").strip()
    if not token:
        raise ValueError("EBAY_APP_TOKEN missing from environment variables.")
    return token


def search_ebay(query: str, limit: int = 20) -> list[dict[str, Any]]:
    token = get_ebay_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    params = {
        "q": query,
        "limit": limit,
        "filter": "conditions:{USED|NEW},buyingOptions:{FIXED_PRICE}",
    }

    response = requests.get(
        EBAY_API_BASE,
        headers=headers,
        params=params,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    return data.get("itemSummaries", [])


def extract_prices(items: list[dict[str, Any]]) -> list[float]:
    prices: list[float] = []

    for item in items:
        title = str(item.get("title", "")).lower()

        # crude filtering, improve this over time
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

    # Trim extremes
    if len(prices) >= 6:
        trimmed = prices[1:-1]
    else:
        trimmed = prices

    market = round(median(trimmed), 2)
    low = round(min(trimmed), 2)
    high = round(max(trimmed), 2)

    return market, low, high, len(trimmed)


def save_price(part_name: str, market: float, low: float, high: float, sample_size: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO part_prices (part_name, market_price, low_price, high_price, sample_size, source, updated_at)
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


def update_part_price(part_name: str) -> None:
    items = search_ebay(part_name, limit=20)
    prices = extract_prices(items)
    market, low, high, sample_size = robust_market_price(prices)

    if sample_size > 0:
        save_price(part_name, market, low, high, sample_size)
        print(f"{part_name}: market=${market}, low=${low}, high=${high}, n={sample_size}")
    else:
        print(f"{part_name}: no usable comps found")


def run_price_refresh() -> None:
    init_price_table()
    for part in PART_QUERIES:
        update_part_price(part)


if __name__ == "__main__":
    run_price_refresh()