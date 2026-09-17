"""Server-rendered reseller dashboard over Flipper's existing services."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import pass_context
from dotenv import load_dotenv
from starlette.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from acquisition import acquire_opportunity
from deals.categories import DealCategory, normalize_category
from deals.ebay import EBAY_CATEGORY_MAP, normalize_ebay_item, normalize_search_results
from deals.economics import calculate_economics
from deals.evaluation import DealEvaluation
from deals.models import ConfidenceEvidence, CostComponent, EvidenceLevel, RiskFactor, TimeToSale
from ebay.compliance import app
from ebay.discovery import DiscoveryConfig, EbayDiscoveryClient, EbayDiscoveryError
from ebay.active_listings import ActiveListingsApiError, ActiveListingsClient
from ebay.listing_reconciliation import summarize as summarize_listings
from ebay.listing_workflow import import_listing, reconcile_listings, sync_listings
from ebay.seller_oauth import SellerOAuthClient, SellerOAuthConfig, SellerOAuthError
from inventory.attachments import AttachmentService
from inventory.store import (
    AttachmentNotFoundError,
    AttachmentValidationError,
    InventoryNotFoundError,
    InventoryValidationError,
    InventoryStore,
    SaleNotFoundError,
)
from reports.service import (
    build_summary_report,
    inventory_report,
    sales_report,
    valuation_accuracy_report,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = ROOT / "data" / "flipper_inventory.db"
load_dotenv(ROOT / ".env")
templates = Jinja2Templates(directory=ROOT / "web" / "templates")
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")


def _store() -> InventoryStore:
    path = Path(os.getenv("FLIPPER_INVENTORY_DB", str(DEFAULT_DATABASE)))
    store = InventoryStore(path)
    store.initialize()
    return store


def _attachments(store: InventoryStore) -> AttachmentService:
    configured = os.getenv("FLIPPER_ATTACHMENT_ROOT")
    return AttachmentService(store, configured if configured else None)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _money(value: Decimal | None, currency: str = "USD") -> str:
    if value is None:
        return "Unavailable"
    places = max(2, -value.as_tuple().exponent)
    return f"{currency} {value:,.{places}f}"


def _percent(value: Decimal | None) -> str:
    return "Unavailable" if value is None else f"{value * 100:,.1f}%"


def _money_two_places(value: Decimal | None, currency: str = "USD") -> str:
    if value is None:
        return "Unavailable"
    rounded = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{currency} {rounded:,.2f}"


templates.env.filters["money"] = _money
templates.env.filters["money2"] = _money_two_places
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


def _live_listing_results(store: InventoryStore):
    oauth = SellerOAuthClient(SellerOAuthConfig.from_environment())
    listings = ActiveListingsClient(oauth).get_active_listings()
    return reconcile_listings(store, listings)


def _discovery() -> EbayDiscoveryClient:
    return EbayDiscoveryClient(DiscoveryConfig.from_environment())


def _estimated(value: str, currency: str) -> CostComponent:
    return CostComponent.estimated(value, currency) if value else CostComponent.unknown()


def _deal_analysis(opportunity, shipping, values: dict[str, str]) -> DealEvaluation:
    minimum = values.get("minimum_sale_days", "")
    maximum = values.get("maximum_sale_days", "")
    time_to_sale = None
    if minimum or maximum:
        if not minimum or not maximum:
            raise ValueError("both minimum and maximum sale days are required")
        time_to_sale = TimeToSale(int(minimum), int(maximum), "user estimate")
    currency = opportunity.base_price.currency
    economics = calculate_economics(
        base_price=opportunity.base_price,
        acquisition_tax=_estimated(values.get("tax", ""), currency),
        inbound_shipping=shipping,
        pickup_travel_cost=_estimated(values.get("travel_cost", ""), currency),
        other_acquisition_cost=_estimated(values.get("other_acquisition_cost", ""), currency),
        expected_resale=_estimated(values.get("expected_resale", ""), currency),
        selling_fees=_estimated(values.get("selling_fees", ""), currency),
        outbound_shipping=_estimated(values.get("outbound_shipping", ""), currency),
        other_selling_cost=_estimated(values.get("other_selling_cost", ""), currency),
        time_to_sale=time_to_sale,
    )
    risks = [
        RiskFactor("no-sold-evidence", "Browse supplies active listings, not sold comparables.")
    ]
    if shipping.status.value == "unknown":
        risks.append(RiskFactor("unknown-inbound-shipping", "Inbound shipping is unknown."))
    if time_to_sale is None:
        risks.append(RiskFactor("unknown-time-to-sale", "Time-to-sale is unknown."))
    return DealEvaluation(
        economics=economics,
        time_to_sale=time_to_sale,
        confidence=ConfidenceEvidence(category_match=EvidenceLevel.WEAK),
        risks=tuple(risks),
    )


def _safe_ebay_error(exc: Exception) -> str:
    if isinstance(exc, SellerOAuthError):
        return (
            "eBay authorization is unavailable or no longer has the required scope. "
            "Reconnect with the supported CLI connection workflow, then try again."
        )
    return "eBay listings are temporarily unavailable. No local inventory was changed."


async def _post_fields(request: Request) -> dict[str, str]:
    """Parse small URL-encoded local forms without introducing multipart handling."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="Unsupported form encoding")
    origin = request.headers.get("origin")
    if origin:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc != request.url.netloc:
            raise HTTPException(status_code=403, detail="Cross-origin mutation rejected")
    raw = await request.body()
    if len(raw) > 16_384:
        raise HTTPException(status_code=413, detail="Form is too large")
    values = parse_qs(raw.decode("utf-8", errors="strict"), keep_blank_values=True)
    return {key: entries[-1].strip() for key, entries in values.items() if entries}


