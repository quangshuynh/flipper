from datetime import datetime, timezone
from decimal import Decimal

import pytest

from acquisition import analyze_listing
from deals.categories import DealCategory, normalize_category
from deals.economics import calculate_economics
from deals.evaluation import ComparisonResult, DealEvaluation, compare
from deals.models import (
    AmountStatus,
    CalculationState,
    ConfidenceEvidence,
    CostComponent,
    DealOpportunity,
    EvidenceLevel,
    LiquidityEvidence,
    Money,
    RiskFactor,
    RoiState,
    SourceIdentity,
    TimeToSale,
)
from deals.pc import adapt_pc_deal
from main import main
from models import Listing


def test_categories_have_stable_unique_slugs_and_normalize_labels():
    assert len({category.slug for category in DealCategory}) == 18
    assert normalize_category("Collectibles & Art") is DealCategory.COLLECTIBLES_ART
    assert normalize_category("gift-cards-coupons") is DealCategory.GIFT_CARDS_COUPONS
    assert normalize_category("PC") is DealCategory.ELECTRONICS
    with pytest.raises(ValueError, match="unknown"):
        normalize_category("imaginary category")


def test_source_neutral_opportunity_preserves_unknowns_and_attributes():
    opportunity = DealOpportunity(
        source=SourceIdentity.LOCAL,
        source_listing_id=None,
        title="Workshop tool",
        category=DealCategory.BUSINESS_INDUSTRIAL,
        base_price=Money.of("25.00"),
        observed_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        normalized_attributes={"tested": "no"},
    )
    assert opportunity.url is None
    assert opportunity.condition is None
    assert opportunity.normalized_attributes["tested"] == "no"


def test_money_rejects_binary_float_and_keeps_decimal_exact():
    assert Money.of("0.10").amount + Money.of("0.20").amount == Decimal("0.30")
    with pytest.raises(TypeError, match="floats"):
        Money.of(0.1)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "100.00"),
        ({"acquisition_tax": CostComponent.estimated("8.00")}, "108.00"),
        ({"inbound_shipping": CostComponent.actual("12.50")}, "112.50"),
        ({"pickup_travel_cost": CostComponent.estimated("6.25")}, "106.25"),
    ],
)
def test_landed_cost_components(kwargs, expected):
    result = calculate_economics(base_price=Money.of("100.00"), **kwargs)
    assert result.landed_cost == Money.of(expected)
    assert result.capital_tied_up == result.landed_cost


def test_explicit_unknown_cost_keeps_landed_cost_unknown():
    result = calculate_economics(
        base_price=Money.of("100"), inbound_shipping=CostComponent.unknown()
    )
    assert result.landed_cost is None
    assert result.state is CalculationState.INCOMPLETE
    assert "inbound shipping is unknown" in result.unavailable_reasons


def test_net_proceeds_profit_and_roi_are_exact():
    result = calculate_economics(
        base_price=Money.of("100"),
        acquisition_tax=CostComponent.estimated("8"),
        expected_resale=CostComponent.estimated("180"),
        selling_fees=CostComponent.estimated("18"),
        outbound_shipping=CostComponent.estimated("12"),
    )
    assert result.expected_net_proceeds == Money.of("150")
    assert result.expected_net_profit == Money.of("42")
    assert result.roi == Decimal("42") / Decimal("108")
    assert result.roi_state is RoiState.AVAILABLE


def test_zero_cost_has_explicit_non_numeric_roi():
    result = calculate_economics(
        base_price=Money.of("0"), expected_resale=CostComponent.estimated("40")
    )
    assert result.roi is None
    assert result.roi_state is RoiState.ZERO_COST_POSITIVE_PROFIT


def test_currency_mismatch_prevents_combination():
    result = calculate_economics(
        base_price=Money.of("50", "USD"),
        expected_resale=CostComponent.estimated("80", "CAD"),
    )
    assert result.state is CalculationState.CURRENCY_MISMATCH
    assert result.expected_net_profit is None


