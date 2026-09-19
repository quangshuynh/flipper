"""Server-rendered reseller dashboard over Flipper's existing services."""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from uuid import uuid4
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
from deals.comparables import (
    Comparable,
    ComparableCondition,
    ComparableType,
)
from deals.ebay import EBAY_CATEGORY_MAP, normalize_ebay_item, normalize_search_results
from deals.economics import calculate_economics
from deals.manual import MANUAL_CONDITIONS, create_manual_opportunity
from deals.outcomes import build_decision_outcome
from deals.evaluation import ComparisonResult, DealEvaluation, compare
from deals.models import (
    ConfidenceEvidence,
    CostComponent,
    EvidenceLevel,
    EvidenceProvenance,
    ProvenanceKind,
    RiskFactor,
    Money as DealMoney,
    SourceIdentity,
    TimeToSale,
)
from deals.research import EphemeralResearchStore
from deals.snapshots import SnapshotPayloadError, build_snapshot_payload
from deals.travel import TripEstimate, trip_from_values
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
    ResearchSnapshotNotFoundError,
    ResearchSnapshotValidationError,
    SourcingTravelValidationError,
    SaleNotFoundError,
)
from reports.service import (
    build_summary_report,
    historical_insights,
    inventory_report,
    sales_report,
    valuation_accuracy_report,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = ROOT / "data" / "flipper_inventory.db"
load_dotenv(ROOT / ".env")
templates = Jinja2Templates(directory=ROOT / "web" / "templates")
app.mount("/static", StaticFiles(directory=ROOT / "web" / "static"), name="static")
research_store = EphemeralResearchStore()
RESEARCH_COOKIE = "flipper_research"


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


def _snapshot_money(value) -> str:
    if not value:
        return "Unknown"
    return _money(Decimal(value["amount"]), value["currency"])


templates.env.filters["money"] = _money
templates.env.filters["money2"] = _money_two_places
templates.env.filters["percent"] = _percent
templates.env.filters["snapshot_money"] = _snapshot_money


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
        store.list_sourcing_travel(),
    )
    return inventory_view, sales_view


def _live_listing_results(store: InventoryStore):
    oauth = SellerOAuthClient(SellerOAuthConfig.from_environment())
    listings = ActiveListingsClient(oauth).get_active_listings()
    return reconcile_listings(store, listings)


def _discovery() -> EbayDiscoveryClient:
    return EbayDiscoveryClient(DiscoveryConfig.from_environment())


USER_ASSUMPTION = EvidenceProvenance(ProvenanceKind.USER_ASSUMPTION, "User assumption")


def _estimated(value: str, currency: str) -> CostComponent:
    return (
        CostComponent.estimated(value, currency, provenance=USER_ASSUMPTION)
        if value
        else CostComponent.unknown()
    )


def _taxonomy(client) -> tuple[dict[str, tuple[str, ...]], str]:
    try:
        return client.category_ancestry(), client.config.marketplace_id
    except (AttributeError, EbayDiscoveryError):
        return {}, getattr(getattr(client, "config", None), "marketplace_id", "EBAY_US")


TRIP_VALUE_NAMES = (
    "one_way_miles",
    "vehicle_mpg",
    "gas_price",
    "additional_travel_cost",
    "round_trip_minutes",
)


def _travel_analysis(values: dict[str, str], currency: str):
    component_supplied = any(
        values.get(name, "")
        for name in ("one_way_miles", "vehicle_mpg", "gas_price", "additional_travel_cost")
    )
    if values.get("travel_cost", "") and component_supplied:
        raise ValueError("direct travel cost cannot be combined with component trip assumptions")
    trip = None
    if component_supplied or values.get("round_trip_minutes", ""):
        trip = trip_from_values(
            currency=currency,
            one_way_miles=values.get("one_way_miles", ""),
            vehicle_mpg=values.get("vehicle_mpg", ""),
            gas_price_per_gallon=values.get("gas_price", ""),
            additional_travel_cost=values.get("additional_travel_cost", ""),
            round_trip_minutes=values.get("round_trip_minutes", ""),
        )
    travel_cost = (
        trip.total_travel_cost
        if trip is not None and component_supplied
        else _estimated(values.get("travel_cost", ""), currency)
    )
    return travel_cost, trip


