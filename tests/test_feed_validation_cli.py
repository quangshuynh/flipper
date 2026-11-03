"""Malformed JSON feeds fail with actionable errors and leave local databases untouched."""

import json

import pytest

import collectors.json_feed_collector as json_feed_collector
import main
import utils.dedupe as dedupe
from inventory.store import InventoryStore

MALFORMED_FEEDS = {
    "missing_listings_key": json.dumps({"items": []}),
    "empty_object": "{}",
    "listings_not_a_list": json.dumps({"listings": {"id": "1"}}),
    "scalar_top_level": json.dumps("listings"),
    "invalid_json": '{"listings": [',
}


@pytest.fixture
def isolated_scan(monkeypatch, tmp_path):
    """Point the default scan at temporary feed and dedupe paths."""
    feed = tmp_path / "listings.json"
    seen_db = tmp_path / "seen.db"
    monkeypatch.setattr(dedupe, "DB_PATH", seen_db)
    monkeypatch.setattr(main, "fetch_listings", lambda: json_feed_collector.fetch_listings(feed))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    return feed, seen_db


@pytest.mark.parametrize("name", sorted(MALFORMED_FEEDS))
def test_default_scan_reports_malformed_feed_without_traceback(isolated_scan, capsys, name):
    feed, seen_db = isolated_scan
    feed.write_text(MALFORMED_FEEDS[name], encoding="utf-8")

    assert main.main([]) == 1

    captured = capsys.readouterr()
    assert captured.err.startswith("Error: JSON feed ")
    assert str(feed) in captured.err
    assert "Traceback" not in captured.err
    assert not seen_db.exists(), "a rejected feed must not create or write the dedupe database"


def test_default_scan_reports_missing_feed(isolated_scan, capsys):
    feed, seen_db = isolated_scan

    assert main.main([]) == 1

    assert "JSON feed not found" in capsys.readouterr().err
    assert not seen_db.exists()


def test_default_scan_accepts_valid_empty_object_feed(isolated_scan, capsys):
    feed, _seen_db = isolated_scan
    feed.write_text(json.dumps({"listings": []}), encoding="utf-8")

    assert main.main([]) == 0

    assert "Fetched 0 listings." in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(MALFORMED_FEEDS))
def test_acquire_rejects_malformed_feed_without_inventory_writes(tmp_path, capsys, name):
    feed = tmp_path / "feed.json"
    feed.write_text(MALFORMED_FEEDS[name], encoding="utf-8")
    database = tmp_path / "inventory.db"

    result = main.main(
        [
            "acquire",
            "--feed",
            str(feed),
            "--listing-id",
            "export-123",
            "--cost",
            "10.00",
            "--acquired-at",
            "2026-11-01",
            "--database",
            str(database),
        ]
    )

    assert result == 1
    assert str(feed) in capsys.readouterr().err
    if database.exists():
        assert InventoryStore(database).list() == []
