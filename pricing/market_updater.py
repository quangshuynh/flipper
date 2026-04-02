"""
Update part prices from eBay Browse API and store them locally.

Required environment variables:
    EBAY_ENV=production            # or: sandbox
    EBAY_CLIENT_ID=...
    EBAY_CLIENT_SECRET=...
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from statistics import median
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


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


def get_ebay_environment() -> tuple[str, str]:
    """
    Returns (token_url, browse_url) for the configured environment.
    """
    env = os.getenv("EBAY_ENV", "production").strip().lower()

    if env == "sandbox":
        return (
            "https://api.sandbox.ebay.com/identity/v1/oauth2/token",
            "https://api.sandbox.ebay.com/buy/browse/v1/item_summary/search",
        )

    return (
        "https://api.ebay.com/identity/v1/oauth2/token",
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
    )


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
    Fetch a fresh eBay OAuth application token using client credentials.
    """
    client_id = os.getenv("EBAY_CLIENT_ID", "").strip()
    client_secret = os.getenv("EBAY_CLIENT_SECRET", "").strip()
    token_url, _ = get_ebay_environment()

    if not client_id or not client_secret:
        raise ValueError(
            "EBAY_CLIENT_ID or EBAY_CLIENT_SECRET missing from environment variables."
        )

    # Safe debug output
    print(f"Using token endpoint: {token_url}")
    print(f"Client ID prefix: {client_id[:12]}... (len={len(client_id)})")
    print(f"Client Secret length: {len(client_secret)}")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }

    response = requests.post(
        token_url,
        headers=headers,
        data=data,
        auth=HTTPBasicAuth(client_id, client_secret),
        timeout=20,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Token request failed.\n"
            f"Status: {response.status_code}\n"
            f"Response: {response.text}\n\n"
            "Check these:\n"
            "1. EBAY_ENV matches your keys (sandbox vs production)\n"
            "2. EBAY_CLIENT_ID is the App ID\n"
            "3. EBAY_CLIENT_SECRET is the Cert ID / Client Secret\n"
            "4. No extra spaces or quotes are in your .env"
        )

    payload = response.json()
    token = payload.get("access_token")

    if not token:
        raise RuntimeError(f"No access_token in token response: {response.text}")

    print(f"Access token acquired. Prefix: {token[:20]}...")
    return token


def search_ebay(query: str, token: str, limit: int = 20) -> list[dict[str, Any]]:
    _, browse_url = get_ebay_environment()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }

    params = {
        "q": query,
        "limit": limit,
        "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW|USED}",
    }

    response = requests.get(
        browse_url,
        headers=headers,
        params=params,
        timeout=20,
    )

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