def _deal_analysis(
    opportunity, shipping, values: dict[str, str]
) -> tuple[DealEvaluation, TripEstimate | None]:
    if any(len(value) > 64 for value in values.values()):
        raise ValueError("assumption value is too long")
    minimum = values.get("minimum_sale_days", "")
    maximum = values.get("maximum_sale_days", "")
    time_to_sale = None
    if minimum or maximum:
        if not minimum or not maximum:
            raise ValueError("both minimum and maximum sale days are required")
        time_to_sale = TimeToSale(int(minimum), int(maximum), "user estimate", USER_ASSUMPTION)
    currency = opportunity.base_price.currency
    travel_cost, trip = _travel_analysis(values, currency)
    economics = calculate_economics(
        base_price=opportunity.base_price,
        acquisition_tax=_estimated(values.get("tax", ""), currency),
        inbound_shipping=shipping,
        pickup_travel_cost=travel_cost,
        other_acquisition_cost=_estimated(values.get("other_acquisition_cost", ""), currency),
        expected_resale=_estimated(values.get("expected_resale", ""), currency),
        selling_fees=_estimated(values.get("selling_fees", ""), currency),
        outbound_shipping=_estimated(values.get("outbound_shipping", ""), currency),
        other_selling_cost=_estimated(values.get("other_selling_cost", ""), currency),
        time_to_sale=time_to_sale,
    )
    evidence_explanation = (
        "Browse supplies active listings, not sold comparables."
        if opportunity.base_price_provenance.kind is ProvenanceKind.SOURCE_API
        else "Manual source facts do not establish sold comparable evidence."
    )
    risks = [RiskFactor("no-sold-evidence", evidence_explanation)]
    if shipping.status.value == "unknown":
        risks.append(RiskFactor("unknown-inbound-shipping", "Inbound shipping is unknown."))
    if time_to_sale is None:
        risks.append(RiskFactor("unknown-time-to-sale", "Time-to-sale is unknown."))
    return (
        DealEvaluation(
            economics=economics,
            time_to_sale=time_to_sale,
            confidence=ConfidenceEvidence(
                category_match=(
                    EvidenceLevel.STRONG
                    if opportunity.category_provenance.label == "eBay Taxonomy ancestry"
                    else EvidenceLevel.WEAK
                    if opportunity.category_provenance.kind is ProvenanceKind.SOURCE_API
                    else None
                )
            ),
            risks=tuple(risks),
        ),
        trip,
    )


def _safe_ebay_error(exc: Exception) -> str:
    if isinstance(exc, SellerOAuthError):
        return (
            "eBay authorization is unavailable or no longer has the required scope. "
            "Reconnect with the supported CLI connection workflow, then try again."
        )
    return "eBay listings are temporarily unavailable. No local inventory was changed."


def _safe_discovery_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "The eBay search filters are invalid. Check the entered values."
    message = str(exc).casefold()
    if "not configured" in message or "ebay_discovery_env" in message:
        return "eBay discovery is not configured. Add matching environment credentials."
    if "authentication" in message and ("rejected" in message or "malformed" in message):
        return "eBay discovery authentication was rejected. Check the selected environment."
    if "http 4" in message:
        return "eBay discovery access was rejected. Check configuration and search filters."
    return "eBay discovery is temporarily unavailable. Try again later."


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


def _research_session(request: Request) -> str | None:
    value = request.cookies.get(RESEARCH_COOKIE)
    valid = value and len(value) == 32 and all(char in "0123456789abcdef" for char in value)
    return value if valid else None


def _research_key(item_id: str) -> str:
    return f"ebay:{item_id}"


def _manual_key(opportunity_id: str) -> str:
    return f"manual:{opportunity_id}"


def _opportunity_path(key: str) -> str:
    if key.startswith("ebay:"):
        return f"/deals/ebay/{key.removeprefix('ebay:')}"
    return f"/deals/opportunities/{key.removeprefix('manual:')}"


