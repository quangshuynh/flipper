"""Immutable source-neutral deal models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from deals.categories import DealCategory


def _decimal(value: Decimal | int | str, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, int, or string input; floats are not exact")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except Exception as exc:
        raise ValueError(f"{name} must be a valid decimal amount") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str = "USD"

    def __post_init__(self) -> None:
        amount = _decimal(self.amount, "money amount")
        currency = self.currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ValueError("currency must be a three-letter code")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "currency", currency)

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: str = "USD") -> Money:
        return cls(_decimal(amount, "money amount"), currency)


class AmountStatus(str, Enum):
    ACTUAL = "actual"
    ESTIMATED = "estimated"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CostComponent:
    status: AmountStatus
    money: Money | None = None

    def __post_init__(self) -> None:
        if (self.status in {AmountStatus.ACTUAL, AmountStatus.ESTIMATED}) != (
            self.money is not None
        ):
            raise ValueError(
                "actual/estimated components require money; unknown/N/A components do not"
            )
        if self.money is not None and self.money.amount < 0:
            raise ValueError("cost components cannot be negative")

    @classmethod
    def actual(cls, amount: Decimal | int | str, currency: str = "USD") -> CostComponent:
        return cls(AmountStatus.ACTUAL, Money.of(amount, currency))

    @classmethod
    def estimated(cls, amount: Decimal | int | str, currency: str = "USD") -> CostComponent:
        return cls(AmountStatus.ESTIMATED, Money.of(amount, currency))

    @classmethod
    def unknown(cls) -> CostComponent:
        return cls(AmountStatus.UNKNOWN)

    @classmethod
    def not_applicable(cls) -> CostComponent:
        return cls(AmountStatus.NOT_APPLICABLE)


class SourceIdentity(str, Enum):
    EBAY = "ebay"
    FACEBOOK_MARKETPLACE = "facebook-marketplace"
    MERCARI = "mercari"
    CRAIGSLIST = "craigslist"
    ESTATE_SALE = "estate-sale"
    LOCAL = "local"
    OTHER = "other"


@dataclass(frozen=True)
class DealOpportunity:
    source: SourceIdentity
    source_listing_id: str | None
    title: str
    category: DealCategory
    base_price: Money
    url: str | None = None
    seller_reference: str | None = None
    location_text: str | None = None
    condition: str | None = None
    observed_at: datetime | None = None
    normalized_attributes: Mapping[str, str] = field(default_factory=dict)
    category_attributes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("opportunity title is required")
        if self.base_price.amount < 0:
            raise ValueError("opportunity base price cannot be negative")
        object.__setattr__(
            self, "normalized_attributes", MappingProxyType(dict(self.normalized_attributes))
        )
        object.__setattr__(
            self, "category_attributes", MappingProxyType(dict(self.category_attributes))
        )


@dataclass(frozen=True)
class TimeToSale:
    minimum_days: int
    maximum_days: int
    source: str

    def __post_init__(self) -> None:
        if self.minimum_days <= 0 or self.maximum_days < self.minimum_days:
            raise ValueError("time-to-sale must be a positive ordered range")
        if not self.source.strip():
            raise ValueError("time-to-sale source is required")


class EvidenceLevel(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


@dataclass(frozen=True)
class LiquidityEvidence:
    recent_sold_count: int | None = None
    active_comparable_count: int | None = None
    sell_through_ratio: Decimal | None = None
    typical_sold_days: TimeToSale | None = None
    price_dispersion: Decimal | None = None
    comp_recency_days: int | None = None
    comp_quality: EvidenceLevel | None = None

    def __post_init__(self) -> None:
        for name in ("recent_sold_count", "active_comparable_count", "comp_recency_days"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        for name in ("sell_through_ratio", "price_dispersion"):
            value = getattr(self, name)
            if value is not None:
                normalized = _decimal(value, name)
                if normalized < 0:
                    raise ValueError(f"{name} cannot be negative")
                object.__setattr__(self, name, normalized)


@dataclass(frozen=True)
class ConfidenceEvidence:
    pricing: EvidenceLevel | None = None
    time_to_sale: EvidenceLevel | None = None
    condition: EvidenceLevel | None = None
    category_match: EvidenceLevel | None = None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RiskFactor:
    code: str
    explanation: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.explanation.strip():
            raise ValueError("risk code and explanation are required")


class CalculationState(str, Enum):
    AVAILABLE = "available"
    INCOMPLETE = "incomplete"
    CURRENCY_MISMATCH = "currency_mismatch"


class RoiState(str, Enum):
    AVAILABLE = "available"
    ZERO_COST_POSITIVE_PROFIT = "zero_cost_positive_profit"
    ZERO_COST_NONPOSITIVE_PROFIT = "zero_cost_nonpositive_profit"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ProfitVelocity:
    conservative_profit_per_day: Money
    optimistic_profit_per_day: Money
    conservative_roi_per_day: Decimal | None
    optimistic_roi_per_day: Decimal | None


@dataclass(frozen=True)
class DealEconomics:
    state: CalculationState
    landed_cost: Money | None
    expected_net_proceeds: Money | None
    expected_net_profit: Money | None
    capital_tied_up: Money | None
    roi: Decimal | None
    roi_state: RoiState
    profit_velocity: ProfitVelocity | None
    unavailable_reasons: tuple[str, ...]
