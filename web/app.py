"""Server-rendered reseller dashboard over Flipper's existing services."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import keyring
from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import pass_context
from keyring.errors import KeyringError
from starlette.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from ebay.compliance import app
from inventory.store import InventoryNotFoundError, InventoryStore, SaleNotFoundError
from reports.service import (
    build_summary_report,
    inventory_report,
    sales_report,
    valuation_accuracy_report,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = ROOT / "data" / "flipper_inventory.db"
templates = Jinja2Templates(directory=ROOT / "web" / "templates")
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")


def _store() -> InventoryStore:
    path = Path(os.getenv("FLIPPER_INVENTORY_DB", str(DEFAULT_DATABASE)))
    store = InventoryStore(path)
    store.initialize()
    return store


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _money(value: Decimal | None, currency: str = "USD") -> str:
    if value is None:
        return "Unavailable"
    places = max(2, -value.as_tuple().exponent)
    return f"{currency} {value:,.{places}f}"


def _percent(value: Decimal | None) -> str:
    return "Unavailable" if value is None else f"{value * 100:,.1f}%"


templates.env.filters["money"] = _money
templates.env.filters["percent"] = _percent


@pass_context
def _nav_class(context, name: str) -> str:
    return "active" if context.get("section") == name else ""


templates.env.globals["nav_class"] = _nav_class


def _render(request: Request, template: str, **context):
    status_code = context.pop("status_code", 200)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def _load_reports(store: InventoryStore):
    inventory = store.list()
    sales = store.list_sales()
    inventory_view = inventory_report(inventory, sales, today=_today())
    sales_view = sales_report(
        inventory,
        sales,
        store.list_all_sale_costs(),
        store.list_reconciliation_confirmations(),
    )
    return inventory_view, sales_view


@app.exception_handler(sqlite3.Error)
@app.exception_handler(RuntimeError)
async def database_error(request: Request, _exc: sqlite3.Error):
    return _render(
        request,
        "error.html",
        section="",
        title="Database unavailable",
        message=(
            "Flipper could not read the local inventory database. Check the path and try again."
        ),
        status_code=503,
    )


@app.exception_handler(StarletteHTTPException)
async def web_http_error(request: Request, exc: StarletteHTTPException):
    if not request.url.path.startswith(("/inventory", "/sales")):
        return await http_exception_handler(request, exc)
    message = (
        "The requested local record does not exist."
        if exc.status_code == 404
        else "The request could not be completed."
    )
    return _render(
        request,
        "error.html",
        section="",
        title="Record not found" if exc.status_code == 404 else "Request error",
        message=message,
        status_code=exc.status_code,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    report = build_summary_report(_store(), today=_today())
    recent_inventory = tuple(reversed(report.inventory.rows[-5:]))
    recent_sales = tuple(reversed(report.sales.rows[-5:]))
    return _render(
        request,
        "dashboard.html",
        section="dashboard",
        title="Dashboard",
        report=report,
        recent_inventory=recent_inventory,
        recent_sales=recent_sales,
    )


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(
    request: Request,
    status: str = "",
    search: str = "",
    sort: str = "acquired_desc",
):
    report, _ = _load_reports(_store())
    rows = list(report.rows)
    if status:
        rows = [row for row in rows if row.record.status == status]
    if search:
        needle = search.casefold()
        rows = [
            row
            for row in rows
            if needle in row.record.inventory_id.casefold()
            or needle in (row.record.marketplace_sku or "").casefold()
            or needle in row.record.title.casefold()
        ]
    sorters = {
        "acquired_desc": lambda row: (row.record.acquired_at, row.record.internal_id),
        "acquired_asc": lambda row: (row.record.acquired_at, row.record.internal_id),
        "cost_desc": lambda row: (row.record.acquisition_cost_cents, row.record.internal_id),
        "cost_asc": lambda row: (row.record.acquisition_cost_cents, row.record.internal_id),
        "age_desc": lambda row: (row.days_held if row.days_held is not None else -1),
    }
    selected_sort = sort if sort in sorters else "acquired_desc"
    rows.sort(key=sorters[selected_sort], reverse=selected_sort.endswith("desc"))
    return _render(
        request,
        "inventory.html",
        section="inventory",
        title="Inventory",
        rows=rows,
        status=status,
        search=search,
        sort=selected_sort,
    )


@app.get("/inventory/{inventory_id}", response_class=HTMLResponse)
def inventory_detail(request: Request, inventory_id: str):
    store = _store()
    try:
        record = store.get(inventory_id)
    except InventoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Inventory item not found") from exc
    report = inventory_report([record], store.list_sales(), today=_today())
    row = report.rows[0]
    sale_row = None
    if row.linked_sale_id:
        sales = sales_report(
            [record],
            [store.get_sale(row.linked_sale_id)],
            store.list_sale_costs(row.linked_sale_id),
            store.list_reconciliation_confirmations(),
        )
        sale_row = sales.rows[0]
    return _render(
        request,
        "inventory_detail.html",
        section="inventory",
        title=record.inventory_id,
        row=row,
        sale_row=sale_row,
        valuation=store.baseline_valuation(record.inventory_id),
        valuation_comparison=next(
            (
                result
                for result in valuation_accuracy_report(store).rows
                if result.inventory.internal_id == record.internal_id
            ),
            None,
        ),
    )


@app.get("/sales", response_class=HTMLResponse)
def sales_page(request: Request, state: str = ""):
    _, report = _load_reports(_store())
    rows = list(reversed(report.rows))
    if state:
        rows = [row for row in rows if row.reconciliation_state == state]
    return _render(
        request,
        "sales.html",
        section="sales",
        title="Sales",
        rows=rows,
        state=state,
    )


@app.get("/sales/{sale_id}", response_class=HTMLResponse)
def sale_detail(request: Request, sale_id: str):
    store = _store()
    try:
        sale = store.get_sale(sale_id)
        item = store.get(sale.inventory_id)
    except (SaleNotFoundError, InventoryNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Sale not found") from exc
    costs = store.list_sale_costs(sale.sale_id)
    report = sales_report([item], [sale], costs, store.list_reconciliation_confirmations())
    valuation_row = next(
        (
            result
            for result in valuation_accuracy_report(store).rows
            if result.inventory.internal_id == item.internal_id
        ),
        None,
    )
    return _render(
        request,
        "sale_detail.html",
        section="sales",
        title=sale.sale_id,
        row=report.rows[0],
        costs=costs,
        valuation_row=valuation_row,
    )


@app.get("/analytics", response_class=HTMLResponse)
def analytics_page(request: Request):
    store = _store()
    inventory, sales = _load_reports(store)
    valuations = valuation_accuracy_report(store)
    sales_by_month: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    profit_by_month: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for row in sales.rows:
        month = row.sale.sold_at[:7]
        sales_by_month[month][row.sale.currency] += row.sale.gross_amount
        if row.economics is not None:
            profit_by_month[month][row.sale.currency] += row.economics.recorded_profit
    return _render(
        request,
        "analytics.html",
        section="analytics",
        title="Analytics",
        inventory=inventory,
        sales=sales,
        sales_by_month={key: dict(value) for key, value in sorted(sales_by_month.items())},
        profit_by_month={key: dict(value) for key, value in sorted(profit_by_month.items())},
        valuations=valuations,
    )


@app.get("/analyze", response_class=HTMLResponse)
def analyze_page(request: Request):
    return _render(request, "analyze.html", section="analyze", title="Analyze")


def _ebay_status() -> dict[str, object]:
    environment = os.getenv("EBAY_SELLER_ENV", "production").strip().lower()
    client_id = os.getenv("EBAY_SELLER_CLIENT_ID", "").strip()
    configured = all(
        os.getenv(name, "").strip()
        for name in ("EBAY_SELLER_CLIENT_ID", "EBAY_SELLER_CLIENT_SECRET", "EBAY_SELLER_RUNAME")
    )
    connected: bool | None = False
    if client_id:
        try:
            connected = bool(keyring.get_password(f"flipper.ebay.seller.{environment}", client_id))
        except KeyringError:
            connected = None
    return {"environment": environment, "configured": configured, "connected": connected}


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _render(
        request,
        "settings.html",
        section="settings",
        title="Integrations / Settings",
        ebay=_ebay_status(),
    )