def _resolve_opportunity(request: Request, key: str):
    if key.startswith("manual:"):
        stored = research_store.get_opportunity(
            _research_session(request), key.removeprefix("manual:")
        )
        return stored.opportunity, stored.inbound_shipping, stored.notes
    if key.startswith("ebay:"):
        client = _discovery()
        ancestry, marketplace = _taxonomy(client)
        opportunity, shipping = normalize_ebay_item(
            client.get_item(key.removeprefix("ebay:")),
            category_ancestry=ancestry,
            marketplace_id=marketplace,
        )
        return opportunity, shipping, None
    raise LookupError("opportunity was not found")


def _render_deal_detail(
    request,
    *,
    key,
    opportunity,
    shipping,
    notes,
    evaluation,
    trip,
    values,
    trip_relevant,
    status_code=200,
):
    evidence = research_store.evidence_set(_research_session(request), key, as_of=_today())
    return _render(
        request,
        "deal_detail.html",
        section="deals",
        title="Deal analysis",
        opportunity=opportunity,
        shipping=shipping,
        evaluation=evaluation,
        values=values,
        notes=notes,
        opportunity_key=key,
        detail_path=_opportunity_path(key),
        source_label=(
            "eBay API"
            if key.startswith("ebay:")
            else opportunity.source.value.replace("-", " ").title()
        ),
        trip=trip,
        trip_relevant=trip_relevant,
        research_rows=research_store.list(_research_session(request), key),
        evidence=evidence,
        comparable_types=ComparableType,
        comparable_conditions=ComparableCondition,
        source_identities=SourceIdentity,
        today=_today().isoformat(),
        save_token=uuid4().hex,
        status_code=status_code,
    )


def _snapshot_assumptions(values: dict[str, str], currency: str, trip) -> dict[str, CostComponent]:
    result = {
        "tax": _estimated(values.get("tax", ""), currency),
        "other_acquisition_cost": _estimated(values.get("other_acquisition_cost", ""), currency),
        "expected_resale": _estimated(values.get("expected_resale", ""), currency),
        "selling_fees": _estimated(values.get("selling_fees", ""), currency),
        "outbound_shipping": _estimated(values.get("outbound_shipping", ""), currency),
        "other_selling_cost": _estimated(values.get("other_selling_cost", ""), currency),
    }
    result["travel_cost"] = (
        trip.total_travel_cost
        if trip is not None
        else _estimated(values.get("travel_cost", ""), currency)
    )
    return result


def _comparable_from_fields(fields: dict[str, str], category: DealCategory) -> Comparable:
    event_date = date.fromisoformat(fields["event_date"]) if fields.get("event_date") else None
    return Comparable(
        evidence_type=ComparableType(fields.get("evidence_type", "")),
        source=fields.get("source", ""),
        price=DealMoney.of(fields.get("price", ""), fields.get("currency", "")),
        observed_date=date.fromisoformat(fields.get("observed_date", "")),
        condition=ComparableCondition(fields.get("condition", "unknown")),
        event_date=event_date,
        source_identity=SourceIdentity(fields.get("source_identity", "other")),
        source_reference_id=fields.get("source_reference_id"),
        title=fields.get("title"),
        source_url=fields.get("source_url"),
        notes=fields.get("notes"),
        category=category,
    )


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
    snapshots = store.list_research_snapshots()
    inventory_by_id = {row.record.inventory_id: row.record for row in report.inventory.rows}
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
        snapshot_count=len(snapshots),
        realized_snapshot_count=sum(
            1
            for snapshot in snapshots
            if snapshot.inventory_id
            and snapshot.inventory_id in inventory_by_id
            and inventory_by_id[snapshot.inventory_id].status == "sold"
        ),
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
        "acquired_desc": lambda row: (
            row.record.acquired_at is not None,
            row.record.acquired_at or "",
            row.record.internal_id,
        ),
        "acquired_asc": lambda row: (
            row.record.acquired_at is None,
            row.record.acquired_at or "",
            row.record.internal_id,
        ),
        "cost_desc": lambda row: (
            row.record.acquisition_cost_cents is not None,
            row.record.acquisition_cost_cents or 0,
            row.record.internal_id,
        ),
        "cost_asc": lambda row: (
            row.record.acquisition_cost_cents is None,
            row.record.acquisition_cost_cents or 0,
            row.record.internal_id,
        ),
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
        sourcing_travel=store.get_sourcing_travel(record.inventory_id),
        linked_snapshots=[
            snapshot
            for snapshot in store.list_research_snapshots()
            if snapshot.inventory_id == record.inventory_id
        ],
    )


