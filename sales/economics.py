"""Deterministic sale economics, independent of persistence and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EconomicComponent:
    """A nonnegative amount with an explicit effect on sale proceeds."""

    category: str
    amount: Decimal
    currency: str
    effect: str = "reduce"


@dataclass(frozen=True)
class SaleEconomics:
    """A calculation based on the economic components recorded locally."""

    gross: Decimal
    acquisition_cost: Decimal
    sourcing_travel_cost: Decimal
    components_by_category: dict[str, Decimal]
    increasing_by_category: dict[str, Decimal]
    recorded_profit: Decimal
    currency: str


def calculate_sale_economics(
    *,
    gross: Decimal,
    acquisition_cost: Decimal,
    currency: str,
    acquisition_currency: str,
    components: list[EconomicComponent],
    sourcing_travel_cost: Decimal = Decimal(0),
) -> SaleEconomics:
    """Calculate recorded profit without conversion or completeness claims.

    Amounts are nonnegative; effect carries the sign to avoid double negatives.
    """
    if currency != acquisition_currency:
        raise ValueError(
            f"sale currency {currency} does not match acquisition currency "
            f"{acquisition_currency}; currency conversion is not supported"
        )
    totals: dict[str, Decimal] = {}
    increases: dict[str, Decimal] = {}
    for component in components:
        if component.currency != currency:
            raise ValueError(
                f"component currency {component.currency} does not match sale currency {currency}"
            )
        if not component.amount.is_finite() or component.amount < 0:
            raise ValueError("component amounts must be finite and nonnegative")
        if component.effect not in {"reduce", "increase"}:
            raise ValueError("component effect must be reduce or increase")
        target = totals if component.effect == "reduce" else increases
        target[component.category] = target.get(component.category, Decimal(0)) + component.amount
    if not sourcing_travel_cost.is_finite() or sourcing_travel_cost < 0:
        raise ValueError("sourcing travel cost must be finite and nonnegative")
    recorded_profit = (
        gross
        - acquisition_cost
        - sourcing_travel_cost
        - sum(totals.values(), Decimal(0))
        + sum(increases.values(), Decimal(0))
    )
    return SaleEconomics(
        gross, acquisition_cost, sourcing_travel_cost, totals, increases, recorded_profit, currency
    )
