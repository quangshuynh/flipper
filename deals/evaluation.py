"""Dimension-preserving evaluation and comparison."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from deals.models import (
    ConfidenceEvidence,
    DealEconomics,
    LiquidityEvidence,
    RiskFactor,
    TimeToSale,
)


@dataclass(frozen=True)
class DealEvaluation:
    economics: DealEconomics
    time_to_sale: TimeToSale | None = None
    liquidity: LiquidityEvidence | None = None
    confidence: ConfidenceEvidence = ConfidenceEvidence()
    risks: tuple[RiskFactor, ...] = ()


class ComparisonResult(str, Enum):
    LEFT_DOMINATES = "left_dominates"
    RIGHT_DOMINATES = "right_dominates"
    TRADEOFF = "tradeoff"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True)
class OpportunityComparison:
    result: ComparisonResult
    left_advantages: tuple[str, ...]
    right_advantages: tuple[str, ...]


def compare(left: DealEvaluation, right: DealEvaluation) -> OpportunityComparison:
    """Compare available dimensions; do not invent weights or force a winner."""
    left_wins: list[str] = []
    right_wins: list[str] = []

    def higher(name: str, left_value, right_value) -> None:
        if left_value is None or right_value is None or left_value == right_value:
            return
        (left_wins if left_value > right_value else right_wins).append(name)

    def lower(name: str, left_value, right_value) -> None:
        if left_value is None or right_value is None or left_value == right_value:
            return
        (left_wins if left_value < right_value else right_wins).append(name)

    def money_higher(name: str, left_money, right_money) -> None:
        if left_money is None or right_money is None or left_money.currency != right_money.currency:
            return
        higher(name, left_money.amount, right_money.amount)

    def money_lower(name: str, left_money, right_money) -> None:
        if left_money is None or right_money is None or left_money.currency != right_money.currency:
            return
        lower(name, left_money.amount, right_money.amount)

    money_higher(
        "expected net profit",
        left.economics.expected_net_profit,
        right.economics.expected_net_profit,
    )
    higher("ROI", left.economics.roi, right.economics.roi)
    money_lower(
        "capital tied up",
        left.economics.capital_tied_up,
        right.economics.capital_tied_up,
    )
    if left.time_to_sale and right.time_to_sale:
        lower(
            "maximum time-to-sale", left.time_to_sale.maximum_days, right.time_to_sale.maximum_days
        )
    left_velocity = left.economics.profit_velocity
    right_velocity = right.economics.profit_velocity
    money_higher(
        "conservative profit velocity",
        getattr(left_velocity, "conservative_profit_per_day", None),
        getattr(right_velocity, "conservative_profit_per_day", None),
    )
    lower("risk-factor count", len(left.risks), len(right.risks))
    if not left_wins and not right_wins:
        result = ComparisonResult.INSUFFICIENT_EVIDENCE
    elif left_wins and right_wins:
        result = ComparisonResult.TRADEOFF
    elif left_wins:
        result = ComparisonResult.LEFT_DOMINATES
    else:
        result = ComparisonResult.RIGHT_DOMINATES
    return OpportunityComparison(result, tuple(left_wins), tuple(right_wins))