@app.post("/inventory/{inventory_id}/sourcing-travel")
async def inventory_sourcing_travel_save(request: Request, inventory_id: str):
    fields = await _post_fields(request)
    try:
        _store().set_sourcing_travel(
            inventory_id,
            round_trip_miles=fields.get("round_trip_miles"),
            fuel_cost=fields.get("fuel_cost"),
            additional_expense=fields.get("additional_expense"),
            travel_minutes=fields.get("travel_minutes"),
            note=fields.get("note", ""),
        )
    except (InventoryNotFoundError, SourcingTravelValidationError) as exc:
        return _redirect(f"/inventory/{inventory_id}", error_message=str(exc))
    return _redirect(f"/inventory/{inventory_id}", message="Actual sourcing travel saved.")


@app.post("/inventory/{inventory_id}/sourcing-travel/clear")
async def inventory_sourcing_travel_clear(request: Request, inventory_id: str):
    await _post_fields(request)
    try:
        _store().clear_sourcing_travel(inventory_id)
    except (InventoryNotFoundError, SourcingTravelValidationError) as exc:
        return _redirect(f"/inventory/{inventory_id}", error_message=str(exc))
    return _redirect(f"/inventory/{inventory_id}", message="Actual sourcing travel cleared.")


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
    report = sales_report(
        [item],
        [sale],
        costs,
        store.list_reconciliation_confirmations(),
        store.list_sourcing_travel(),
    )
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
        linked_snapshots=[
            snapshot
            for snapshot in store.list_research_snapshots()
            if snapshot.inventory_id == item.inventory_id
        ],
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


