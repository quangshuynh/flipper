import json
from datetime import datetime, timezone
from decimal import Decimal

import main
import pytest

from acquisition import AnalysisResult, acquire_from_analysis
from inventory.store import InventoryStore, InventoryValidationError
from models import DealEvaluation, Listing, ParsedSpecs


ANALYZED_AT = datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc)


def _analysis() -> AnalysisResult:
    listing = Listing(
        listing_id="export-123",
        source="synthetic-market",
        title="Synthetic RTX computer",
        description="RTX 3060, Ryzen 5 5600X, 16GB RAM",
        price=300.0,
        url="",
    )
    return AnalysisResult(
        deal=DealEvaluation(
            listing=listing,
            specs=ParsedSpecs(gpu="RTX 3060", cpu="Ryzen 5 5600X", ram="16 GB RAM"),
            distance_miles=None,
            estimated_market_value=500.0,
            asking_price=300.0,
            ideal_buy_price=340.0,
            expected_resale_value=475.0,
            estimated_gross_profit=175.0,
            estimated_roi=0.5833,
            score=72,
        ),
        analyzed_at=ANALYZED_AT,
    )


def test_analysis_or_declining_acquisition_creates_nothing(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    analysis = _analysis()

    assert analysis.deal.score == 72
    assert store.list() == []
    assert store.list_valuation_snapshots() == []


def test_explicit_acquisition_creates_q_number_and_exact_baseline(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    item = acquire_from_analysis(
        store,
        _analysis(),
        acquisition_cost="241.37",
        acquired_at="2026-09-16",
        acquisition_source="estate sale",
    )

    assert item.inventory_id == "Q0001"
    assert item.acquisition_cost_cents == 24137
    assert item.acquired_at == "2026-09-16"
    assert item.source == "estate sale"
    assert item.acquisition_cost != Decimal("300")
    assert item.marketplace == "synthetic-market"
    assert item.marketplace_item_id == "export-123"
    baseline = store.baseline_valuation(item.inventory_id)
    assert baseline is not None
    assert baseline.is_baseline is True
    assert baseline.analyzed_at == "2026-09-15T14:30:00Z"
    assert baseline.currency == "USD"
    assert baseline.estimated_market_value == Decimal("500")
    assert baseline.expected_resale_value == Decimal("475")
    assert baseline.asking_price == Decimal("300")
    assert baseline.ideal_buy_price == Decimal("340")
    assert baseline.estimated_gross_profit == Decimal("175")
    assert baseline.estimated_roi == Decimal("0.5833")
    assert baseline.deal_score == 72
    assert baseline.pricing_method == "component-estimator"
    assert not hasattr(baseline, "confidence")
    assert not hasattr(baseline, "fee_estimate")


def test_invalid_inventory_input_creates_neither_record(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    with pytest.raises(InventoryValidationError):
        acquire_from_analysis(
            store,
            _analysis(),
            acquisition_cost="300.001",
            acquired_at="2026-09-16",
        )
    assert store.list() == []
    assert store.list_valuation_snapshots() == []


def test_valuation_insert_failure_rolls_back_item_and_q_sequence(monkeypatch, tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic valuation failure")

    monkeypatch.setattr(store, "_insert_valuation_snapshot", fail)
    with pytest.raises(RuntimeError, match="synthetic valuation failure"):
        acquire_from_analysis(
            store,
            _analysis(),
            acquisition_cost="200.00",
            acquired_at="2026-09-16",
        )
    assert store.list() == []
    assert store.list_valuation_snapshots() == []

    clean_store = InventoryStore(store.path)
    item = clean_store.add(
        title="Later item",
        source="synthetic",
        acquired_at="2026-09-16",
        acquisition_cost="1.00",
    )
    assert item.inventory_id == "Q0001"


def test_retry_is_explicitly_not_idempotent_and_reanalysis_appends(tmp_path):
    store = InventoryStore(tmp_path / "inventory.db")
    first = acquire_from_analysis(
        store, _analysis(), acquisition_cost="200", acquired_at="2026-09-16"
    )
    second = acquire_from_analysis(
        store, _analysis(), acquisition_cost="200", acquired_at="2026-09-16"
    )
    assert [first.inventory_id, second.inventory_id] == ["Q0001", "Q0002"]

    later = store.add_valuation_snapshot(
        first.inventory_id,
        analyzed_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        currency="USD",
        estimated_market_value=Decimal("480"),
        expected_resale_value=Decimal("450"),
        asking_price=Decimal("300"),
        ideal_buy_price=Decimal("325"),
        estimated_gross_profit=Decimal("150"),
        estimated_roi=Decimal("0.5"),
        deal_score=68,
        pricing_method="component-estimator",
    )
    snapshots = store.list_valuation_snapshots(first.inventory_id)
    assert [snapshot.is_baseline for snapshot in snapshots] == [True, False]
    assert later.expected_resale_value == Decimal("450")


def test_cli_requires_explicit_command_and_reports_q_number(monkeypatch, tmp_path, capsys):
    feed = tmp_path / "listings.json"
    feed.write_text(
        json.dumps(
            [
                {
                    "id": "export-123",
                    "source": "synthetic-market",
                    "title": "Synthetic RTX computer",
                    "description": "RTX 3060 computer",
                    "price": 300,
                }
            ]
        ),
        encoding="utf-8",
    )
    database = tmp_path / "inventory.db"
    monkeypatch.setattr(main, "analyze_listing", lambda *_args, **_kwargs: _analysis())

    assert (
        main.main(
            [
                "acquire",
                "--feed",
                str(feed),
                "--listing-id",
                "export-123",
                "--cost",
                "241.37",
                "--acquired-at",
                "2026-09-16",
                "--database",
                str(database),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Acquired Q0001" in output
    assert "Actual acquisition cost: $241.37" in output
    store = InventoryStore(database)
    assert store.get("Q0001").acquisition_cost_cents == 24137
    assert store.baseline_valuation("Q0001") is not None
