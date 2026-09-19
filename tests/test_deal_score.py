from datetime import date
from decimal import Decimal

import pytest

from deals.comparables import Comparable, ComparableCondition, ComparableEvidenceSet, ComparableType
from deals.economics import calculate_economics
from deals.models import CostComponent, Money
from deals.scoring import (
    DealScore,
    DealScoreConfidence,
    calculate_deal_score,
    present_deal_score,
    present_frozen_deal_score,
)
from main import main


def evidence(*prices):
    return ComparableEvidenceSet(
        tuple(
            Comparable(
                ComparableType.SOLD,
                "Research",
                Money.of(price),
                date(2026, 9, 19),
                ComparableCondition.USED_TESTED,
                date(2026, 9, 18),
            )
            for price in prices
        ),
        as_of=date(2026, 9, 19),
    )


def economics(base, resale, *, travel="0", fees="0", repair=None):
    return calculate_economics(
        base_price=Money.of(base),
        pickup_travel_cost=CostComponent.estimated(travel),
        other_acquisition_cost=(
            CostComponent.estimated(repair)
            if repair is not None
            else CostComponent.not_applicable()
        ),
        expected_resale=CostComponent.estimated(resale),
        selling_fees=CostComponent.estimated(fees),
    )


def score(base, resale, prices, **kwargs):
    return calculate_deal_score(
        asking_price=Money.of(base),
        economics=economics(base, resale, **kwargs),
        evidence=evidence(*prices),
        condition_known=True,
    )


def test_calibration_ordering_and_bounds_are_not_reverse_engineered_exact_values():
    a = score("50", "250", [240, 250, 250, 260, 250], fees="25")
    b = score("225", "250", [240, 250, 250, 260, 250], fees="25")
    c = score("300", "225", [215, 220, 225, 230, 235], fees="20")
    assert Decimal(1) <= c.value < b.value < a.value <= Decimal(10)
    assert a.label == "Exceptional potential"


def test_price_above_and_far_below_comps_are_explainable_but_cheap_is_not_a_scam_claim():
    poor = score("300", "250", [240, 250, 260], fees="25")
    cheap = score("1", "250", [240, 250, 260], fees="25")
    assert poor.components[0].assessment == "strong negative"
    assert cheap.components[0].assessment == "strong positive"
    assert all("scam" not in part.explanation.lower() for part in cheap.components)


def test_sparse_dispersed_and_strong_comps_change_confidence_not_score_formula():
    sparse = score("50", "250", [250])
    dispersed = score("50", "250", [100, 250, 400, 250, 250])
    strong = score("50", "250", [245, 248, 250, 252, 255])
    assert sparse.confidence is DealScoreConfidence.LOW
    assert dispersed.confidence is DealScoreConfidence.MEDIUM
    assert strong.confidence is DealScoreConfidence.HIGH
    assert sparse.value == strong.value


def test_unknowns_are_unavailable_while_known_zero_costs_are_complete():
    missing_ask = calculate_deal_score(
        asking_price=None, economics=economics("50", "250"), evidence=evidence(250)
    )
    missing_comps = calculate_deal_score(
        asking_price=Money.of("50"), economics=economics("50", "250"), evidence=evidence()
    )
    incomplete = calculate_deal_score(
        asking_price=Money.of("50"),
        economics=calculate_economics(base_price=Money.of("50")),
        evidence=evidence(250),
    )
    known_zero = score("50", "250", [250], travel="0", fees="0")
    assert not missing_ask.available and not missing_comps.available and not incomplete.available
    assert known_zero.available


def test_travel_and_explicit_repair_cost_reduce_score_once_and_repeatability_is_exact():
    baseline = score("50", "250", [250, 250, 250], travel="0")
    travel = score("50", "250", [250, 250, 250], travel="50")
    repair = score("50", "250", [250, 250, 250], repair="75")
    repeated = score("50", "250", [250, 250, 250], travel="0")
    assert travel.value < baseline.value
    assert repair.value < baseline.value
    assert repeated == baseline


def test_high_score_can_have_low_or_high_confidence():
    low = score("40", "250", [250])
    high = score("40", "250", [245, 248, 250, 252, 255])
    assert low.value == high.value and low.value >= Decimal("9")
    assert low.confidence is DealScoreConfidence.LOW
    assert high.confidence is DealScoreConfidence.HIGH


def test_cli_uses_authoritative_score_and_explains_unavailable(capsys):
    common = [
        "deals",
        "analyze",
        "--title",
        "CLI deal",
        "--source",
        "local",
        "--category",
        "electronics",
        "--base-price",
        "50",
        "--expected-resale",
        "250",
    ]
    assert main(common) == 0
    assert "Deal Score: Unavailable" in capsys.readouterr().out
    assert main(common + ["--sold-comparable", "250", "--condition", "Used"]) == 0
    output = capsys.readouterr().out
    assert "Deal Score:" in output and "/ 10" in output
    assert "Confidence: Low" in output
    assert "Price vs. sold comparables" in output


def test_card_presentations_project_authoritative_results_without_rescoring():
    unavailable = calculate_deal_score(
        asking_price=Money.of("50"), economics=economics("50", "250"), evidence=evidence()
    )
    card = present_deal_score(unavailable)
    assert not card.available
    assert card.value is None
    assert card.unavailable_reason == "Needs sold comps"

    available = score("50", "250", [245, 248, 250, 252, 255])
    frozen = present_frozen_deal_score(
        {
            "derived": {
                "deal_score": {
                    "version": available.version,
                    "value": str(available.value),
                    "label": available.label,
                    "confidence": available.confidence.value,
                    "unavailable_reasons": [],
                }
            }
        }
    )
    assert frozen.available and frozen.frozen
    assert frozen.label == "Exceptional potential"
    assert frozen.confidence == "High"


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (
            ("sold comparable evidence in the asking-price currency is unavailable",),
            "Needs sold comps",
        ),
        (("modeled economics are incomplete",), "Needs assumptions"),
        (("landed cost and expected net proceeds are required",), "Needs cost estimate"),
        (("expected net proceeds must be greater than zero",), "Needs resale estimate"),
        (
            (
                "sold comparable evidence in the asking-price currency is unavailable",
                "modeled economics are incomplete",
            ),
            "Needs evaluation",
        ),
    ],
)
def test_card_unavailable_copy_is_concise_and_requirement_specific(reasons, expected):
    result = DealScore("deal-score-v1", None, None, None, unavailable_reasons=reasons)
    assert present_deal_score(result).unavailable_reason == expected
