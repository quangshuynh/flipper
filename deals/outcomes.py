"""Descriptive comparison of saved deal research with authoritative outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from inventory.store import InventoryRecord, InventoryStore, ResearchSnapshotRecord
from reports.service import SaleReportRow, inventory_report, sales_report


@dataclass(frozen=True)
class OutcomeMetric:
    expected: Decimal | None
    actual: Decimal | None
    difference: Decimal | None
    currency: str | None = None


@dataclass(frozen=True)
class DecisionOutcome:
    state: str
    inventory: InventoryRecord | None
    sale: SaleReportRow | None
    acquisition_cost: OutcomeMetric
    resale: OutcomeMetric
    profit: OutcomeMetric
    roi: OutcomeMetric
    expected_minimum_days: int | None
    expected_maximum_days: int | None
    actual_holding_days: int | None
    holding_range_position: str | None


def _decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _money(payload: dict, name: str) -> tuple[Decimal | None, str | None]:
    value = payload.get("derived", {}).get(name)
    if not isinstance(value, dict):
        return None, None
    currency = value.get("currency")
    amount = _decimal(value.get("amount"))
    return (amount, currency) if isinstance(currency, str) else (None, None)


def _assumption_money(payload: dict, name: str) -> tuple[Decimal | None, str | None]:
    component = payload.get("assumptions", {}).get(name)
    if not isinstance(component, dict):
        return None, None
    money = component.get("money")
    if not isinstance(money, dict):
        return None, None
    currency = money.get("currency")
    amount = _decimal(money.get("amount"))
    return (amount, currency) if isinstance(currency, str) else (None, None)


def _metric(expected, actual, *, currency=None) -> OutcomeMetric:
    difference = actual - expected if expected is not None and actual is not None else None
    return OutcomeMetric(expected, actual, difference, currency)


def build_decision_outcome(
    store: InventoryStore, snapshot: ResearchSnapshotRecord, *, today: date
) -> DecisionOutcome:
    """Build a read-only outcome from captured expectations and current local records."""
    payload = snapshot.payload or {}
    expected_landed, landed_currency = _money(payload, "landed_cost")
    expected_resale, resale_currency = _assumption_money(payload, "expected_resale")
    expected_profit, profit_currency = _money(payload, "expected_net_profit")
    landed_currency = landed_currency or snapshot.currency
    resale_currency = resale_currency or snapshot.currency
    profit_currency = profit_currency or snapshot.currency
    expected_roi = _decimal(payload.get("derived", {}).get("roi"))
    time_range = payload.get("time_to_sale")
    minimum_days = time_range.get("minimum_days") if isinstance(time_range, dict) else None
    maximum_days = time_range.get("maximum_days") if isinstance(time_range, dict) else None
    empty = dict(
        acquisition_cost=_metric(expected_landed, None, currency=landed_currency),
        resale=_metric(expected_resale, None, currency=resale_currency),
        profit=_metric(expected_profit, None, currency=profit_currency),
        roi=_metric(expected_roi, None),
        expected_minimum_days=minimum_days,
        expected_maximum_days=maximum_days,
        actual_holding_days=None,
        holding_range_position=None,
    )
    if not snapshot.inventory_id:
        return DecisionOutcome("not_linked", None, None, **empty)

    inventory = store.get(snapshot.inventory_id)
    all_sales = store.list_sales()
    held = inventory_report([inventory], all_sales, today=today).rows[0].days_held
    matching_sales = [s for s in all_sales if s.inventory_internal_id == inventory.internal_id]
    sale_rows = sales_report(
        [inventory],
        matching_sales,
        store.list_all_sale_costs(),
        store.list_reconciliation_confirmations(),
    ).rows
    sale = sale_rows[0] if sale_rows else None
    acquisition_actual = inventory.acquisition_cost if landed_currency == "USD" else None
    resale_actual = (
        sale.sale.gross_amount if sale and resale_currency == sale.sale.currency else None
    )
    profit_actual = (
        sale.economics.recorded_profit
        if sale and sale.economics and profit_currency == sale.sale.currency
        else None
    )
    roi_actual = None
    if sale and sale.economics and inventory.acquisition_cost != 0:
        roi_actual = sale.economics.recorded_profit / inventory.acquisition_cost
    range_position = None
    if sale and held is not None and minimum_days is not None and maximum_days is not None:
        range_position = (
            "below range"
            if held < minimum_days
            else "above range"
            if held > maximum_days
            else "within range"
        )
    return DecisionOutcome(
        "realized" if sale else "in_progress",
        inventory,
        sale,
        _metric(expected_landed, acquisition_actual, currency=landed_currency),
        _metric(expected_resale, resale_actual, currency=resale_currency),
        _metric(expected_profit, profit_actual, currency=profit_currency),
        _metric(expected_roi, roi_actual),
        minimum_days,
        maximum_days,
        held if sale else None,
        range_position,
    )
