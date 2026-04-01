"""
Simple SQLite-based dedupe store for processed listings.
"""

import sqlite3
from pathlib import Path


DB_PATH = Path("flipper_seen.db")


def init_db() -> None:
    """
    Initialize dedupe database.

    :returns: None.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_listings (
            listing_id TEXT PRIMARY KEY
        )
        """
    )
    conn.commit()
    conn.close()


def has_seen(listing_id: str) -> bool:
    """
    Check whether a listing was already processed.

    :param listing_id: Listing ID.
    :returns: True if seen, else False.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM seen_listings WHERE listing_id = ?", (listing_id,))
    result = cur.fetchone()
    conn.close()
    return result is not None


def mark_seen(listing_id: str) -> None:
    """
    Mark listing as seen.

    :param listing_id: Listing ID.
    :returns: None.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO seen_listings (listing_id) VALUES (?)",
        (listing_id,)
    )
    conn.commit()
    conn.close()