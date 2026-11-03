"""Deterministic, read-only CSV projections of authoritative local inventory and sales.

Exports never write to the database. Every value comes from stored records or from the existing
report calculations in :mod:`reports.service`; nothing is estimated, defaulted, or converted.

Conventions shared by every export:

- Columns appear in the fixed order of ``INVENTORY_COLUMNS`` / ``SALES_COLUMNS``.
- An empty cell means unknown, not recorded, or not applicable. ``0.00`` is a known zero.
- Money is written in plain decimal notation from exact stored values with at least two
  fractional digits; stored precision is never rounded away and no floats are involved.
- Timestamps are UTC ``YYYY-MM-DDTHH:MM:SSZ``; dates are ``YYYY-MM-DD``.
- Output is UTF-8 with a byte-order mark (so spreadsheet apps detect Unicode), RFC 4180
  quoting, and CRLF row endings.
- Text cells starting with ``=``, ``+``, ``-``, ``@``, a tab, or a line break are prefixed with
  an apostrophe so spreadsheet apps treat them as text rather than formulas.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable

from inventory.store import (
    InventoryRecord,
    InventoryStore,
    SaleCostRecord,
    SaleRecord,
    SourcingTravelRecord,
    ValuationSnapshot,
    q_number_value,
)
from reports.service import SaleReportRow, sales_report

CSV_ENCODING = "utf-8-sig"
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")

# Sale cost categories in reconciliation-category order, with their export column names.
SALE_COST_COLUMNS = (
    ("fees", "marketplace_fee", "marketplace_fees"),
    ("shipping", "shipping_cost", "seller_shipping_cost"),
    ("refunds", "refund", "refunds"),
    ("adjustments", "other_adjustment", "other_adjustments_net"),
)


class ExportError(RuntimeError):
    """An export could not be produced; nothing was written."""


@dataclass(frozen=True)
class Column:
    name: str
    kind: str  # "text" values are formula-escaped; "value" cells are generated, never escaped.


def _text(name: str) -> Column:
    return Column(name, "text")


def _value(name: str) -> Column:
    return Column(name, "value")


INVENTORY_COLUMNS = (
    _text("inventory_id"),
    _text("title"),
    _value("status"),
    _value("quantity"),
    _text("acquisition_source"),
    _value("acquired_at"),
    _value("acquisition_cost_usd"),
    _value("sourcing_fuel_cost_usd"),
    _value("sourcing_additional_expense_usd"),
    _text("marketplace"),
    _text("marketplace_item_id"),
    _text("marketplace_sku"),
    _value("listed_at"),
    _value("sold_at"),
    _value("sale_id"),
    _value("estimate_currency"),
    _value("estimate_expected_resale_value"),
    _value("estimate_gross_profit"),
    _value("estimate_analyzed_at"),
    _text("notes"),
)

SALES_COLUMNS = (
    _value("sale_id"),
    _text("inventory_id"),
    _text("title"),
    _text("marketplace"),
    _text("marketplace_sku"),
    _text("external_order_id"),
    _text("external_line_item_id"),
    _value("quantity"),
    _value("acquired_at"),
    _value("sold_at"),
    _value("days_held"),
    _value("currency"),
    _value("seller_revenue"),
    _value("item_revenue"),
    _value("buyer_shipping_revenue"),
    _value("acquisition_cost_usd"),
    _value("sourcing_travel_expense_usd"),
    *(_value(column) for _category, _cost_type, column in SALE_COST_COLUMNS),
    _value("reconciliation_state"),
    _value("unconfirmed_categories"),
    _value("recorded_profit"),
    _value("realized_profit"),
    _value("profit_status"),
)


def format_money(value: Decimal | None) -> str:
    """Write an exact amount in plain notation with at least two fractional digits."""
    if value is None:
        return ""
    if not value.is_finite():
        raise ExportError("cannot export a non-finite amount")
    if value == 0:
        value = abs(value)
    places = max(2, -value.as_tuple().exponent)
    return format(value.quantize(Decimal(1).scaleb(-places)), "f")


def format_timestamp(value: str | None) -> str:
    """Normalize a stored ISO timestamp to UTC seconds; unparseable values stay verbatim."""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return escape_text(value)
    if parsed.tzinfo is None:
        return escape_text(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_date(value: str | None) -> str:
    if not value:
        return ""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return escape_text(value)


def escape_text(value: str | None) -> str:
    """Neutralize spreadsheet formula injection in user- or marketplace-provided text."""
    if value is None:
        return ""
    return f"'{value}" if value.startswith(FORMULA_PREFIXES) else value


def _render(columns: tuple[Column, ...], rows: Iterable[dict[str, object]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow([column.name for column in columns])
    for row in rows:
        cells = []
        for column in columns:
            value = row[column.name]
            text = "" if value is None else str(value)
            cells.append(escape_text(text) if column.kind == "text" else text)
        writer.writerow(cells)
    return buffer.getvalue().encode(CSV_ENCODING)


def _inventory_sort_key(record: InventoryRecord) -> tuple[int, int, str, int]:
    number = q_number_value(record.inventory_id)
    return (number is None, number or 0, record.inventory_id, record.internal_id)


def inventory_rows(
    inventory: list[InventoryRecord],
    sales: list[SaleRecord],
    valuations: list[ValuationSnapshot],
    travel: list[SourcingTravelRecord],
) -> list[dict[str, object]]:
    """Project inventory records; baseline research estimates stay in ``estimate_`` columns."""
    sale_by_item = {sale.inventory_internal_id: sale.sale_id for sale in sales}
    baseline = {
        snapshot.inventory_internal_id: snapshot for snapshot in valuations if snapshot.is_baseline
    }
    travel_by_item = {row.inventory_internal_id: row for row in travel}
    rows = []
    for record in sorted(inventory, key=_inventory_sort_key):
        estimate = baseline.get(record.internal_id)
        trip = travel_by_item.get(record.internal_id)
        rows.append(
            {
                "inventory_id": record.inventory_id,
                "title": record.title,
                "status": record.status,
                "quantity": record.quantity,
                "acquisition_source": record.source,
                "acquired_at": format_date(record.acquired_at),
                "acquisition_cost_usd": format_money(record.acquisition_cost),
                "sourcing_fuel_cost_usd": format_money(trip.fuel_cost if trip else None),
                "sourcing_additional_expense_usd": format_money(
                    trip.additional_expense if trip else None
                ),
                "marketplace": record.marketplace,
                "marketplace_item_id": record.marketplace_item_id,
                "marketplace_sku": record.marketplace_sku,
                "listed_at": format_timestamp(record.listed_at),
                "sold_at": format_timestamp(record.sold_at),
                "sale_id": sale_by_item.get(record.internal_id),
                "estimate_currency": estimate.currency if estimate else None,
                "estimate_expected_resale_value": format_money(
                    estimate.expected_resale_value if estimate else None
                ),
                "estimate_gross_profit": format_money(
                    estimate.estimated_gross_profit if estimate else None
                ),
                "estimate_analyzed_at": format_timestamp(
                    estimate.analyzed_at if estimate else None
                ),
                "notes": record.notes,
            }
        )
    return rows


def _category_amounts(row: SaleReportRow, costs: list[SaleCostRecord]) -> dict[str, Decimal | None]:
    """Net recorded reduction per category; unknown when unconfirmed and nothing recorded."""
    amounts: dict[str, Decimal | None] = {}
    for category, cost_type, column in SALE_COST_COLUMNS:
        parts = [cost for cost in costs if cost.category == cost_type]
        if any(cost.currency != row.sale.currency for cost in parts):
            raise ExportError(
                f"sale {row.sale.sale_id} has {cost_type} components in another currency"
            )
        if not parts and category in row.missing_categories:
            amounts[column] = None
            continue
        amounts[column] = sum(
            (cost.amount if cost.effect == "reduce" else -cost.amount for cost in parts),
            Decimal(0),
        )
    return amounts


def _profit_status(row: SaleReportRow, item: InventoryRecord) -> str:
    if row.economics is not None:
        return "realized" if row.reconciliation_state == "fully_reconciled" else "provisional"
    if row.sale.currency != "USD":
        return "unavailable: non-USD sale; currency conversion is not supported"
    if item.acquisition_cost is None:
        return "unavailable: acquisition cost unknown"
    return "unavailable"


def sales_rows(
    inventory: list[InventoryRecord],
    sales: list[SaleRecord],
    costs: list[SaleCostRecord],
    confirmations: dict[int, frozenset[str]],
    travel: list[SourcingTravelRecord],
) -> list[dict[str, object]]:
    """Project completed sales with recorded economics from the shared sales report."""
    report = sales_report(inventory, sales, costs, confirmations, travel)
    inventory_by_id = {record.internal_id: record for record in inventory}
    travel_by_item = {row.inventory_internal_id: row for row in travel}
    costs_by_sale: dict[int, list[SaleCostRecord]] = defaultdict(list)
    for cost in costs:
        costs_by_sale[cost.sale_internal_id].append(cost)
    rows = []
    ordered = sorted(
        report.rows, key=lambda row: (format_timestamp(row.sale.sold_at), row.sale.internal_id)
    )
    for row in ordered:
        sale = row.sale
        item = inventory_by_id[sale.inventory_internal_id]
        trip = travel_by_item.get(item.internal_id)
        recorded = row.economics.recorded_profit if row.economics is not None else None
        realized = recorded if row.reconciliation_state == "fully_reconciled" else None
        rows.append(
            {
                "sale_id": sale.sale_id,
                "inventory_id": sale.inventory_id,
                "title": item.title,
                "marketplace": sale.marketplace,
                "marketplace_sku": sale.marketplace_sku,
                "external_order_id": sale.external_order_id,
                "external_line_item_id": sale.external_line_item_id,
                "quantity": sale.quantity,
                "acquired_at": format_date(item.acquired_at),
                "sold_at": format_timestamp(sale.sold_at),
                "days_held": row.days_held,
                "currency": sale.currency,
                "seller_revenue": format_money(sale.gross_amount),
                "item_revenue": format_money(sale.item_revenue),
                "buyer_shipping_revenue": format_money(sale.buyer_shipping),
                "acquisition_cost_usd": format_money(item.acquisition_cost),
                "sourcing_travel_expense_usd": format_money(
                    trip.recorded_expense if trip is not None else None
                ),
                **{
                    column: format_money(amount)
                    for column, amount in _category_amounts(
                        row, costs_by_sale.get(sale.internal_id, [])
                    ).items()
                },
                "reconciliation_state": row.reconciliation_state,
                "unconfirmed_categories": ";".join(row.missing_categories),
                "recorded_profit": format_money(recorded),
                "realized_profit": format_money(realized),
                "profit_status": _profit_status(row, item),
            }
        )
    return rows


def export_inventory_csv(store: InventoryStore) -> bytes:
    """Return the complete inventory export, or raise without producing partial output."""
    return _export(
        lambda: _render(
            INVENTORY_COLUMNS,
            inventory_rows(
                store.list(),
                store.list_sales(),
                store.list_valuation_snapshots(),
                store.list_sourcing_travel(),
            ),
        )
    )


def export_sales_csv(store: InventoryStore) -> bytes:
    """Return the complete sales export, or raise without producing partial output."""
    return _export(
        lambda: _render(
            SALES_COLUMNS,
            sales_rows(
                store.list(),
                store.list_sales(),
                store.list_all_sale_costs(),
                store.list_reconciliation_confirmations(),
                store.list_sourcing_travel(),
            ),
        )
    )


def _export(build: Callable[[], bytes]) -> bytes:
    try:
        return build()
    except ExportError:
        raise
    except (ArithmeticError, KeyError, ValueError, RuntimeError, sqlite3.Error) as exc:
        raise ExportError(f"export failed: {exc}") from exc


EXPORTS = {"inventory": export_inventory_csv, "sales": export_sales_csv}