def _redirect(path: str, **params: str) -> RedirectResponse:
    target = f"{path}?{urlencode(params)}" if params else path
    return RedirectResponse(target, status_code=303)


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
    store = _store()
    report = build_summary_report(store, today=_today())
    recent_inventory = tuple(reversed(report.inventory.rows[-5:]))
    recent_sales = tuple(reversed(report.sales.rows[-5:]))
    attachment_counts = store.attachment_counts()
    attention = {
        "acquired": tuple(row for row in report.inventory.rows if row.record.status == "acquired"),
        "unlinked": tuple(
            row
            for row in report.inventory.rows
            if row.record.status == "listed"
            and not (row.record.marketplace_item_id and row.record.marketplace_sku)
        ),
        "missing_attachments": tuple(
            row
            for row in report.inventory.rows
            if row.record.status in {"acquired", "listed"}
            and attachment_counts.get(row.record.internal_id, 0) == 0
        ),
        "unreconciled_sales": tuple(
            row for row in report.sales.rows if row.reconciliation_state != "fully_reconciled"
        ),
    }
    return _render(
        request,
        "dashboard.html",
        section="dashboard",
        title="Dashboard",
        report=report,
        recent_inventory=recent_inventory,
        recent_sales=recent_sales,
        attention=attention,
    )


@app.get("/inventory", response_class=HTMLResponse)
def inventory_page(
    request: Request,
    status: str = "",
    search: str = "",
    sort: str = "acquired_desc",
    linkage: str = "",
):
    store = _store()
    report, _ = _load_reports(store)
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
    if linkage == "linked":
        rows = [row for row in rows if row.record.marketplace_item_id]
    elif linkage == "unlinked":
        rows = [row for row in rows if not row.record.marketplace_item_id]
    sorters = {
        "acquired_desc": lambda row: (row.record.acquired_at, row.record.internal_id),
        "acquired_asc": lambda row: (row.record.acquired_at, row.record.internal_id),
        "cost_desc": lambda row: (row.record.acquisition_cost_cents, row.record.internal_id),
        "cost_asc": lambda row: (row.record.acquisition_cost_cents, row.record.internal_id),
        "age_desc": lambda row: (row.days_held if row.days_held is not None else -1),
        "q_asc": lambda row: row.record.internal_id,
        "q_desc": lambda row: row.record.internal_id,
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
        linkage=linkage if linkage in {"linked", "unlinked"} else "",
        attachment_counts=store.attachment_counts(),
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
        attachments=_attachments(store).list(record.inventory_id),
    )


