"""Deterministic sale economics, independent of persistence and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EconomicComponent:
    """A nonnegative amount that reduces sale proceeds."""

    category: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class SaleEconomics:
    """A calculation based on the economic components recorded locally."""

    gross: Decimal
    acquisition_cost: Decimal
    components_by_category: dict[str, Decimal]
    recorded_profit: Decimal
    currency: str


def calculate_sale_economics(
    *,
    gross: Decimal,
    acquisition_cost: Decimal,
    currency: str,
    acquisition_currency: str,
    components: list[EconomicComponent],
) -> SaleEconomics:
    """Calculate recorded profit without conversion or completeness claims.

    Every component category currently has reducing semantics. Amounts are
    nonnegative, avoiding ambiguous double negatives.
    """
    if currency != acquisition_currency:
        raise ValueError(
            f"sale currency {currency} does not match acquisition currency "
            f"{acquisition_currency}; currency conversion is not supported"
        )
    totals: dict[str, Decimal] = {}
    for component in components:
        if component.currency != currency:
            raise ValueError(
                f"component currency {component.currency} does not match sale currency {currency}"
            )
        if not component.amount.is_finite() or component.amount < 0:
            raise ValueError("component amounts must be finite and nonnegative")
        totals[component.category] = totals.get(component.category, Decimal(0)) + component.amount
    recorded_profit = gross - acquisition_cost - sum(totals.values(), Decimal(0))
    return SaleEconomics(gross, acquisition_cost, totals, recorded_profit, currency)