@app.get("/insights", response_class=HTMLResponse)
def insights_page(request: Request, range: str = "all"):
    presets = {"all": None, "30": 30, "90": 90, "365": 365}
    selected = range if range in presets else "all"
    end = _today() if presets[selected] is not None else None
    start = end - timedelta(days=presets[selected] - 1) if end is not None else None
    insights = historical_insights(_store(), start=start, end=end)
    return _render(
        request,
        "insights.html",
        section="insights",
        title="Historical Insights",
        insights=insights,
        selected_range=selected,
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
            client = _discovery()
            body = client.search(
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
            ancestry, marketplace = _taxonomy(client)
            results.extend(
                normalize_search_results(
                    body, category_ancestry=ancestry, marketplace_id=marketplace
                )
            )
        except (EbayDiscoveryError, ValueError) as exc:
            error = _safe_discovery_error(exc)
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
        manual_opportunities=research_store.list_opportunities(_research_session(request)),
    )


@app.get("/deals/opportunities/new", response_class=HTMLResponse)
def manual_opportunity_form(request: Request):
    return _render(
        request,
        "deal_add.html",
        section="deals",
        title="Add opportunity",
        categories=DealCategory,
        sources=SourceIdentity,
        conditions=MANUAL_CONDITIONS,
        values=dict(request.query_params),
    )


@app.post("/deals/opportunities")
async def manual_opportunity_add(request: Request):
    fields = await _post_fields(request)
    session_id = _research_session(request) or uuid4().hex
    try:
        stored = create_manual_opportunity(fields)
        opportunity_id = research_store.add_opportunity(session_id, stored)
    except (KeyError, TypeError, ValueError) as exc:
        return _redirect("/deals/opportunities/new", error_message=str(exc))
    response = _redirect(f"/deals/opportunities/{opportunity_id}", message="Opportunity added.")
    response.set_cookie(RESEARCH_COOKIE, session_id, httponly=True, samesite="strict")
    return response


@app.get("/deals/opportunities/{opportunity_id}", response_class=HTMLResponse)
def manual_opportunity_detail(request: Request, opportunity_id: str):
    values = dict(request.query_params)
    key = _manual_key(opportunity_id)
    try:
        opportunity, shipping, notes = _resolve_opportunity(request, key)
    except LookupError:
        return _render(
            request,
            "error.html",
            section="deals",
            title="Opportunity unavailable",
            message="This ephemeral opportunity was not found.",
            status_code=404,
        )
    try:
        evaluation, trip = _deal_analysis(opportunity, shipping, values)
    except ValueError as exc:
        evaluation, trip = _deal_analysis(opportunity, shipping, {})
        values["error_message"] = str(exc)
        return _render_deal_detail(
            request,
            key=key,
            opportunity=opportunity,
            shipping=shipping,
            notes=notes,
            evaluation=evaluation,
            trip=trip,
            values=values,
            trip_relevant=True,
            status_code=400,
        )
    return _render_deal_detail(
        request,
        key=key,
        opportunity=opportunity,
        shipping=shipping,
        notes=notes,
        evaluation=evaluation,
        trip=trip,
        values=values,
        trip_relevant=True,
    )


@app.get("/deals/ebay/{item_id}", response_class=HTMLResponse)
def deal_detail(request: Request, item_id: str):
    values = dict(request.query_params)
    try:
        client = _discovery()
        ancestry, marketplace = _taxonomy(client)
        opportunity, shipping = normalize_ebay_item(
            client.get_item(item_id),
            category_ancestry=ancestry,
            marketplace_id=marketplace,
        )
        evaluation, trip = _deal_analysis(opportunity, shipping, values)
    except (EbayDiscoveryError, ValueError):
        return _render(
            request,
            "error.html",
            section="deals",
            title="Deal unavailable",
            message="The eBay opportunity could not be loaded or an assumption was invalid.",
            status_code=503,
        )
    return _render_deal_detail(
        request,
        key=_research_key(item_id),
        opportunity=opportunity,
        shipping=shipping,
        notes=None,
        evaluation=evaluation,
        trip=trip,
        values=values,
        trip_relevant=False,
    )


@app.get("/deals/compare", response_class=HTMLResponse)
def deal_compare(request: Request):
    if len(request.url.query) > 5_000:
        return _render(
            request,
            "deal_compare.html",
            section="deals",
            title="Compare deals",
            entries=(),
            comparisons=(),
            pareto=(),
            error="Comparison state is too large.",
            status_code=400,
        )
    keys = request.query_params.getlist("opportunity_key")
    if not keys:  # Preserve existing eBay-only links.
        keys = [_research_key(item_id) for item_id in request.query_params.getlist("item_id")]
    unique_ids = list(dict.fromkeys(keys))
    error = None
    if len(unique_ids) != len(keys):
        error = "Choose each opportunity only once."
    elif not 2 <= len(unique_ids) <= 4:
        error = "Choose between 2 and 4 opportunities to compare."
    if error:
        return _render(
            request,
            "deal_compare.html",
            section="deals",
            title="Compare deals",
            entries=(),
            comparisons=(),
            pareto=(),
            error=error,
            status_code=400,
        )
    entries = []
    for index, opportunity_key in enumerate(unique_ids):
        prefix = f"d{index}_"
        values = {
            name: request.query_params.get(prefix + name, "")
            for name in (
                "tax",
                "travel_cost",
                "other_acquisition_cost",
                "expected_resale",
                "selling_fees",
                "outbound_shipping",
                "other_selling_cost",
                "minimum_sale_days",
                "maximum_sale_days",
                *TRIP_VALUE_NAMES,
            )
        }
        try:
            opportunity, shipping, _ = _resolve_opportunity(request, opportunity_key)
            evaluation, trip = _deal_analysis(opportunity, shipping, values)
            evidence = research_store.evidence_set(
                _research_session(request), opportunity_key, as_of=_today()
            )
            entries.append(
                {
                    "item_id": opportunity_key,
                    "opportunity_key": opportunity_key,
                    "opportunity": opportunity,
                    "shipping": shipping,
                    "evaluation": evaluation,
                    "trip": trip,
                    "evidence": evidence,
                    "sold_summaries": evidence.summaries(ComparableType.SOLD),
                    "active_summaries": evidence.summaries(ComparableType.ACTIVE_ASKING),
                    "values": values,
                    "index": index,
                    "error": None,
                }
            )
        except (EbayDiscoveryError, LookupError, ValueError):
            entries.append(
                {
                    "item_id": opportunity_key,
                    "opportunity_key": opportunity_key,
                    "index": index,
                    "error": "Unavailable",
                }
            )
    valid = [entry for entry in entries if not entry["error"]]
    comparisons = []
    dominated: set[str] = set()
    for left_index, left in enumerate(valid):
        for right in valid[left_index + 1 :]:
            result = compare(left["evaluation"], right["evaluation"])
            comparisons.append((left, right, result))
            if result.result is ComparisonResult.LEFT_DOMINATES:
                dominated.add(right["item_id"])
            elif result.result is ComparisonResult.RIGHT_DOMINATES:
                dominated.add(left["item_id"])
    pareto = tuple(entry["item_id"] for entry in valid if entry["item_id"] not in dominated)
    return _render(
        request,
        "deal_compare.html",
        section="deals",
        title="Compare deals",
        entries=entries,
        comparisons=comparisons,
        pareto=pareto,
        error=None,
    )


@app.get("/deals/history", response_class=HTMLResponse)
def research_history(request: Request):
    return _render(
        request,
        "research_history.html",
        section="research",
        title="Research history",
        snapshots=_store().list_research_snapshots(),
    )


@app.get("/deals/history/{snapshot_id}", response_class=HTMLResponse)
def research_snapshot_detail(request: Request, snapshot_id: str):
    store = _store()
    try:
        snapshot = store.get_research_snapshot(snapshot_id)
    except (ResearchSnapshotNotFoundError, SnapshotPayloadError):
        return _render(
            request,
            "error.html",
            section="research",
            title="Research snapshot unavailable",
            message="That saved research snapshot was not found or cannot be safely read.",
            status_code=404,
        )
    return _render(
        request,
        "research_snapshot.html",
        section="research",
        title="Saved research snapshot",
        snapshot=snapshot,
        payload=snapshot.payload,
        outcome=build_decision_outcome(store, snapshot, today=_today()),
    )


async def _save_snapshot(request: Request, opportunity_key: str):
    fields = await _post_fields(request)
    path = _opportunity_path(opportunity_key)
    token = fields.get("save_token", "")
    values = {
        name: fields.get(name, "")
        for name in (
            "tax",
            "travel_cost",
            "other_acquisition_cost",
            "expected_resale",
            "selling_fees",
            "outbound_shipping",
            "other_selling_cost",
            "minimum_sale_days",
            "maximum_sale_days",
            *TRIP_VALUE_NAMES,
        )
    }
    try:
        opportunity, shipping, notes = _resolve_opportunity(request, opportunity_key)
        evaluation, trip = _deal_analysis(opportunity, shipping, values)
        evidence = research_store.evidence_set(
            _research_session(request), opportunity_key, as_of=_today()
        )
        assumptions = _snapshot_assumptions(values, opportunity.base_price.currency, trip)
        payload = build_snapshot_payload(
            opportunity=opportunity,
            shipping=shipping,
            notes=notes,
            assumption_components=assumptions,
            evaluation=evaluation,
            trip=trip,
            evidence=evidence,
        )
        snapshot = _store().save_research_snapshot(
            opportunity_identity=opportunity_key,
            source=opportunity.source.value,
            title=opportunity.title,
            category=opportunity.category.slug,
            currency=opportunity.base_price.currency,
            asking_price=opportunity.base_price.amount,
            expected_resale=(
                assumptions["expected_resale"].money.amount
                if assumptions["expected_resale"].money
                else None
            ),
            expected_profit=(
                evaluation.economics.expected_net_profit.amount
                if evaluation.economics.expected_net_profit
                else None
            ),
            payload=payload,
            save_token=token,
        )
    except EbayDiscoveryError:
        return _redirect(path, error_message="eBay discovery is unavailable.")
    except (LookupError, TypeError, ValueError) as exc:
        return _redirect(path, error_message=str(exc))
    return _redirect(f"/deals/history/{snapshot.snapshot_id}", message="Research snapshot saved.")


@app.post("/deals/ebay/{item_id}/snapshot")
async def ebay_snapshot_save(request: Request, item_id: str):
    return await _save_snapshot(request, _research_key(item_id))


@app.post("/deals/opportunities/{opportunity_id}/snapshot")
async def manual_snapshot_save(request: Request, opportunity_id: str):
    return await _save_snapshot(request, _manual_key(opportunity_id))


@app.post("/deals/history/{snapshot_id}/link")
async def research_snapshot_link(request: Request, snapshot_id: str):
    fields = await _post_fields(request)
    try:
        snapshot = _store().link_research_snapshot(snapshot_id, fields.get("inventory_id", ""))
    except (
        InventoryNotFoundError,
        ResearchSnapshotNotFoundError,
        ResearchSnapshotValidationError,
    ) as exc:
        return _redirect(f"/deals/history/{snapshot_id}", error_message=str(exc))
    return _redirect(f"/deals/history/{snapshot.snapshot_id}", message="Inventory link saved.")


@app.post("/deals/ebay/{item_id}/comparables")
async def comparable_add(request: Request, item_id: str):
    fields = await _post_fields(request)
    session_id = _research_session(request) or uuid4().hex
    try:
        client = _discovery()
        ancestry, marketplace = _taxonomy(client)
        opportunity, _ = normalize_ebay_item(
            client.get_item(item_id), category_ancestry=ancestry, marketplace_id=marketplace
        )
        comparable = _comparable_from_fields(fields, opportunity.category)
        research_store.add(session_id, _research_key(item_id), comparable)
    except EbayDiscoveryError:
        return _redirect(f"/deals/ebay/{item_id}", error_message="eBay discovery is unavailable.")
    except (KeyError, TypeError, ValueError) as exc:
        return _redirect(f"/deals/ebay/{item_id}", error_message=str(exc))
    response = _redirect(f"/deals/ebay/{item_id}", message="Comparable added.")
    response.set_cookie(RESEARCH_COOKIE, session_id, httponly=True, samesite="strict")
    return response


@app.post("/inventory/{inventory_id}/notes")
async def inventory_notes_update(request: Request, inventory_id: str):
    fields = await _post_fields(request)
    try:
        record = _store().update(inventory_id, notes=fields.get("notes", ""))
    except InventoryNotFoundError:
        return _redirect("/inventory", error_message="Inventory item was not found.")
    except InventoryValidationError as exc:
        return _redirect(f"/inventory/{inventory_id}", error_message=str(exc))
    return _redirect(f"/inventory/{record.inventory_id}", message="Notes saved.")


@app.post("/deals/ebay/{item_id}/comparables/{comparable_id}/edit")
async def comparable_edit(request: Request, item_id: str, comparable_id: str):
    fields = await _post_fields(request)
    session_id = _research_session(request)
    if not session_id:
        return _redirect(f"/deals/ebay/{item_id}", error_message="Comparable record was not found.")
    try:
        client = _discovery()
        ancestry, marketplace = _taxonomy(client)
        opportunity, _ = normalize_ebay_item(
            client.get_item(item_id), category_ancestry=ancestry, marketplace_id=marketplace
        )
        research_store.replace(
            session_id,
            _research_key(item_id),
            comparable_id,
            _comparable_from_fields(fields, opportunity.category),
        )
    except EbayDiscoveryError:
        return _redirect(f"/deals/ebay/{item_id}", error_message="eBay discovery is unavailable.")
    except (KeyError, LookupError, TypeError, ValueError) as exc:
        return _redirect(f"/deals/ebay/{item_id}", error_message=str(exc))
    return _redirect(f"/deals/ebay/{item_id}", message="Comparable updated.")


@app.post("/deals/ebay/{item_id}/comparables/{comparable_id}/remove")
async def comparable_remove(request: Request, item_id: str, comparable_id: str):
    await _post_fields(request)
    session_id = _research_session(request)
    try:
        if not session_id:
            raise LookupError("comparable record was not found")
        research_store.remove(session_id, _research_key(item_id), comparable_id)
    except LookupError as exc:
        return _redirect(f"/deals/ebay/{item_id}", error_message=str(exc))
    return _redirect(f"/deals/ebay/{item_id}", message="Comparable removed.")


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
        client = _discovery()
        ancestry, marketplace = _taxonomy(client)
        opportunity, _ = normalize_ebay_item(
            client.get_item(item_id),
            category_ancestry=ancestry,
            marketplace_id=marketplace,
        )
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


@app.post("/deals/opportunities/{opportunity_id}/comparables")
async def manual_comparable_add(request: Request, opportunity_id: str):
    fields = await _post_fields(request)
    session_id = _research_session(request)
    path = f"/deals/opportunities/{opportunity_id}"
    try:
        if not session_id:
            raise LookupError("manual opportunity was not found")
        stored = research_store.get_opportunity(session_id, opportunity_id)
        comparable = _comparable_from_fields(fields, stored.opportunity.category)
        research_store.add(session_id, _manual_key(opportunity_id), comparable)
    except (KeyError, LookupError, TypeError, ValueError) as exc:
        return _redirect(path, error_message=str(exc))
    return _redirect(path, message="Comparable added.")


@app.post("/deals/opportunities/{opportunity_id}/comparables/{comparable_id}/edit")
async def manual_comparable_edit(request: Request, opportunity_id: str, comparable_id: str):
    fields = await _post_fields(request)
    session_id = _research_session(request)
    path = f"/deals/opportunities/{opportunity_id}"
    try:
        if not session_id:
            raise LookupError("comparable record was not found")
        stored = research_store.get_opportunity(session_id, opportunity_id)
        research_store.replace(
            session_id,
            _manual_key(opportunity_id),
            comparable_id,
            _comparable_from_fields(fields, stored.opportunity.category),
        )
    except (KeyError, LookupError, TypeError, ValueError) as exc:
        return _redirect(path, error_message=str(exc))
    return _redirect(path, message="Comparable updated.")


@app.post("/deals/opportunities/{opportunity_id}/comparables/{comparable_id}/remove")
async def manual_comparable_remove(request: Request, opportunity_id: str, comparable_id: str):
    await _post_fields(request)
    session_id = _research_session(request)
    path = f"/deals/opportunities/{opportunity_id}"
    try:
        if not session_id:
            raise LookupError("comparable record was not found")
        research_store.remove(session_id, _manual_key(opportunity_id), comparable_id)
    except LookupError as exc:
        return _redirect(path, error_message=str(exc))
    return _redirect(path, message="Comparable removed.")


@app.post("/deals/opportunities/{opportunity_id}/acquire")
async def manual_deal_acquire(request: Request, opportunity_id: str):
    fields = await _post_fields(request)
    path = f"/deals/opportunities/{opportunity_id}"
    required = {
        "acquisition_cost": "actual acquisition cost",
        "acquired_at": "actual acquisition date",
        "acquisition_source": "actual acquisition source",
    }
    missing = [label for name, label in required.items() if not fields.get(name)]
    if missing:
        return _redirect(path, error_message=f"Required: {', '.join(missing)}.")
    try:
        stored = research_store.get_opportunity(_research_session(request), opportunity_id)
        reference_notes = stored.notes or ""
        if stored.opportunity.url:
            reference_notes = "\n".join(
                part for part in (reference_notes, f"Source URL: {stored.opportunity.url}") if part
            )
        record = acquire_opportunity(
            _store(),
            stored.opportunity,
            acquisition_cost=fields["acquisition_cost"],
            acquired_at=fields["acquired_at"],
            acquisition_source=fields["acquisition_source"],
            notes=reference_notes,
        )
    except (InventoryValidationError, LookupError, ValueError) as exc:
        return _redirect(path, error_message=str(exc))
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
            source=fields.get("source"),
            acquired_at=fields.get("acquired_at"),
            acquisition_cost=fields.get("acquisition_cost"),
        )
    except (SellerOAuthError, ActiveListingsApiError) as exc:
        return _redirect("/ebay/listings", error_message=_safe_ebay_error(exc))
    except (InventoryValidationError, ValueError) as exc:
        return _redirect("/ebay/listings", error_message=str(exc))
    return _redirect(
        f"/inventory/{record.inventory_id}",
        message=("Imported from eBay." if created else "Already imported; no duplicate created."),
    )
