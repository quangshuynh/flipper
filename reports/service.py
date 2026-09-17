"""Deterministic reporting calculations, independent of CLI presentation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from statistics import median

from inventory.store import (
    RECONCILIATION_CATEGORIES,
    InventoryRecord,
    InventoryStore,
    SaleCostRecord,
    SaleRecord,
    ValuationSnapshot,
)
from sales.economics import EconomicComponent, SaleEconomics, calculate_sale_economics
from valuations import ValuationComparison, compare_valuation_to_sale

ACTIVE_INVENTORY_STATUSES = frozenset({"acquired", "listed"})


@dataclass(frozen=True)
class InventoryReportRow:
    record: InventoryRecord
    linked_sale_id: str | None
    days_held: int | None
    listed_age_days: int | None


@dataclass(frozen=True)
class InventoryReport:
    rows: tuple[InventoryReportRow, ...]
    status_counts: dict[str, int]
    active_count: int
    active_capital_usd: Decimal
    average_active_acquisition_cost_usd: Decimal | None
    average_listed_age_days: Decimal | None
    oldest_active: InventoryReportRow | None


@dataclass(frozen=True)
class SaleReportRow:
    sale: SaleRecord
    acquisition_cost_usd: Decimal
    reducing_costs: Decimal
    increasing_credits: Decimal
    economics: SaleEconomics | None
    margin: Decimal | None
    reconciliation_state: str
    missing_categories: tuple[str, ...]
    days_held: int | None


@dataclass(frozen=True)
class SalesReport:
    rows: tuple[SaleReportRow, ...]
    gross_by_currency: dict[str, Decimal]
    acquisition_cost_usd: Decimal
    reducing_by_currency: dict[str, Decimal]
    increasing_by_currency: dict[str, Decimal]
    recorded_profit_by_currency: dict[str, Decimal]
    fully_reconciled_profit_by_currency: dict[str, Decimal]
    incomplete_recorded_profit_by_currency: dict[str, Decimal]
    aggregate_margin_by_currency: dict[str, Decimal | None]
    reconciliation_counts: dict[str, int]
    average_days_held: Decimal | None
    median_days_held: Decimal | None


@dataclass(frozen=True)
class SummaryReport:
    inventory: InventoryReport
    sales: SalesReport


@dataclass(frozen=True)
class ValuationReportRow:
    inventory: InventoryRecord
    baseline: ValuationSnapshot
    sale: SaleRecord | None
    reconciliation_state: str | None
    comparison: ValuationComparison | None


@dataclass(frozen=True)
class ValuationAccuracyReport:
    rows: tuple[ValuationReportRow, ...]
    comparable_count: int
    average_absolute_resale_error: Decimal | None
    median_resale_error: Decimal | None
    average_percentage_error: Decimal | None
    aggregate_currency: str | None


def _utc_date(value: str) -> date:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).date()


def _days_between(start: date, end: date) -> int | None:
    days = (end - start).days
    return days if days >= 0 else None


def _in_date_range(value: date, start: date | None, end: date | None) -> bool:
    return (start is None or value >= start) and (end is None or value <= end)


def _mean(values: list[int | Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum((Decimal(value) for value in values), Decimal(0)) / len(values)


def inventory_report(
    inventory: list[InventoryRecord],
    sales: list[SaleRecord],
    *,
    today: date,
    start: date | None = None,
    end: date | None = None,
) -> InventoryReport:
    """Build a current inventory report, optionally filtered by acquisition date."""
    sale_by_inventory = {sale.inventory_internal_id: sale.sale_id for sale in sales}
    selected = [
        record
        for record in inventory
        if _in_date_range(date.fromisoformat(record.acquired_at), start, end)
    ]
    rows: list[InventoryReportRow] = []
    for record in selected:
        acquired = date.fromisoformat(record.acquired_at)
        held_end = _utc_date(record.sold_at) if record.sold_at else today
        days_held = (
            _days_between(acquired, held_end)
            if record.status in ACTIVE_INVENTORY_STATUSES or record.status == "sold"
            else None
        )
        listed_age = (
            _days_between(_utc_date(record.listed_at), today)
            if record.status == "listed" and record.listed_at
            else None
        )
        rows.append(
            InventoryReportRow(
                record=record,
                linked_sale_id=sale_by_inventory.get(record.internal_id),
                days_held=days_held,
                listed_age_days=listed_age,
            )
        )

    active_rows = [row for row in rows if row.record.status in ACTIVE_INVENTORY_STATUSES]
    active_capital = sum((row.record.acquisition_cost for row in active_rows), Decimal(0))
    listed_ages = [row.listed_age_days for row in rows if row.listed_age_days is not None]
    oldest_active = min(
        active_rows,
        key=lambda row: (row.record.acquired_at, row.record.internal_id),
        default=None,
    )
    return InventoryReport(
        rows=tuple(rows),
        status_counts=dict(Counter(row.record.status for row in rows)),
        active_count=len(active_rows),
        active_capital_usd=active_capital,
        average_active_acquisition_cost_usd=(
            active_capital / len(active_rows) if active_rows else None
        ),
        average_listed_age_days=_mean(listed_ages),
        oldest_active=oldest_active,
    )


def _reconciliation_state(
    sale_internal_id: int, confirmations: dict[int, frozenset[str]]
) -> tuple[str, tuple[str, ...]]:
    confirmed = confirmations.get(sale_internal_id, frozenset())
    missing = tuple(category for category in RECONCILIATION_CATEGORIES if category not in confirmed)
    state = (
        "fully_reconciled"
        if not missing
        else ("partially_reconciled" if confirmed else "incomplete")
    )
    return state, missing


def sales_report(
    inventory: list[InventoryRecord],
    sales: list[SaleRecord],
    costs: list[SaleCostRecord],
    confirmations: dict[int, frozenset[str]],
    *,
    start: date | None = None,
    end: date | None = None,
) -> SalesReport:
    """Build sale-level and aggregate economics using sale sold_at date filtering."""
    inventory_by_id = {record.internal_id: record for record in inventory}
    costs_by_sale: dict[int, list[SaleCostRecord]] = defaultdict(list)
    for cost in costs:
        costs_by_sale[cost.sale_internal_id].append(cost)
    selected = [sale for sale in sales if _in_date_range(_utc_date(sale.sold_at), start, end)]

    rows: list[SaleReportRow] = []
    gross: dict[str, Decimal] = defaultdict(Decimal)
    reducing: dict[str, Decimal] = defaultdict(Decimal)
    increasing: dict[str, Decimal] = defaultdict(Decimal)
    recorded_profit: dict[str, Decimal] = defaultdict(Decimal)
    fully_reconciled_profit: dict[str, Decimal] = defaultdict(Decimal)
    incomplete_recorded_profit: dict[str, Decimal] = defaultdict(Decimal)
    reconciliation_counts = Counter(
        {"incomplete": 0, "partially_reconciled": 0, "fully_reconciled": 0}
    )
    held_days: list[int] = []
    acquisition_cost = Decimal(0)

    for sale in selected:
        item = inventory_by_id[sale.inventory_internal_id]
        components = costs_by_sale.get(sale.internal_id, [])
        reducing_total = sum(
            (component.amount for component in components if component.effect == "reduce"),
            Decimal(0),
        )
        increasing_total = sum(
            (component.amount for component in components if component.effect == "increase"),
            Decimal(0),
        )
        state, missing = _reconciliation_state(sale.internal_id, confirmations)
        reconciliation_counts[state] += 1
        gross[sale.currency] += sale.gross_amount
        reducing[sale.currency] += reducing_total
        increasing[sale.currency] += increasing_total
        acquisition_cost += item.acquisition_cost
        economics = None
        margin = None
        if sale.currency == "USD":
            economics = calculate_sale_economics(
                gross=sale.gross_amount,
                acquisition_cost=item.acquisition_cost,
                currency=sale.currency,
                acquisition_currency="USD",
                components=[
                    EconomicComponent(
                        component.category,
                        component.amount,
                        component.currency,
                        component.effect,
                    )
                    for component in components
                ],
            )
            recorded_profit[sale.currency] += economics.recorded_profit
            target = (
                fully_reconciled_profit
                if state == "fully_reconciled"
                else incomplete_recorded_profit
            )
            target[sale.currency] += economics.recorded_profit
            if sale.gross_amount > 0:
                margin = economics.recorded_profit / sale.gross_amount
        days_held = _days_between(date.fromisoformat(item.acquired_at), _utc_date(sale.sold_at))
        if days_held is not None:
            held_days.append(days_held)
        rows.append(
            SaleReportRow(
                sale=sale,
                acquisition_cost_usd=item.acquisition_cost,
                reducing_costs=reducing_total,
                increasing_credits=increasing_total,
                economics=economics,
                margin=margin,
                reconciliation_state=state,
                missing_categories=missing,
                days_held=days_held,
            )
        )

    margins: dict[str, Decimal | None] = {}
    for currency, profit in recorded_profit.items():
        margins[currency] = profit / gross[currency] if gross[currency] > 0 else None
    return SalesReport(
        rows=tuple(rows),
        gross_by_currency=dict(gross),
        acquisition_cost_usd=acquisition_cost,
        reducing_by_currency=dict(reducing),
        increasing_by_currency=dict(increasing),
        recorded_profit_by_currency=dict(recorded_profit),
        fully_reconciled_profit_by_currency=dict(fully_reconciled_profit),
        incomplete_recorded_profit_by_currency=dict(incomplete_recorded_profit),
        aggregate_margin_by_currency=margins,
        reconciliation_counts=dict(reconciliation_counts),
        average_days_held=_mean(held_days),
        median_days_held=Decimal(str(median(held_days))) if held_days else None,
    )


def build_summary_report(
    store: InventoryStore,
    *,
    today: date,
    start: date | None = None,
    end: date | None = None,
) -> SummaryReport:
    """Load reporting records in bounded bulk queries and build the business summary."""
    inventory = store.list()
    sales = store.list_sales()
    return SummaryReport(
        inventory=inventory_report(inventory, sales, today=today),
        sales=sales_report(
            inventory,
            sales,
            store.list_all_sale_costs(),
            store.list_reconciliation_confirmations(),
            start=start,
            end=end,
        ),
    )


def valuation_accuracy_report(store: InventoryStore) -> ValuationAccuracyReport:
    """Compare immutable baseline valuations with compatible durable sale results."""
    inventory = store.list()
    inventory_by_internal = {item.internal_id: item for item in inventory}
    sale_by_inventory = {sale.inventory_internal_id: sale for sale in store.list_sales()}
    costs_by_sale: dict[int, list[SaleCostRecord]] = defaultdict(list)
    for cost in store.list_all_sale_costs():
        costs_by_sale[cost.sale_internal_id].append(cost)
    confirmations = store.list_reconciliation_confirmations()
    rows: list[ValuationReportRow] = []
    for baseline in (item for item in store.list_valuation_snapshots() if item.is_baseline):
        item = inventory_by_internal[baseline.inventory_internal_id]
        sale = sale_by_inventory.get(item.internal_id)
        state = None
        comparison = None
        if sale is not None:
            state, _ = _reconciliation_state(sale.internal_id, confirmations)
            economics = None
            if sale.currency == "USD":
                components = costs_by_sale.get(sale.internal_id, [])
                economics = calculate_sale_economics(
                    gross=sale.gross_amount,
                    acquisition_cost=item.acquisition_cost,
                    currency=sale.currency,
                    acquisition_currency="USD",
                    components=[
                        EconomicComponent(c.category, c.amount, c.currency, c.effect)
                        for c in components
                    ],
                )
            comparison = compare_valuation_to_sale(
                estimated_resale=baseline.expected_resale_value,
                estimated_profit=baseline.estimated_gross_profit,
                estimate_currency=baseline.currency,
                actual_gross=sale.gross_amount,
                sale_currency=sale.currency,
                recorded_actual_profit=(economics.recorded_profit if economics else None),
                fully_reconciled=state == "fully_reconciled",
            )
        rows.append(ValuationReportRow(item, baseline, sale, state, comparison))
    comparisons = [row.comparison for row in rows if row.comparison is not None]
    percentages = [
        value.resale_percentage_error
        for value in comparisons
        if value.resale_percentage_error is not None
    ]
    currencies = {value.currency for value in comparisons}
    aggregate_currency = next(iter(currencies)) if len(currencies) == 1 else None
    return ValuationAccuracyReport(
        rows=tuple(rows),
        comparable_count=len(comparisons),
        average_absolute_resale_error=(
            sum((value.absolute_resale_error for value in comparisons), Decimal(0))
            / len(comparisons)
            if comparisons and aggregate_currency is not None
            else None
        ),
        median_resale_error=(
            Decimal(str(median([value.resale_error for value in comparisons])))
            if comparisons and aggregate_currency is not None
            else None
        ),
        average_percentage_error=(
            sum(percentages, Decimal(0)) / len(percentages) if percentages else None
        ),
        aggregate_currency=aggregate_currency,
    )
