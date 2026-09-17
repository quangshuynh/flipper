"""Exact, explainable expected deal-economics calculations."""

from __future__ import annotations

from deals.models import (
    AmountStatus,
    CalculationState,
    CostComponent,
    DealEconomics,
    Money,
    EvidenceProvenance,
    ProvenanceKind,
    ProfitVelocity,
    RoiState,
    TimeToSale,
)


def _sum_components(
    required: Money, named: tuple[tuple[str, CostComponent], ...]
) -> tuple[Money | None, list[str], bool]:
    reasons: list[str] = []
    mismatch = False
    total = required.amount
    for name, component in named:
        if component.status is AmountStatus.UNKNOWN:
            reasons.append(f"{name} is unknown")
        elif component.money is not None:
            if component.money.currency != required.currency:
                mismatch = True
                reasons.append(f"{name} currency does not match {required.currency}")
            else:
                total += component.money.amount
    if reasons:
        return None, reasons, mismatch
    return Money(total, required.currency), reasons, mismatch


def calculate_economics(
    *,
    base_price: Money,
    acquisition_tax: CostComponent = CostComponent.not_applicable(),
    inbound_shipping: CostComponent = CostComponent.not_applicable(),
    pickup_travel_cost: CostComponent = CostComponent.not_applicable(),
    other_acquisition_cost: CostComponent = CostComponent.not_applicable(),
    expected_resale: CostComponent = CostComponent.unknown(),
    selling_fees: CostComponent = CostComponent.not_applicable(),
    outbound_shipping: CostComponent = CostComponent.not_applicable(),
    other_selling_cost: CostComponent = CostComponent.not_applicable(),
    time_to_sale: TimeToSale | None = None,
) -> DealEconomics:
    """Calculate expected economics without converting currencies or filling unknowns."""
    if base_price.amount < 0:
        raise ValueError("base price cannot be negative")
    landed, reasons, mismatch = _sum_components(
        base_price,
        (
            ("acquisition tax", acquisition_tax),
            ("inbound shipping", inbound_shipping),
            ("pickup/travel cost", pickup_travel_cost),
            ("other acquisition cost", other_acquisition_cost),
        ),
    )
    proceeds = profit = None
    resale = expected_resale.money
    if expected_resale.status is AmountStatus.UNKNOWN:
        reasons.append("expected resale proceeds are unknown")
    elif expected_resale.status is AmountStatus.NOT_APPLICABLE:
        reasons.append("expected resale proceeds are not supplied")
    elif resale is not None:
        proceeds, selling_reasons, selling_mismatch = _sum_components(
            resale,
            (
                ("selling fees", selling_fees),
                ("outbound shipping", outbound_shipping),
                ("other selling cost", other_selling_cost),
            ),
        )
        # _sum_components adds costs; net proceeds subtracts them instead.
        if proceeds is not None:
            costs = proceeds.amount - resale.amount
            proceeds = Money(resale.amount - costs, resale.currency)
        reasons.extend(selling_reasons)
        mismatch = mismatch or selling_mismatch
    if landed is not None and proceeds is not None:
        if landed.currency != proceeds.currency:
            mismatch = True
            reasons.append("acquisition and resale currencies do not match")
        else:
            profit_amount = proceeds.amount - landed.amount
            # Profit may be negative, while Money intentionally models nonnegative amounts.
            profit = Money(profit_amount, landed.currency)

    roi = None
    roi_state = RoiState.UNAVAILABLE
    velocity = None
    if profit is not None and landed is not None:
        if landed.amount == 0:
            roi_state = (
                RoiState.ZERO_COST_POSITIVE_PROFIT
                if profit.amount > 0
                else RoiState.ZERO_COST_NONPOSITIVE_PROFIT
            )
        else:
            roi = profit.amount / landed.amount
            roi_state = RoiState.AVAILABLE
        if time_to_sale is not None:
            velocity = ProfitVelocity(
                conservative_profit_per_day=Money(
                    profit.amount / time_to_sale.maximum_days, profit.currency
                ),
                optimistic_profit_per_day=Money(
                    profit.amount / time_to_sale.minimum_days, profit.currency
                ),
                conservative_roi_per_day=(
                    roi / time_to_sale.maximum_days if roi is not None else None
                ),
                optimistic_roi_per_day=(
                    roi / time_to_sale.minimum_days if roi is not None else None
                ),
            )
    state = (
        CalculationState.CURRENCY_MISMATCH
        if mismatch
        else CalculationState.INCOMPLETE
        if reasons
        else CalculationState.AVAILABLE
    )
    return DealEconomics(
        state=state,
        landed_cost=landed,
        expected_net_proceeds=proceeds,
        expected_net_profit=profit,
        capital_tied_up=landed,
        roi=roi,
        roi_state=roi_state,
        profit_velocity=velocity,
        unavailable_reasons=tuple(reasons),
        provenance={
            "landed_cost": EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper calculation",
                ("asking price", "tax", "inbound shipping", "travel", "other acquisition cost"),
            ),
            "expected_net_proceeds": EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper calculation",
                ("expected resale", "selling fees", "outbound shipping", "other selling costs"),
            ),
            "expected_net_profit": EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper calculation",
                ("landed cost", "expected net proceeds"),
            ),
            "roi": EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper calculation",
                ("expected net profit", "landed cost"),
            ),
            "profit_velocity": EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper calculation",
                ("expected net profit", "time-to-sale range"),
            ),
        },
    )