def test_time_range_produces_velocity_range_without_false_precision():
    result = calculate_economics(
        base_price=Money.of("55"),
        expected_resale=CostComponent.estimated("95"),
        time_to_sale=TimeToSale(4, 7, "manual estimate"),
    )
    assert result.profit_velocity is not None
    assert result.profit_velocity.conservative_profit_per_day.amount == Decimal("40") / 7
    assert result.profit_velocity.optimistic_profit_per_day.amount == Decimal("10")


def test_unknown_time_does_not_fabricate_velocity():
    result = calculate_economics(
        base_price=Money.of("55"), expected_resale=CostComponent.estimated("95")
    )
    assert result.profit_velocity is None


def test_liquidity_confidence_and_risk_are_structured_and_optional():
    liquidity = LiquidityEvidence(
        recent_sold_count=8,
        active_comparable_count=4,
        sell_through_ratio=Decimal("2"),
        typical_sold_days=TimeToSale(3, 7, "sold comps"),
        comp_quality=EvidenceLevel.MODERATE,
    )
    confidence = ConfidenceEvidence(pricing=EvidenceLevel.MODERATE)
    evaluation = DealEvaluation(
        calculate_economics(base_price=Money.of("20")),
        liquidity=liquidity,
        confidence=confidence,
        risks=(RiskFactor("incomplete-testing", "Functionality is not fully tested."),),
    )
    assert evaluation.confidence.time_to_sale is None
    assert evaluation.liquidity.sell_through_ratio == Decimal("2")
    assert evaluation.risks[0].code == "incomplete-testing"


def test_comparison_preserves_high_profit_slow_vs_lower_profit_fast_tradeoff():
    slow_time = TimeToSale(30, 45, "manual")
    fast_time = TimeToSale(4, 7, "manual")
    slow = DealEvaluation(
        calculate_economics(
            base_price=Money.of("250"),
            expected_resale=CostComponent.estimated("330"),
            time_to_sale=slow_time,
        ),
        time_to_sale=slow_time,
    )
    fast = DealEvaluation(
        calculate_economics(
            base_price=Money.of("55"),
            expected_resale=CostComponent.estimated("95"),
            time_to_sale=fast_time,
        ),
        time_to_sale=fast_time,
    )
    result = compare(slow, fast)
    assert result.result is ComparisonResult.TRADEOFF
    assert "expected net profit" in result.left_advantages
    assert {
        "ROI",
        "capital tied up",
        "maximum time-to-sale",
        "conservative profit velocity",
    } <= set(result.right_advantages)


def test_existing_pc_analyzer_adapts_without_changing_legacy_score():
    listing = Listing("pc-1", "local", "Gaming PC RTX 3060", "Ryzen 5, 16GB RAM", 300, "")
    legacy = analyze_listing(listing, home_lat=0, home_lon=0)
    opportunity, economics = adapt_pc_deal(legacy.deal)
    assert opportunity.category is DealCategory.ELECTRONICS
    assert opportunity.category_attributes["gpu"] != "not listed"
    assert economics.expected_net_profit.amount == Decimal(str(legacy.deal.estimated_gross_profit))
    assert legacy.deal.score >= 0


def test_component_status_requires_amount_only_when_known():
    assert CostComponent.unknown().status is AmountStatus.UNKNOWN
    with pytest.raises(ValueError):
        CostComponent(AmountStatus.ESTIMATED)


def test_generalized_cli_prints_ephemeral_economics(capsys):
    assert (
        main(
            [
                "deals",
                "analyze",
                "--title",
                "Fast flip",
                "--source",
                "local",
                "--category",
                "electronics",
                "--base-price",
                "55",
                "--expected-resale",
                "95",
                "--minimum-sale-days",
                "4",
                "--maximum-sale-days",
                "7",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "Expected net profit: USD 40.00" in output
    assert "Expected profit/day: USD 5.71 to USD 10.00" in output
