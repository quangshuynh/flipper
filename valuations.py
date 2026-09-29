"""Exact, deterministic valuation snapshot and estimate-vs-actual calculations."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ValuationComparison:
    """A historical baseline estimate compared with one durable sale result."""

    estimated_resale: Decimal
    actual_gross: Decimal
    resale_error: Decimal
    absolute_resale_error: Decimal
    resale_percentage_error: Decimal | None
    estimated_profit: Decimal | None
    recorded_actual_profit: Decimal | None
    fully_reconciled_actual_profit: Decimal | None
    profit_error: Decimal | None
    profit_comparison_basis: str | None
    currency: str


def compare_valuation_to_sale(
    *,
    estimated_resale: Decimal,
    estimated_profit: Decimal | None,
    estimate_currency: str,
    actual_gross: Decimal,
    sale_currency: str,
    recorded_actual_profit: Decimal | None,
    fully_reconciled: bool,
) -> ValuationComparison | None:
    """Compare compatible exact amounts without currency conversion."""
    if estimate_currency != sale_currency:
        return None
    error = actual_gross - estimated_resale
    percentage = error / estimated_resale if estimated_resale > 0 else None
    fully_reconciled_profit = recorded_actual_profit if fully_reconciled else None
    return ValuationComparison(
        estimated_resale=estimated_resale,
        actual_gross=actual_gross,
        resale_error=error,
        absolute_resale_error=abs(error),
        resale_percentage_error=percentage,
        estimated_profit=estimated_profit,
        recorded_actual_profit=recorded_actual_profit,
        fully_reconciled_actual_profit=fully_reconciled_profit,
        profit_error=None,
        profit_comparison_basis=None,
        currency=estimate_currency,
    )