@app.get("/inventory/{inventory_id}/attachments/{attachment_id}", response_class=FileResponse)
def inventory_attachment(inventory_id: str, attachment_id: str):
    """Serve only a database-owned attachment resolved inside Flipper's local root."""
    store = _store()
    service = _attachments(store)
    try:
        attachment = store.get_attachment(attachment_id, inventory_id=inventory_id)
        path = service.path_for(attachment)
    except (AttachmentNotFoundError, AttachmentValidationError) as exc:
        raise HTTPException(status_code=404, detail="Attachment not found") from exc
    return FileResponse(
        path,
        media_type=attachment.media_type,
        headers={"X-Content-Type-Options": "nosniff"},
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


@app.get("/deals", response_class=HTMLResponse)
def deals_page(
    request: Request,
    q: str = "",
    category: str = "",
    minimum_price: str = "",
    maximum_price: str = "",
    condition: str = "",
    sort: str = "relevance",
    page: int = 1,
):
    results = []
    error = None
    page = max(1, min(page, 100))
    selected_category = None
    if category:
        try:
            selected_category = normalize_category(category)
        except ValueError:
            error = "Choose a recognized Flipper category."
    if q and error is None:
        try:
            body = _discovery().search(
                q,
                category_id=next(
                    (key for key, value in EBAY_CATEGORY_MAP.items() if value == selected_category),
                    None,
                ),
                minimum_price=minimum_price or None,
                maximum_price=maximum_price or None,
                condition=condition or None,
                limit=24,
                offset=(page - 1) * 24,
            )
            results.extend(normalize_search_results(body))
        except (EbayDiscoveryError, ValueError):
            error = (
                "eBay discovery is unavailable or the search is invalid. "
                "Check configuration and filters."
            )
    if sort in {"price_asc", "price_desc"}:
        results.sort(key=lambda row: row[0].base_price.amount, reverse=sort.endswith("desc"))
    else:
        sort = "relevance"
    return _render(
        request,
        "deals.html",
        section="deals",
        title="Deals",
        results=results,
        error=error,
        searched=bool(q),
        categories=DealCategory,
        q=q,
        category=category,
        minimum_price=minimum_price,
        maximum_price=maximum_price,
        condition=condition,
        sort=sort,
        page=page,
    )


@app.get("/deals/ebay/{item_id}", response_class=HTMLResponse)
def deal_detail(request: Request, item_id: str):
    values = dict(request.query_params)
    try:
        opportunity, shipping = normalize_ebay_item(_discovery().get_item(item_id))
        evaluation = _deal_analysis(opportunity, shipping, values)
    except (EbayDiscoveryError, ValueError):
        return _render(
            request,
            "error.html",
            section="deals",
            title="Deal unavailable",
            message="The eBay opportunity could not be loaded or an assumption was invalid.",
            status_code=503,
        )
    return _render(
        request,
        "deal_detail.html",
        section="deals",
        title="Deal analysis",
        opportunity=opportunity,
        shipping=shipping,
        evaluation=evaluation,
        values=values,
    )


@app.post("/deals/ebay/{item_id}/acquire")
async def deal_acquire(request: Request, item_id: str):
    fields = await _post_fields(request)
    required = {
        "acquisition_cost": "actual acquisition cost",
        "acquired_at": "actual acquisition date",
        "acquisition_source": "actual acquisition source",
    }
    missing = [label for name, label in required.items() if not fields.get(name)]
    if missing:
        return _redirect(f"/deals/ebay/{item_id}", error_message=f"Required: {', '.join(missing)}.")
    try:
        opportunity, _ = normalize_ebay_item(_discovery().get_item(item_id))
        record = acquire_opportunity(
            _store(),
            opportunity,
            acquisition_cost=fields["acquisition_cost"],
            acquired_at=fields["acquired_at"],
            acquisition_source=fields["acquisition_source"],
        )
    except EbayDiscoveryError:
        return _redirect(f"/deals/ebay/{item_id}", error_message="eBay discovery is unavailable.")
    except (InventoryValidationError, ValueError) as exc:
        return _redirect(f"/deals/ebay/{item_id}", error_message=str(exc))
    return _redirect(f"/inventory/{record.inventory_id}", message="Acquisition recorded.")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    return _render(
        request,
        "settings.html",
        section="settings",
        title="Integrations / Settings",
        ebay=SellerOAuthConfig.connection_status_from_environment(),
    )


@app.get("/ebay/listings", response_class=HTMLResponse)
def ebay_listings_page(request: Request, message: str = "", error_message: str = ""):
    """Isolate the optional live eBay call from every other dashboard page."""
    try:
        results = _live_listing_results(_store())
    except (SellerOAuthError, ActiveListingsApiError) as exc:
        return _render(
            request,
            "ebay_listings.html",
            section="ebay",
            title="eBay Listings",
            results=(),
            summary=None,
            error=_safe_ebay_error(exc),
            status_code=503,
        )
    return _render(
        request,
        "ebay_listings.html",
        section="ebay",
        title="eBay Listings",
        results=results,
        summary=summarize_listings(results),
        error=None,
        message=message,
        error_message=error_message,
    )


@app.post("/ebay/listings/sync")
async def ebay_listings_sync(request: Request):
    await _post_fields(request)
    store = _store()
    try:
        summary = sync_listings(store, _live_listing_results(store))
    except (SellerOAuthError, ActiveListingsApiError) as exc:
        return _render(
            request,
            "ebay_listings.html",
            section="ebay",
            title="eBay Listings",
            results=(),
            summary=None,
            error=_safe_ebay_error(exc),
            status_code=503,
        )
    message = (
        f"{summary.checked} listings checked; {summary.already_synchronized} already "
        f"synchronized; {summary.updated} local inventory items updated; "
        f"{summary.conflicts} conflicts. eBay was not modified."
    )
    return _redirect("/ebay/listings", message=message)


@app.post("/ebay/listings/import")
async def ebay_listing_import(request: Request):
    fields = await _post_fields(request)
    required = {
        "item_id": "listing identity",
        "sku": "Q-number",
        "source": "acquisition source",
        "acquired_at": "acquisition date",
        "acquisition_cost": "acquisition cost",
    }
    missing = [label for name, label in required.items() if not fields.get(name)]
    if missing:
        return _redirect("/ebay/listings", error_message=f"Required: {', '.join(missing)}.")
    store = _store()
    try:
        results = _live_listing_results(store)
        record, created = import_listing(
            store,
            results,
            item_id=fields["item_id"],
            sku=fields["sku"],
            source=fields["source"],
            acquired_at=fields["acquired_at"],
            acquisition_cost=fields["acquisition_cost"],
        )
    except (SellerOAuthError, ActiveListingsApiError) as exc:
        return _redirect("/ebay/listings", error_message=_safe_ebay_error(exc))
    except (InventoryValidationError, ValueError) as exc:
        return _redirect("/ebay/listings", error_message=str(exc))
    return _redirect(
        f"/inventory/{record.inventory_id}",
        message=("Imported from eBay." if created else "Already imported; no duplicate created."),
    )
