"""Deterministic, explainable Deal Score v1.

The score measures modeled opportunity quality, not purchase suitability or realized outcome.
Unknown inputs never become zero.  Confidence describes evidence strength separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

from deals.comparables import ComparableEvidenceSet, ComparableType, EvidenceSummary
from deals.models import CalculationState, DealEconomics, Money

DEAL_SCORE_VERSION = "deal-score-v1"


class DealScoreConfidence(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    score: Decimal
    weight: Decimal
    contribution: Decimal
    assessment: str
    explanation: str


@dataclass(frozen=True)
class DealScore:
    version: str
    value: Decimal | None
    label: str | None
    confidence: DealScoreConfidence | None
    components: tuple[ScoreComponent, ...] = ()
    unavailable_reasons: tuple[str, ...] = ()
    confidence_reasons: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class DealScorePresentation:
    """Small display-only projection shared by Deal cards."""

    available: bool
    value: str | None
    label: str | None
    confidence: str | None
    unavailable_reason: str | None
    frozen: bool = False


def _concise_unavailable_reason(reasons: tuple[str, ...]) -> str | None:
    categories: set[str] = set()
    for reason in reasons:
        if "sold comparable evidence" in reason or "sold comparable median" in reason:
            categories.add("sold_comps")
        elif "asking price" in reason:
            categories.add("asking_price")
        elif "landed cost" in reason:
            categories.add("cost")
        elif "modeled economics" in reason:
            categories.add("assumptions")
        elif "expected net proceeds" in reason:
            categories.add("resale")
        else:
            categories.add("other")
    if len(categories) != 1:
        return "Needs evaluation" if categories else None
    return {
        "sold_comps": "Needs sold comps",
        "asking_price": "Needs asking price",
        "cost": "Needs cost estimate",
        "assumptions": "Needs assumptions",
        "resale": "Needs resale estimate",
        "other": "Needs evaluation",
    }[next(iter(categories))]


def present_deal_score(score: DealScore, *, frozen: bool = False) -> DealScorePresentation:
    return DealScorePresentation(
        available=score.available,
        value=str(score.value) if score.value is not None else None,
        label=score.label,
        confidence=score.confidence.value if score.confidence else None,
        unavailable_reason=_concise_unavailable_reason(score.unavailable_reasons),
        frozen=frozen,
    )


def present_frozen_deal_score(payload: dict) -> DealScorePresentation | None:
    """Read the frozen v1 projection; older or malformed payloads remain unavailable to callers."""
    score = payload.get("derived", {}).get("deal_score")
    if not isinstance(score, dict) or score.get("version") != DEAL_SCORE_VERSION:
        return None
    reasons = score.get("unavailable_reasons")
    normalized_reasons = (
        tuple(reason for reason in reasons if isinstance(reason, str))
        if isinstance(reasons, list)
        else ()
    )
    value = score.get("value")
    return DealScorePresentation(
        available=value is not None,
        value=value if isinstance(value, str) else None,
        label=score.get("label") if isinstance(score.get("label"), str) else None,
        confidence=(score.get("confidence") if isinstance(score.get("confidence"), str) else None),
        unavailable_reason=_concise_unavailable_reason(normalized_reasons),
        frozen=True,
    )


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return max(low, min(high, value))


def _linear(value: Decimal, unfavorable: Decimal, exceptional: Decimal) -> Decimal:
    position = _clamp((value - unfavorable) / (exceptional - unfavorable), Decimal(0), Decimal(1))
    return Decimal(1) + Decimal(9) * position


def _assessment(value: Decimal) -> str:
    if value >= Decimal("8.5"):
        return "strong positive"
    if value >= Decimal("6.5"):
        return "positive"
    if value >= Decimal("4.5"):
        return "neutral"
    if value >= Decimal("2.5"):
        return "negative"
    return "strong negative"


def score_label(value: Decimal) -> str:
    if value < Decimal(3):
        return "Very unfavorable"
    if value < Decimal(5):
        return "Weak"
    if value < Decimal(7):
        return "Marginal / needs investigation"
    if value < Decimal(9):
        return "Promising"
    return "Exceptional potential"


def _matching_sold_summary(
    evidence: ComparableEvidenceSet, currency: str
) -> EvidenceSummary | None:
    return next(
        (
            summary
            for summary in evidence.summaries(ComparableType.SOLD)
            if summary.currency == currency
        ),
        None,
    )


def _confidence(
    summary: EvidenceSummary, *, condition_known: bool
) -> tuple[DealScoreConfidence, tuple[str, ...]]:
    dispersion = (
        (summary.maximum.amount - summary.minimum.amount) / summary.median.amount
        if summary.median.amount > 0
        else None
    )
    points = 0
    reasons: list[str] = []
    if summary.count >= 5:
        points += 2
        reasons.append(f"{summary.count} sold comparables")
    elif summary.count >= 2:
        points += 1
        reasons.append(f"only {summary.count} sold comparables")
    else:
        reasons.append("only one sold comparable")
    if summary.count < 2:
        reasons.append("price dispersion needs at least two sold comparables")
    elif dispersion is not None and dispersion <= Decimal("0.25"):
        points += 2
        reasons.append("sold prices are tightly grouped")
    elif dispersion is not None and dispersion <= Decimal("0.75"):
        points += 1
        reasons.append("sold prices have moderate dispersion")
    else:
        reasons.append("sold-price dispersion is high or unavailable")
    if condition_known:
        points += 1
        reasons.append("source condition is recorded")
    else:
        reasons.append("source condition is unknown")
    confidence = (
        DealScoreConfidence.HIGH
        if points >= 5
        else DealScoreConfidence.MEDIUM
        if points >= 3
        else DealScoreConfidence.LOW
    )
    return confidence, tuple(reasons)


def calculate_deal_score(
    *,
    asking_price: Money | None,
    economics: DealEconomics,
    evidence: ComparableEvidenceSet,
    condition_known: bool = False,
) -> DealScore:
    """Calculate Deal Score v1 from sold evidence and complete modeled economics.

    Price-vs-comps is 45%. Economic spread is 55%, itself composed of 60% ROI and
    40% net margin. Piecewise-linear anchor ranges are documented in the user docs.
    """
    reasons: list[str] = []
    if asking_price is None:
        reasons.append("asking price is unknown")
        summary = None
    else:
        summary = _matching_sold_summary(evidence, asking_price.currency)
        if summary is None:
            reasons.append("sold comparable evidence in the asking-price currency is unavailable")
        elif summary.median.amount <= 0:
            reasons.append("sold comparable median must be greater than zero")
    if economics.state is not CalculationState.AVAILABLE:
        reasons.append("modeled economics are incomplete")
    if economics.landed_cost is None or economics.expected_net_proceeds is None:
        reasons.append("landed cost and expected net proceeds are required")
    elif economics.landed_cost.amount <= 0:
        reasons.append("landed cost must be greater than zero")
    elif economics.expected_net_proceeds.amount <= 0:
        reasons.append("expected net proceeds must be greater than zero")
    if reasons:
        return DealScore(
            DEAL_SCORE_VERSION, None, None, None, unavailable_reasons=tuple(dict.fromkeys(reasons))
        )

    assert asking_price is not None and summary is not None
    assert economics.landed_cost is not None and economics.expected_net_proceeds is not None
    assert economics.expected_net_profit is not None
    discount = (summary.median.amount - asking_price.amount) / summary.median.amount
    price_score = _linear(discount, Decimal("-0.25"), Decimal("0.75"))
    roi = economics.expected_net_profit.amount / economics.landed_cost.amount
    margin = economics.expected_net_profit.amount / economics.expected_net_proceeds.amount
    roi_score = _linear(roi, Decimal("-0.25"), Decimal(3))
    margin_score = _linear(margin, Decimal("-0.25"), Decimal("0.75"))
    economics_score = Decimal("0.60") * roi_score + Decimal("0.40") * margin_score
    price_weight = Decimal("0.45")
    economics_weight = Decimal("0.55")
    raw = price_weight * price_score + economics_weight * economics_score
    value = _clamp(raw, Decimal(1), Decimal(10)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    travel_text = (
        " Modeled travel and other recorded acquisition costs are included in landed cost."
        if economics.landed_cost.amount != asking_price.amount
        else " No additional modeled acquisition cost changes landed cost."
    )
    components = (
        ScoreComponent(
            "Price vs. sold comparables",
            price_score,
            price_weight,
            price_weight * price_score,
            _assessment(price_score),
            f"Asking price is compared with the {summary.currency} sold median "
            f"from {summary.count} comparable(s).",
        ),
        ScoreComponent(
            "Economic spread",
            economics_score,
            economics_weight,
            economics_weight * economics_score,
            _assessment(economics_score),
            "Uses existing modeled ROI and net margin; no missing cost is treated as zero."
            + travel_text,
        ),
    )
    confidence, confidence_reasons = _confidence(summary, condition_known=condition_known)
    return DealScore(
        DEAL_SCORE_VERSION,
        value,
        score_label(value),
        confidence,
        components,
        confidence_reasons=confidence_reasons,
    )


def deal_score_to_payload(score: DealScore) -> dict:
    return {
        "version": score.version,
        "value": str(score.value) if score.value is not None else None,
        "label": score.label,
        "confidence": score.confidence.value if score.confidence else None,
        "components": [
            {
                "name": item.name,
                "score": str(item.score),
                "weight": str(item.weight),
                "contribution": str(item.contribution),
                "assessment": item.assessment,
                "explanation": item.explanation,
            }
            for item in score.components
        ],
        "unavailable_reasons": list(score.unavailable_reasons),
        "confidence_reasons": list(score.confidence_reasons),
    }
