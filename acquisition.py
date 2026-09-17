"""Explicit conversion of an analyzed listing into durable acquired inventory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from inventory.store import InventoryRecord, InventoryStore
from models import DealEvaluation, Listing
from parser.ai_enricher import enrich_specs_with_ai
from parser.extractor import extract_specs
from pricing.estimator import calculate_pricing_result, estimate_market_value, score_deal
from utils.distance import compute_distance_miles


@dataclass(frozen=True)
class AnalysisResult:
    """One completed analyzer result with the metadata needed for a snapshot."""

    deal: DealEvaluation
    analyzed_at: datetime
    currency: str = "USD"
    pricing_method: str = "component-estimator"
    ai_summary: str = ""


def analyze_listing(
    listing: Listing,
    *,
    home_lat: float,
    home_lon: float,
    analyzed_at: datetime | None = None,
) -> AnalysisResult:
    """Run the existing parsing, enrichment, pricing, and scoring pipeline once."""
    base_specs = extract_specs(listing.title, listing.description)
    specs, ai_summary = enrich_specs_with_ai(
        title=listing.title,
        description=listing.description,
        base_specs=base_specs,
    )
    distance_miles = compute_distance_miles(
        home_lat=home_lat,
        home_lon=home_lon,
        listing_lat=listing.latitude,
        listing_lon=listing.longitude,
    )
    estimated_value = estimate_market_value(specs)
    pricing = calculate_pricing_result(estimated_value, listing.price)
    score, profit = score_deal(listing.price, estimated_value, specs, distance_miles)
    return AnalysisResult(
        deal=DealEvaluation(
            listing=listing,
            specs=specs,
            distance_miles=distance_miles,
            estimated_market_value=estimated_value,
            asking_price=listing.price,
            ideal_buy_price=pricing.ideal_buy_price,
            expected_resale_value=pricing.expected_resale_value,
            estimated_gross_profit=profit,
            estimated_roi=pricing.estimated_roi,
            score=score,
        ),
        analyzed_at=analyzed_at or datetime.now(timezone.utc),
        ai_summary=ai_summary,
    )


def acquire_from_analysis(
    store: InventoryStore,
    analysis: AnalysisResult,
    *,
    acquisition_cost: str | Decimal,
    acquired_at: str,
    acquisition_source: str | None = None,
    notes: str = "",
) -> InventoryRecord:
    """Record an explicit acquisition and its baseline estimate in one transaction."""
    deal = analysis.deal
    listing = deal.listing
    roi = Decimal(str(deal.estimated_roi)) if deal.estimated_roi is not None else None
    return store.acquire_with_valuation(
        title=listing.title,
        source=acquisition_source or listing.source,
        acquired_at=acquired_at,
        acquisition_cost=acquisition_cost,
        notes=notes,
        marketplace=listing.source,
        marketplace_item_id=listing.listing_id,
        analyzed_at=analysis.analyzed_at,
        currency=analysis.currency,
        estimated_market_value=Decimal(str(deal.estimated_market_value)),
        expected_resale_value=Decimal(str(deal.expected_resale_value)),
        asking_price=Decimal(str(deal.asking_price)),
        ideal_buy_price=Decimal(str(deal.ideal_buy_price)),
        estimated_gross_profit=Decimal(str(deal.estimated_gross_profit)),
        estimated_roi=roi,
        deal_score=deal.score,
        pricing_method=analysis.pricing_method,
    )
