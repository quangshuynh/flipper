"""Main entry point for Flipper."""

import argparse
import os
import sqlite3
import sys
import webbrowser
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from getpass import getpass
from pathlib import Path

from dotenv import load_dotenv

from acquisition import acquire_from_analysis, analyze_listing
from collectors.json_feed_collector import DEFAULT_JSON_PATH, fetch_listings
from deals.categories import normalize_category
from deals.economics import calculate_economics
from deals.models import (
    CostComponent,
    EvidenceProvenance,
    Money as DealMoney,
    ProvenanceKind,
    SourceIdentity,
    TimeToSale,
)
from deals.travel import trip_from_values
from ebay.finance_reconciliation import (
    FinanceMatchStatus,
    reconcile_finance_transactions,
)
from ebay.finance_import import FinanceImportStatus, import_finance_transactions
from ebay.finance_transactions import FinanceTransaction
from ebay.active_listings import ActiveListingsApiError, ActiveListingsClient
from ebay.listing_reconciliation import (
    ListingReconciliationState,
    summarize as summarize_listings,
)
from ebay.listing_workflow import import_listing, reconcile_listings, sync_listings
from ebay.finances import FinancesApiError, FinancesClient
from ebay.fulfillment import FulfillmentApiError, FulfillmentClient
from ebay.orders import EbayOrder, Money
from ebay.reconciliation import ReconciliationStatus, reconcile_ebay_orders, summarize
from ebay.sale_import import SaleImportStatus, import_ebay_sales
from ebay.seller_oauth import SellerOAuthClient, SellerOAuthConfig, SellerOAuthError
from inventory.attachments import AttachmentService
from inventory.store import (
    ATTACHMENT_CATEGORIES,
    RECONCILIATION_CATEGORIES,
    SALE_COST_TYPES,
    VALID_STATUSES,
    AttachmentNotFoundError,
    AttachmentValidationError,
    InventoryNotFoundError,
    InventoryRecord,
    InventoryStore,
    InventoryValidationError,
    SaleNotFoundError,
    SaleCostNotFoundError,
    SaleCostValidationError,
    ValuationValidationError,
)
from notifier.discord_notifier import send_deal_to_discord
from reports.service import (
    build_summary_report,
    inventory_report,
    sales_report,
    valuation_accuracy_report,
)
from sales.economics import EconomicComponent, calculate_sale_economics
from utils.dedupe import init_db, has_seen, mark_seen


EBAY_NOW_SAFETY_MARGIN = timedelta(minutes=5)
INVENTORY_DB_PATH = "data/flipper_inventory.db"


def run() -> None:
    """
    Execute one Flipper scan cycle.

    :returns: None.
    """
    load_dotenv()
    init_db()

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    home_lat = float(os.getenv("HOME_LAT", "43.0831"))
    home_lon = float(os.getenv("HOME_LON", "-77.6743"))
    max_radius_miles = float(os.getenv("MAX_RADIUS_MILES", "100"))
    min_profit = float(os.getenv("MIN_PROFIT", "100"))
    min_score = int(os.getenv("MIN_DEAL_SCORE", "55"))

    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not configured; alerts are disabled.")

    listings = fetch_listings()
    print(f"Fetched {len(listings)} listings.\n")

    for listing in listings:
        print("=" * 80)
        print(f"Checking listing: {listing.listing_id}")
        print(f"Title: {listing.title}")

        if has_seen(listing.listing_id):
            print(f"Skipping seen listing: {listing.listing_id}")
            continue

        analysis = analyze_listing(listing, home_lat=home_lat, home_lon=home_lon)
        deal = analysis.deal

        print(f"AI summary: {analysis.ai_summary}")

        distance_miles = deal.distance_miles

        print(f"Distance: {distance_miles}")

        if distance_miles is not None and distance_miles > max_radius_miles:
            print(
                f"Skipping {listing.listing_id}: "
                f"out of radius ({distance_miles:.1f} mi > {max_radius_miles:.1f} mi)"
            )
            mark_seen(listing.listing_id)
            continue

        print(
            f"[{listing.listing_id}] listing_price=${listing.price:.2f}, "
            f"estimated_value=${deal.estimated_market_value:.2f}, "
            f"ideal_buy=${deal.ideal_buy_price:.2f}, "
            f"expected_resale=${deal.expected_resale_value:.2f}, "
            f"gross_profit=${deal.estimated_gross_profit:.2f}, score={deal.score}"
        )
        if deal.estimated_gross_profit >= min_profit and deal.score >= min_score:
            print(
                f"Listing PASSED filters (profit {deal.estimated_gross_profit:.2f} "
                f">= {min_profit:.2f}, score {deal.score} >= {min_score})"
            )
            if not webhook_url:
                print("Alert skipped because Discord is not configured.")
                continue
            success = send_deal_to_discord(
                webhook_url=webhook_url, deal=deal, ai_summary=analysis.ai_summary
            )
            if success:
                print(f"Sent deal alert for {listing.listing_id}")
                mark_seen(listing.listing_id)
            else:
                print(f"Failed to send alert for {listing.listing_id}")
                print("Not marking as seen so it can retry next run.")
        else:
            print(
                f"Listing FAILED filters (profit {deal.estimated_gross_profit:.2f} "
                f"< {min_profit:.2f} or score {deal.score} < {min_score})"
            )
            mark_seen(listing.listing_id)


def _seller_oauth() -> SellerOAuthClient:
    return SellerOAuthClient(SellerOAuthConfig.from_environment())


def _format_money(value: Money | None) -> str:
    if value is None:
        return "not provided"
    symbols = {"USD": "$", "CAD": "C$", "GBP": "£", "EUR": "€"}
    prefix = symbols.get(value.currency, f"{value.currency} ")
    return f"{prefix}{value.value:.2f}"


def _print_orders(orders: list[EbayOrder]) -> None:
    print("Recent eBay Orders")
    if not orders:
        print("\nNo orders found in the selected date range.")
        return
    for order in orders:
        print(f"\n{order.creation_date.date().isoformat()}")
        for item in order.line_items:
            print(item.title)
            details = f"Qty: {item.quantity}"
            if item.sku:
                details += f" | SKU: {item.sku}"
            print(details)
        print(f"Order total: {_format_money(order.pricing.total)}")
        print(f"Fulfillment: {order.fulfillment_status or 'not provided'}")
        print(f"Payment: {order.payment_status or 'not provided'}")
        if order.cancellation_status:
            print(f"Cancellation: {order.cancellation_status}")
        print(f"Order: {order.order_id}")


def _print_reconciliation(orders: list[EbayOrder], inventory: list[InventoryRecord]) -> None:
    results = reconcile_ebay_orders(orders, inventory)
    print("eBay Inventory Reconciliation")
    for result in results:
        line = result.line_item
        print("")
        if result.status is ReconciliationStatus.MATCHED:
            record = result.inventory_matches[0]
            print(f"{record.inventory_id} | {record.title}")
        else:
            print(line.title)
        print(f"eBay order: {result.order_id}")
        print(f"SKU: {line.sku or 'not provided'}")
        print(f"Match: {result.status.value.replace('_', ' ')}")
        if result.status is ReconciliationStatus.AMBIGUOUS:
            identifiers = ", ".join(record.inventory_id for record in result.inventory_matches)
            print(f"Local inventory candidates: {identifiers}")
        if line.legacy_item_id:
            print(f"eBay item ID: {line.legacy_item_id}")

    summary = summarize(results)
    print("")
    print(
        "Summary: "
        f"{summary.examined} examined | {summary.matched} matched | "
        f"{summary.unmatched} unmatched | {summary.missing_sku} missing SKU | "
        f"{summary.ambiguous} ambiguous"
    )


def _format_sale_money(amount: int, scale: int, currency: str) -> str:
    value = Decimal(amount).scaleb(-scale)
    display_scale = max(2, scale)
    return f"{currency} {value:.{display_scale}f}"


def _print_sale_import(orders: list[EbayOrder], store: InventoryStore) -> bool:
    results = import_ebay_sales(orders, store)
    print("eBay Sale Import")
    for result in results:
        target = f" -> {result.inventory_id}" if result.inventory_id else ""
        print(
            f"{result.order_id} / {result.line_item_id} | "
            f"{result.status.value.replace('_', ' ')}{target}"
        )
        if result.detail:
            print(f"  {result.detail}")
    counts = {status: 0 for status in SaleImportStatus}
    for result in results:
        counts[result.status] += 1
    print("")
    print(
        "Summary: "
        f"{len(results)} examined | {counts[SaleImportStatus.IMPORTED]} imported | "
        f"{counts[SaleImportStatus.ALREADY_IMPORTED]} already imported | "
        f"{counts[SaleImportStatus.UNMATCHED]} unmatched | "
        f"{counts[SaleImportStatus.MISSING_SKU]} missing SKU | "
        f"{counts[SaleImportStatus.AMBIGUOUS]} ambiguous | "
        f"{counts[SaleImportStatus.MISSING_GROSS]} missing gross | "
        f"{counts[SaleImportStatus.UNSUPPORTED_QUANTITY]} unsupported quantity | "
        f"{counts[SaleImportStatus.INCOMPATIBLE_INVENTORY]} incompatible | "
        f"{counts[SaleImportStatus.CONFLICT]} conflict"
    )
    return not any(
        result.status in {SaleImportStatus.INCOMPATIBLE_INVENTORY, SaleImportStatus.CONFLICT}
        for result in results
    )


def _print_finances(transactions: list[FinanceTransaction], sales) -> None:
    matches = reconcile_finance_transactions(transactions, sales)
    print("eBay Financial Transactions")
    for result in matches:
        transaction = result.transaction
        print("")
        print(f"Transaction: {transaction.transaction_id}")
        print(f"Type: {transaction.native_type}")
        if transaction.classification.value == "unknown":
            print("Classification: unknown/unsupported")
        else:
            print(f"Classification: {transaction.classification.value.replace('_', ' ')}")
        print(f"Date: {transaction.transaction_date.isoformat()}")
        print(f"Amount: {transaction.amount.currency} {transaction.amount.value}")
        print(f"Order: {transaction.order_id or 'not provided'}")
        line_ids = ", ".join(line.order_line_item_id for line in transaction.order_lines)
        print(f"Order line: {line_ids or 'not provided'}")
        fee_types = sorted({fee for line in transaction.order_lines for fee in line.fee_types})
        if fee_types:
            print(f"eBay fee type: {', '.join(fee_types)}")
        sale_ids = ", ".join(sale.sale_id for sale in result.sales)
        print(f"Sale: {sale_ids or 'none'}")
        print(f"Match: {result.status.value.replace('_', ' ')}")

    counts = {status: 0 for status in FinanceMatchStatus}
    for result in matches:
        counts[result.status] += 1
    unknown = sum(transaction.classification.value == "unknown" for transaction in transactions)
    currencies = ", ".join(sorted({transaction.amount.currency for transaction in transactions}))
    print("")
    print(
        "Summary: "
        f"{len(matches)} transactions | {counts[FinanceMatchStatus.MATCHED]} matched | "
        f"{counts[FinanceMatchStatus.UNMATCHED]} unmatched | "
        f"{counts[FinanceMatchStatus.AMBIGUOUS]} ambiguous | "
        f"{counts[FinanceMatchStatus.NOT_ENOUGH_IDENTIFIERS]} not enough identifiers | "
        f"{unknown} unknown/unsupported | currencies: {currencies or 'none'}"
    )


def _print_finance_import(transactions: list[FinanceTransaction], store: InventoryStore) -> bool:
    results = import_finance_transactions(transactions, store)
    print("eBay Finances Accounting Import")
    for result in results:
        target = f" -> {result.sale.sale_id}" if result.sale else ""
        category = f" | {result.category}" if result.category else ""
        print(
            f"{result.transaction_id} | {result.status.value.replace('_', ' ')}{target}{category}"
        )
        if result.detail:
            print(f"  {result.detail}")
    counts = {status: 0 for status in FinanceImportStatus}
    for result in results:
        counts[result.status] += 1
    print("")
    print(f"examined: {len(transactions)}")
    print(f"imported: {counts[FinanceImportStatus.IMPORTED]}")
    print(f"already imported: {counts[FinanceImportStatus.ALREADY_IMPORTED]}")
    print(f"unsupported: {counts[FinanceImportStatus.UNSUPPORTED]}")
    print(f"unmatched: {counts[FinanceImportStatus.UNMATCHED]}")
    print(f"ambiguous: {counts[FinanceImportStatus.AMBIGUOUS]}")
    print(f"insufficient identifiers: {counts[FinanceImportStatus.INSUFFICIENT_IDENTIFIERS]}")
    print(f"conflicts: {counts[FinanceImportStatus.CONFLICT]}")
    print(f"rejected: {counts[FinanceImportStatus.REJECTED]}")
    return not any(
        result.status in {FinanceImportStatus.CONFLICT, FinanceImportStatus.REJECTED}
        for result in results
    )


def _parse_date(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("dates must use YYYY-MM-DD") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _order_date_range(
    start: datetime | None,
    end: datetime | None,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> tuple[datetime, datetime]:
    current = _aware_utc(clock(), "Current time")
    safe_now = current - EBAY_NOW_SAFETY_MARGIN
    start_utc = _aware_utc(start, "Order start date") if start else None

    if end is None:
        inclusive_end = safe_now
    else:
        end_utc = _aware_utc(end, "Order end date")
        if end_utc.date() > current.date():
            raise ValueError("Order end date cannot be in the future")
        end_of_date = end_utc + timedelta(days=1, milliseconds=-1)
        # Only today's boundary represents "now" and needs clock-skew protection.
        inclusive_end = min(end_of_date, safe_now)

    start_utc = start_utc or (inclusive_end - timedelta(days=30))
    if start_utc >= inclusive_end:
        raise ValueError("Order start date must be before end date")
    return start_utc, inclusive_end


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Flipper resale analysis CLI")
    commands = parser.add_subparsers(dest="command")
    acquire = commands.add_parser(
        "acquire", help="explicitly analyze and record one exported listing as acquired"
    )
    acquire.add_argument("--listing-id", required=True)
    acquire.add_argument("--feed", type=Path, default=DEFAULT_JSON_PATH)
    acquire.add_argument("--cost", required=True, help="actual acquisition cost in USD")
    acquire.add_argument(
        "--acquired-at", required=True, help="actual acquisition date (YYYY-MM-DD)"
    )
    acquire.add_argument("--source", help="actual acquisition source; defaults to listing source")
    acquire.add_argument("--notes", default="")
    acquire.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    deals = commands.add_parser("deals", help="analyze source-neutral deal economics")
    deal_commands = deals.add_subparsers(dest="deals_command", required=True)
    deal_analyze = deal_commands.add_parser(
        "analyze", help="analyze one explicitly supplied synthetic or manual opportunity"
    )
    deal_analyze.add_argument("--title", required=True)
    deal_analyze.add_argument(
        "--source", choices=[source.value for source in SourceIdentity], required=True
    )
    deal_analyze.add_argument("--category", required=True)
    deal_analyze.add_argument("--currency", default="USD")
    deal_analyze.add_argument("--base-price", type=Decimal, required=True)
    deal_analyze.add_argument("--tax", type=Decimal)
    deal_analyze.add_argument("--inbound-shipping", type=Decimal)
    deal_analyze.add_argument("--travel-cost", type=Decimal)
    deal_analyze.add_argument("--one-way-distance", type=Decimal, dest="one_way_miles")
    deal_analyze.add_argument("--vehicle-mpg", type=Decimal)
    deal_analyze.add_argument("--gas-price", type=Decimal)
    deal_analyze.add_argument("--additional-travel-cost", type=Decimal)
    deal_analyze.add_argument("--travel-minutes", type=int, dest="round_trip_minutes")
    deal_analyze.add_argument("--other-acquisition-cost", type=Decimal)
    deal_analyze.add_argument("--expected-resale", type=Decimal)
    deal_analyze.add_argument("--selling-fees", type=Decimal)
    deal_analyze.add_argument("--outbound-shipping", type=Decimal)
    deal_analyze.add_argument("--other-selling-cost", type=Decimal)
    deal_analyze.add_argument("--minimum-sale-days", type=int)
    deal_analyze.add_argument("--maximum-sale-days", type=int)
    ebay = commands.add_parser("ebay", help="eBay seller integration")
    ebay_commands = ebay.add_subparsers(dest="ebay_command", required=True)
    ebay_commands.add_parser("connect", help="authorize a seller account")
    ebay_commands.add_parser("disconnect", help="remove local seller authorization")
    orders = ebay_commands.add_parser("orders", help="show recent seller orders")
    orders.add_argument("--from", dest="start", type=_parse_date, help="start date (YYYY-MM-DD)")
    orders.add_argument("--to", dest="end", type=_parse_date, help="end date (YYYY-MM-DD)")
    reconcile = ebay_commands.add_parser(
        "reconcile", help="match seller order lines to local inventory"
    )
    reconcile.add_argument("--from", dest="start", type=_parse_date, help="start date (YYYY-MM-DD)")
    reconcile.add_argument("--to", dest="end", type=_parse_date, help="end date (YYYY-MM-DD)")
    reconcile.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    import_sales = ebay_commands.add_parser(
        "import-sales", help="persist deterministically matched order lines as sales"
    )
    import_sales.add_argument(
        "--from", dest="start", type=_parse_date, help="start date (YYYY-MM-DD)"
    )
    import_sales.add_argument("--to", dest="end", type=_parse_date, help="end date (YYYY-MM-DD)")
    import_sales.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    finances = ebay_commands.add_parser(
        "finances", help="show read-only seller financial transactions"
    )
    finances.add_argument("--from", dest="start", type=_parse_date, help="start date (YYYY-MM-DD)")
    finances.add_argument("--to", dest="end", type=_parse_date, help="end date (YYYY-MM-DD)")
    finances.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    import_finances = ebay_commands.add_parser(
        "import-finances", help="import supported matched Finances costs into local sales"
    )
    import_finances.add_argument(
        "--from", dest="start", type=_parse_date, help="start date (YYYY-MM-DD)"
    )
    import_finances.add_argument("--to", dest="end", type=_parse_date, help="end date (YYYY-MM-DD)")
    import_finances.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    listings = ebay_commands.add_parser(
        "listings", help="show active seller listings and local reconciliation"
    )
    listings.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    sync_listings = ebay_commands.add_parser(
        "sync-listings", help="explicitly sync safe matched listings to local inventory"
    )
    sync_listings.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    import_listing = ebay_commands.add_parser(
        "import-listing", help="adopt one missing-local listing; unknown history may be omitted"
    )
    import_listing.add_argument("sku")
    import_listing.add_argument("--source", help="actual acquisition source, if known")
    import_listing.add_argument(
        "--acquired-at", help="actual acquisition date (YYYY-MM-DD), if known"
    )
    import_listing.add_argument("--cost", help="actual acquisition cost in USD, if known")
    import_listing.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    inventory = commands.add_parser("inventory", help="manage local inventory")
    inventory.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    inventory.add_argument("--attachment-root", help=argparse.SUPPRESS)
    inventory_commands = inventory.add_subparsers(dest="inventory_command", required=True)
    add = inventory_commands.add_parser("add", help="record an acquired item")
    add.add_argument("--title", required=True)
    add.add_argument("--source", required=True, help="acquisition source")
    add.add_argument("--acquired-at", required=True, help="acquisition date (YYYY-MM-DD)")
    add.add_argument("--cost", required=True, help="acquisition cost in USD")
    add.add_argument("--quantity", type=int, default=1)
    add.add_argument("--notes", default="")
    add.add_argument("--marketplace")
    add.add_argument("--marketplace-item-id")
    add.add_argument("--marketplace-sku")
    inventory_commands.add_parser("list", help="list inventory")
    show = inventory_commands.add_parser("show", help="show one inventory record")
    show.add_argument("inventory_id")
    update = inventory_commands.add_parser("update", help="update an inventory record")
    update.add_argument("inventory_id")
    update.add_argument("--title")
    update.add_argument("--source", help="acquisition source")
    update.add_argument("--acquired-at", help="acquisition date (YYYY-MM-DD)")
    update.add_argument("--cost", dest="acquisition_cost", help="acquisition cost in USD")
    update.add_argument("--quantity", type=int)
    update.add_argument("--notes")
    update.add_argument("--marketplace")
    update.add_argument("--marketplace-item-id")
    update.add_argument("--marketplace-sku")
    status = inventory_commands.add_parser("status", help="transition inventory lifecycle status")
    status.add_argument("inventory_id")
    status.add_argument("status", choices=VALID_STATUSES)
    attach = inventory_commands.add_parser("attach", help="import a local attachment")
    attach.add_argument("inventory_id")
    attach.add_argument("source")
    attach.add_argument("--category", choices=ATTACHMENT_CATEGORIES, default="other")
    attachments = inventory_commands.add_parser(
        "attachments", help="list attachments for one inventory item"
    )
    attachments.add_argument("inventory_id")
    remove_attachment = inventory_commands.add_parser(
        "remove-attachment", help="remove one attachment by durable ID"
    )
    remove_attachment.add_argument("inventory_id")
    remove_attachment.add_argument("attachment_id")
    valuation = inventory_commands.add_parser(
        "valuation", help="show immutable valuation snapshots for one inventory item"
    )
    valuation.add_argument("inventory_id")
    attach_valuation = inventory_commands.add_parser(
        "attach-valuation", help="attach exact typed analyzer outputs to a Q-number"
    )
    attach_valuation.add_argument("inventory_id")
    attach_valuation.add_argument("--analyzed-at", type=_parse_date)
    attach_valuation.add_argument("--currency", default="USD")
    attach_valuation.add_argument("--market-value", type=Decimal, required=True)
    attach_valuation.add_argument("--estimated-resale", type=Decimal, required=True)
    attach_valuation.add_argument("--asking-price", type=Decimal, required=True)
    attach_valuation.add_argument("--ideal-buy-price", type=Decimal, required=True)
    attach_valuation.add_argument("--estimated-profit", type=Decimal, required=True)
    attach_valuation.add_argument("--estimated-roi", type=Decimal)
    attach_valuation.add_argument("--deal-score", type=int, required=True)
    attach_valuation.add_argument("--pricing-method", default="component-estimator")
    sales = commands.add_parser("sales", help="inspect imported local sales")
    sales.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    sales_commands = sales.add_subparsers(dest="sales_command", required=True)
    sales_commands.add_parser("list", help="list imported sales")
    sale_show = sales_commands.add_parser("show", help="show one imported sale")
    sale_show.add_argument("sale_id")
    add_cost = sales_commands.add_parser(
        "add-cost", help="record a manual cost or reducing adjustment"
    )
    add_cost.add_argument("sale_id")
    add_cost.add_argument("--type", dest="category", choices=SALE_COST_TYPES, required=True)
    add_cost.add_argument("--amount", type=Decimal, required=True)
    add_cost.add_argument("--currency", help="defaults to the sale currency")
    add_cost.add_argument("--note", default="")
    add_cost.add_argument("--effect", choices=("reduce", "increase"), default="reduce")
    remove_cost = sales_commands.add_parser("remove-cost", help="remove an erroneous cost entry")
    remove_cost.add_argument("cost_id")
    confirm = sales_commands.add_parser(
        "confirm", help="confirm an economics category is complete, including zero/not applicable"
    )
    confirm.add_argument("sale_id")
    confirm.add_argument("--category", choices=RECONCILIATION_CATEGORIES, required=True)
    unconfirm = sales_commands.add_parser(
        "unconfirm", help="reset a mistaken reconciliation confirmation"
    )
    unconfirm.add_argument("sale_id")
    unconfirm.add_argument("--category", choices=RECONCILIATION_CATEGORIES, required=True)
    reports = commands.add_parser("reports", help="show local reseller business reports")
    reports.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
    report_commands = reports.add_subparsers(dest="report_command", required=True)
    for name, help_text in (
        ("summary", "show inventory and realized-economics summary"),
        ("inventory", "show current inventory detail"),
        ("sales", "show sale-level realized economics"),
    ):
        report = report_commands.add_parser(name, help=help_text)
        report.add_argument(
            "--from", dest="start", type=_parse_date, help="inclusive date (YYYY-MM-DD)"
        )
        report.add_argument(
            "--to", dest="end", type=_parse_date, help="inclusive date (YYYY-MM-DD)"
        )
    report_commands.add_parser("valuation", help="show baseline estimate accuracy")
    return parser


def run_deals_command(args: argparse.Namespace) -> int:
    """Print ephemeral generalized economics without writing inventory."""
    try:
        category = normalize_category(args.category)
        if (args.minimum_sale_days is None) != (args.maximum_sale_days is None):
            raise ValueError("minimum and maximum sale days must be supplied together")
        time_to_sale = (
            TimeToSale(args.minimum_sale_days, args.maximum_sale_days, "manual CLI input")
            if args.minimum_sale_days is not None
            else None
        )

        user_assumption = EvidenceProvenance(ProvenanceKind.USER_ASSUMPTION, "User assumption")

        def estimate(value: Decimal | None, *, unknown: bool = False) -> CostComponent:
            if value is not None:
                return CostComponent.estimated(value, args.currency, provenance=user_assumption)
            return CostComponent.unknown() if unknown else CostComponent.not_applicable()

        component_trip_supplied = any(
            value is not None
            for value in (
                args.one_way_miles,
                args.vehicle_mpg,
                args.gas_price,
                args.additional_travel_cost,
            )
        )
        if args.travel_cost is not None and component_trip_supplied:
            raise ValueError("--travel-cost cannot be combined with component trip assumptions")
        trip = None
        if component_trip_supplied or args.round_trip_minutes is not None:
            trip = trip_from_values(
                currency=args.currency,
                one_way_miles=args.one_way_miles,
                vehicle_mpg=args.vehicle_mpg,
                gas_price_per_gallon=args.gas_price,
                additional_travel_cost=args.additional_travel_cost,
                round_trip_minutes=args.round_trip_minutes,
            )
        travel_cost = (
            trip.total_travel_cost
            if trip is not None and component_trip_supplied
            else estimate(args.travel_cost)
        )

        economics = calculate_economics(
            base_price=DealMoney(args.base_price, args.currency),
            acquisition_tax=estimate(args.tax),
            inbound_shipping=estimate(args.inbound_shipping),
            pickup_travel_cost=travel_cost,
            other_acquisition_cost=estimate(args.other_acquisition_cost),
            expected_resale=estimate(args.expected_resale, unknown=True),
            selling_fees=estimate(args.selling_fees),
            outbound_shipping=estimate(args.outbound_shipping),
            other_selling_cost=estimate(args.other_selling_cost),
            time_to_sale=time_to_sale,
        )
        print(f"{args.title} | {category.label} | {args.source}")
        print(f"Economics state: {economics.state.value.replace('_', ' ')}")
        for label, value in (
            ("Landed acquisition cost", economics.landed_cost),
            ("Expected net proceeds", economics.expected_net_proceeds),
            ("Expected net profit", economics.expected_net_profit),
            ("Capital tied up", economics.capital_tied_up),
        ):
            print(f"{label}: {_format_deal_money(value)}")
        roi_display = economics.roi if economics.roi is not None else economics.roi_state.value
        print(f"Expected ROI: {roi_display}")
        if trip:
            print(
                "Round-trip mileage: "
                f"{trip.round_trip_miles if trip.round_trip_miles is not None else 'unknown'}"
            )
            print(
                "Estimated fuel used: "
                f"{trip.estimated_gallons if trip.estimated_gallons is not None else 'unknown'}"
            )
            print(f"Estimated fuel cost: {_format_deal_money(trip.estimated_fuel_cost)}")
            print(f"Total modeled travel cost: {_format_deal_money(trip.total_travel_cost.money)}")
            if trip.assumptions.round_trip_minutes is not None:
                print(f"Round-trip travel time: {trip.assumptions.round_trip_minutes} minutes")
            for missing in trip.missing_inputs:
                print(f"Trip input needed: {missing}")
        if time_to_sale:
            print(f"Time to sale: {time_to_sale.minimum_days}-{time_to_sale.maximum_days} days")
        if economics.profit_velocity:
            velocity = economics.profit_velocity
            print(
                "Expected profit/day: "
                f"{_format_deal_money(velocity.conservative_profit_per_day)} to "
                f"{_format_deal_money(velocity.optimistic_profit_per_day)}"
            )
        for reason in economics.unavailable_reasons:
            print(f"Unavailable: {reason}")
        return 0
    except (TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _format_deal_money(value: DealMoney | None) -> str:
    if value is None:
        return "unknown"
    return f"{value.currency} {value.amount.quantize(Decimal('0.01'))}"


def run_acquisition_command(args: argparse.Namespace) -> int:
    """Analyze one selected exported listing and explicitly record its acquisition."""
    try:
        matches = [
            listing
            for listing in fetch_listings(args.feed)
            if listing.listing_id == args.listing_id
        ]
        if not matches:
            raise ValueError(f"listing {args.listing_id} was not found in {args.feed}")
        if len(matches) != 1:
            raise ValueError(f"listing ID {args.listing_id} is not unique in {args.feed}")
        analysis = analyze_listing(
            matches[0],
            home_lat=float(os.getenv("HOME_LAT", "43.0831")),
            home_lon=float(os.getenv("HOME_LON", "-77.6743")),
        )
        record = acquire_from_analysis(
            InventoryStore(args.database),
            analysis,
            acquisition_cost=args.cost,
            acquired_at=args.acquired_at,
            acquisition_source=args.source,
            notes=args.notes,
        )
        print(f"Acquired {record.inventory_id}: {record.title}")
        print(f"Actual acquisition cost: ${record.acquisition_cost:.2f}")
        print("Baseline valuation attached from the selected analysis.")
        return 0
    except (
        FileNotFoundError,
        ValueError,
        InventoryValidationError,
        ValuationValidationError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_inventory_command(args: argparse.Namespace) -> int:
    store = InventoryStore(args.database)
    attachment_service = AttachmentService(store, args.attachment_root)
    try:
        if args.inventory_command == "add":
            record = store.add(
                title=args.title,
                source=args.source,
                acquired_at=args.acquired_at,
                acquisition_cost=args.cost,
                quantity=args.quantity,
                notes=args.notes,
                marketplace=args.marketplace,
                marketplace_item_id=args.marketplace_item_id,
                marketplace_sku=args.marketplace_sku,
            )
            print(f"Created inventory item {record.inventory_id}")
            return 0
        if args.inventory_command == "list":
            records = store.list()
            if not records:
                print("No inventory items found.")
                return 0
            for record in records:
                print(
                    f"{record.inventory_id} | {record.status} | qty {record.quantity} | "
                    f"{_optional_usd(record.acquisition_cost)} | {record.title}"
                )
            return 0

        if args.inventory_command == "update":
            fields = {
                name: value
                for name, value in vars(args).items()
                if name
                in {
                    "title",
                    "source",
                    "acquired_at",
                    "acquisition_cost",
                    "quantity",
                    "notes",
                    "marketplace",
                    "marketplace_item_id",
                    "marketplace_sku",
                }
                and value is not None
            }
            record = store.update(args.inventory_id, **fields)
            print(f"Updated inventory item {record.inventory_id}")
            print(
                f"{record.status} | qty {record.quantity} | "
                f"{_optional_usd(record.acquisition_cost)} | {record.title}"
            )
            return 0

        if args.inventory_command == "status":
            record = store.transition_status(args.inventory_id, args.status)
            print(f"Inventory item {record.inventory_id}: {record.status}")
            return 0

        if args.inventory_command == "attach":
            attachment = attachment_service.add(
                args.inventory_id, args.source, category=args.category
            )
            print(f"Attached {attachment.attachment_id} to {attachment.inventory_id}")
            print(
                f"{attachment.category} | {attachment.original_filename} | "
                f"{attachment.media_type} | {attachment.byte_size} bytes"
            )
            return 0

        if args.inventory_command == "attachments":
            attachments = attachment_service.list(args.inventory_id)
            if not attachments:
                print(f"No attachments found for {args.inventory_id.upper()}.")
                return 0
            for attachment in attachments:
                print(
                    f"{attachment.attachment_id} | {attachment.category} | "
                    f"{attachment.original_filename} | {attachment.media_type} | "
                    f"{attachment.byte_size} bytes | {attachment.created_at}"
                )
            return 0

        if args.inventory_command == "remove-attachment":
            attachment = attachment_service.remove(args.inventory_id, args.attachment_id)
            print(f"Removed attachment {attachment.attachment_id} from {attachment.inventory_id}")
            return 0

        if args.inventory_command == "attach-valuation":
            snapshot = store.add_valuation_snapshot(
                args.inventory_id,
                analyzed_at=args.analyzed_at or _utc_now(),
                currency=args.currency,
                estimated_market_value=args.market_value,
                expected_resale_value=args.estimated_resale,
                asking_price=args.asking_price,
                ideal_buy_price=args.ideal_buy_price,
                estimated_gross_profit=args.estimated_profit,
                estimated_roi=args.estimated_roi,
                deal_score=args.deal_score,
                pricing_method=args.pricing_method,
            )
            kind = "baseline" if snapshot.is_baseline else "additional"
            print(f"Attached {kind} valuation to {snapshot.inventory_id}")
            return 0

        if args.inventory_command == "valuation":
            snapshots = store.list_valuation_snapshots(args.inventory_id)
            if not snapshots:
                print(f"No valuation snapshots found for {args.inventory_id.upper()}.")
                return 0
            for snapshot in snapshots:
                label = "baseline" if snapshot.is_baseline else "additional"
                print(f"{snapshot.inventory_id} | {label} | {snapshot.analyzed_at}")
                resale = _report_money(snapshot.currency, snapshot.expected_resale_value)
                profit = _report_money(snapshot.currency, snapshot.estimated_gross_profit)
                print(
                    f"  estimated resale: {resale} | estimated gross profit: {profit} | "
                    f"deal score: {snapshot.deal_score}"
                )
                print(f"  pricing method: {snapshot.pricing_method or 'unavailable'}")
            return 0

        record = store.get(args.inventory_id)
        print(f"{record.inventory_id}: {record.title}")
        print("Acquisition")
        print(f"  Source: {record.source or 'unknown'}")
        print(f"  Date: {record.acquired_at or 'unknown'}")
        print(f"  Cost (USD): {_optional_usd(record.acquisition_cost)}")
        print(f"  Quantity: {record.quantity}")
        print(f"  Status: {record.status}")
        print(f"  Listed at (UTC): {record.listed_at or 'none'}")
        print(f"  Sold at (UTC): {record.sold_at or 'none'}")
        print(f"  Notes: {record.notes or 'none'}")
        print("Marketplace linkage")
        print(f"  Marketplace: {record.marketplace or 'none'}")
        print(f"  Listing/item ID: {record.marketplace_item_id or 'none'}")
        print(f"  SKU/custom label: {record.marketplace_sku or 'none'}")
        return 0
    except (
        InventoryValidationError,
        InventoryNotFoundError,
        AttachmentValidationError,
        AttachmentNotFoundError,
        ValuationValidationError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_ebay_command(args: argparse.Namespace) -> int:
    try:
        oauth = _seller_oauth()
        if args.ebay_command == "connect":
            state = oauth.new_state()
            url = oauth.authorization_url(state)
            print("Open this URL to authorize Flipper with eBay:")
            print(url)
            webbrowser.open(url)
            redirected_url = getpass(
                "Paste the full URL eBay redirected you to (input hidden): "
            ).strip()
            code = oauth.parse_redirect(redirected_url, state)
            oauth.exchange_code(code)
            print("eBay seller account connected. Refresh token stored in the OS credential store.")
            return 0
        if args.ebay_command == "disconnect":
            if oauth.disconnect():
                print("Local eBay seller authorization removed.")
            else:
                print("No local eBay seller authorization was stored.")
            return 0

        if args.ebay_command in {"listings", "sync-listings", "import-listing"}:
            listings = ActiveListingsClient(oauth).get_active_listings()
            store = InventoryStore(args.database)
            results = reconcile_listings(store, listings)
            if args.ebay_command == "listings":
                print("SKU | Item ID | Price | Qty | State | Title")
                for result in results:
                    listing = result.listing
                    price = _format_money(listing.asking_price)
                    quantity = (
                        listing.quantity_available
                        if listing.quantity_available is not None
                        else "-"
                    )
                    print(
                        f"{listing.sku or '-'} | {listing.item_id} | {price} | "
                        f"{quantity} | {result.state.value} | {listing.title}"
                    )
                counts = summarize_listings(results)
                print(
                    f"Summary: total={len(results)} "
                    f"matched={counts[ListingReconciliationState.MATCHED]} "
                    f"missing_local={counts[ListingReconciliationState.MISSING_LOCAL]} "
                    f"missing_sku={counts[ListingReconciliationState.MISSING_SKU]} "
                    f"invalid_sku={counts[ListingReconciliationState.INVALID_SKU]} "
                    f"conflicts={counts[ListingReconciliationState.CONFLICT]}"
                )
                return 0
            if args.ebay_command == "sync-listings":
                summary = sync_listings(store, results)
                print(
                    f"Synchronized {summary.updated} local item(s); "
                    f"{summary.conflicts} conflict(s) require "
                    "review; eBay was not modified."
                )
                return 0
            sku = args.sku.strip()
            matches = [result for result in results if result.listing.sku == sku]
            if len(matches) != 1:
                raise ValueError("listing cannot be imported safely; inspect 'ebay listings'")
            record, created = import_listing(
                store,
                results,
                item_id=matches[0].listing.item_id,
                sku=sku,
                source=args.source,
                acquired_at=args.acquired_at,
                acquisition_cost=args.cost,
            )
            print(
                f"{'Imported' if created else 'Already imported'} {record.inventory_id}; "
                "eBay was not modified."
            )
            return 0

        start, end = _order_date_range(args.start, args.end)
        if args.ebay_command in {"finances", "import-finances"}:
            if end - start > timedelta(days=1096):
                raise SellerOAuthError("Finance date range cannot exceed eBay's 36-month window")
            transactions = FinancesClient(oauth).get_transactions(start, end)
            store = InventoryStore(args.database)
            if args.ebay_command == "finances":
                _print_finances(transactions, store.list_sales())
            elif not _print_finance_import(transactions, store):
                return 1
            return 0
        if end - start > timedelta(days=730):
            raise SellerOAuthError("Order date range cannot exceed eBay's two-year history window")
        orders = FulfillmentClient(oauth).get_orders(start, end)
        if args.ebay_command == "reconcile":
            _print_reconciliation(orders, InventoryStore(args.database).list())
        elif args.ebay_command == "import-sales":
            if not _print_sale_import(orders, InventoryStore(args.database)):
                return 1
        else:
            _print_orders(orders)
        return 0
    except (
        SellerOAuthError,
        FulfillmentApiError,
        FinancesApiError,
        ActiveListingsApiError,
        ValueError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def run_sales_command(args: argparse.Namespace) -> int:
    store = InventoryStore(args.database)
    try:
        if args.sales_command == "add-cost":
            cost = store.add_sale_cost(
                args.sale_id,
                category=args.category,
                amount=args.amount,
                currency=args.currency,
                note=args.note,
                effect=args.effect,
            )
            print(
                f"Recorded {cost.cost_id} for {cost.sale_id}: {cost.category} "
                f"{_format_sale_money(cost.amount_minor, cost.amount_scale, cost.currency)} "
                f"(source: {cost.source})"
            )
            return 0
        if args.sales_command in {"confirm", "unconfirm"}:
            confirmed = args.sales_command == "confirm"
            store.set_reconciliation_confirmation(args.sale_id, args.category, confirmed=confirmed)
            action = "Confirmed" if confirmed else "Reset"
            print(f"{action} {args.category} reconciliation for {args.sale_id.upper()}")
            return 0
        if args.sales_command == "remove-cost":
            cost = store.remove_sale_cost(args.cost_id)
            print(f"Removed {cost.cost_id} from {cost.sale_id}")
            return 0
        if args.sales_command == "list":
            sales = store.list_sales()
            if not sales:
                print("No imported sales found.")
                return 0
            for sale in sales:
                gross = _format_sale_money(
                    sale.gross_amount_minor, sale.gross_amount_scale, sale.currency
                )
                print(
                    f"{sale.sale_id} | {sale.inventory_id} | {sale.marketplace} | "
                    f"{gross} | {sale.sold_at}"
                )
            return 0
        sale = store.get_sale(args.sale_id)
        gross = _format_sale_money(sale.gross_amount_minor, sale.gross_amount_scale, sale.currency)
        print(f"Sale {sale.sale_id}")
        print(f"  Inventory: {sale.inventory_id}")
        print(f"  Marketplace: {sale.marketplace}")
        print(f"  Order: {sale.external_order_id}")
        print(f"  Order line: {sale.external_line_item_id}")
        print(f"  SKU: {sale.marketplace_sku}")
        print(f"  Quantity: {sale.quantity}")
        if sale.item_revenue is not None:
            print(f"  Item revenue: {sale.currency} {sale.item_revenue}")
            shipping = (
                f"{sale.currency} {sale.buyer_shipping}"
                if sale.buyer_shipping is not None
                else "unknown"
            )
            print(f"  Buyer-paid shipping revenue: {shipping}")
        print(f"  Seller revenue: {gross}")
        if sale.marketplace_tax is not None:
            print(f"  Marketplace-collected tax (excluded): {sale.currency} {sale.marketplace_tax}")
        print(f"  Sold at: {sale.sold_at}")
        print(f"  Imported at: {sale.imported_at}")
        costs = store.list_sale_costs(sale.sale_id)
        print("Economics (recorded local data)")
        print(f"  Seller revenue: {gross}")
        inventory = store.get(sale.inventory_id)
        if sale.currency != "USD" or inventory.acquisition_cost is None:
            print("  Recorded profit: unavailable")
            reason = (
                "acquisition cost is unknown"
                if inventory.acquisition_cost is None
                else "acquisition cost is stored in USD and currency conversion is not supported"
            )
            print(f"  Reason: {reason}")
        else:
            economics = calculate_sale_economics(
                gross=sale.gross_amount,
                acquisition_cost=inventory.acquisition_cost,
                currency=sale.currency,
                acquisition_currency="USD",
                components=[
                    EconomicComponent(cost.category, cost.amount, cost.currency, cost.effect)
                    for cost in costs
                ],
            )
            labels = {
                "marketplace_fee": "Marketplace fees",
                "shipping_cost": "Shipping",
                "refund": "Refunds",
                "other_adjustment": "Other adjustments",
            }
            print(f"  Acquisition cost: -USD {economics.acquisition_cost:.2f}")
            for category in SALE_COST_TYPES:
                amount = economics.components_by_category.get(category, Decimal(0))
                print(f"  {labels[category]}: -USD {amount:.2f}")
            for category, amount in economics.increasing_by_category.items():
                print(f"  {labels[category]} credits: +USD {amount:.2f}")
            print(f"  Recorded realized profit: USD {economics.recorded_profit:.2f}")
        status, missing = store.reconciliation_status(sale.sale_id)
        print(f"  Reconciliation: {status}")
        print(f"  Missing/unconfirmed: {', '.join(missing) if missing else 'none'}")
        print("Cost components")
        if not costs:
            print("  None recorded (this does not mean zero costs)")
        for cost in costs:
            amount = _format_sale_money(cost.amount_minor, cost.amount_scale, cost.currency)
            note = f" | {cost.note}" if cost.note else ""
            external = (
                f" | eBay transaction: {cost.external_transaction_id}"
                if cost.external_transaction_id
                else ""
            )
            sign = "-" if cost.effect == "reduce" else "+"
            related = f" | reverses: {cost.related_cost_id}" if cost.related_cost_id else ""
            print(
                f"  {cost.cost_id} | {cost.category} | {sign}{amount} | "
                f"source: {cost.source}{external}{related}{note}"
            )
        return 0
    except (
        SaleNotFoundError,
        SaleCostNotFoundError,
        SaleCostValidationError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _report_date_range(args: argparse.Namespace) -> tuple[date | None, date | None]:
    start = args.start.date() if args.start else None
    end = args.end.date() if args.end else None
    if start is not None and end is not None and start > end:
        raise ValueError("report start date must be on or before end date")
    return start, end


def _report_money(currency: str, amount: Decimal) -> str:
    exact = format(amount, "f")
    if "." not in exact:
        exact += ".00"
    else:
        whole, fractional = exact.split(".", 1)
        exact = f"{whole}.{fractional.ljust(2, '0')}"
    return f"{currency} {exact}"


def _optional_usd(amount: Decimal | None) -> str:
    return "unknown" if amount is None else f"${amount:.2f}"


def _report_decimal(value: Decimal | None, *, suffix: str = "") -> str:
    return "unavailable" if value is None else f"{value:.2f}{suffix}"


def _print_inventory_report(report) -> None:
    print("Inventory Report")
    if not report.rows:
        print("No inventory items found in the selected acquisition-date range.")
    for row in report.rows:
        record = row.record
        print(
            f"{record.inventory_id} | {record.status} | "
            f"{_optional_usd(record.acquisition_cost)} | "
            f"marketplace: {record.marketplace or 'none'} | SKU: {record.marketplace_sku or 'none'}"
        )
        print(
            f"  acquired: {record.acquired_at or 'unknown'} | "
            f"listed: {record.listed_at or 'none'} | "
            f"sold: {record.sold_at or 'none'} | days held: "
            f"{row.days_held if row.days_held is not None else 'unavailable'} | "
            f"sale: {row.linked_sale_id or 'none'}"
        )


def _print_sales_report(report) -> None:
    print("Sales Report")
    if not report.rows:
        print("No sales found in the selected sold-date range.")
    for row in report.rows:
        sale = row.sale
        profit = (
            _report_money(sale.currency, row.economics.recorded_profit)
            if row.economics
            else "unavailable"
        )
        margin = (
            _report_decimal(row.margin * 100, suffix="%")
            if row.margin is not None
            else "unavailable"
        )
        print(
            f"{sale.sale_id} | {sale.inventory_id} | {sale.sold_at} | "
            f"gross: {_report_money(sale.currency, sale.gross_amount)} | "
            f"acquisition: {_optional_usd(row.acquisition_cost_usd)}"
        )
        print(
            f"  reducing: {_report_money(sale.currency, row.reducing_costs)} | "
            f"credits: {_report_money(sale.currency, row.increasing_credits)} | "
            f"recorded profit: {profit} | margin: {margin} | "
            f"reconciliation: {row.reconciliation_state}"
        )
        if row.missing_categories:
            print(f"  needs attention: {', '.join(row.missing_categories)}")


def _print_summary_report(report, *, start, end) -> None:
    inventory = report.inventory
    sales = report.sales
    print("Reseller Business Summary")
    print("Inventory snapshot (current state; not affected by sale date filters)")
    print(
        f"  Active: {inventory.active_count} "
        f"(acquired {inventory.status_counts.get('acquired', 0)}, "
        f"listed {inventory.status_counts.get('listed', 0)})"
    )
    print(
        f"  Sold: {inventory.status_counts.get('sold', 0)} | "
        f"archived: {inventory.status_counts.get('archived', 0)}"
    )
    print(
        "  Acquisition capital tied up: "
        + (
            f"USD {inventory.active_capital_usd:.2f}"
            if inventory.active_capital_usd is not None
            else "unavailable (unknown acquisition cost)"
        )
    )
    average_cost = inventory.average_active_acquisition_cost_usd
    print(f"  Average active acquisition cost: {_report_decimal(average_cost)}")
    listed_age = _report_decimal(inventory.average_listed_age_days, suffix=" days")
    print(f"  Average listed age: {listed_age}")
    oldest = inventory.oldest_active
    oldest_text = (
        f"{oldest.record.inventory_id} ({oldest.days_held} days)"
        if oldest is not None and oldest.days_held is not None
        else "unavailable"
    )
    print(f"  Oldest active item: {oldest_text}")
    range_text = f"{start or 'beginning'} through {end or 'present'}"
    print(f"Sales (sold_at: {range_text})")
    print(f"  Sales count: {len(sales.rows)}")
    for currency, amount in sorted(sales.gross_by_currency.items()):
        print(f"  Gross sales ({currency}): {_report_money(currency, amount)}")
    print(
        "  Acquisition cost / COGS (USD): "
        + (
            f"USD {sales.acquisition_cost_usd:.2f}"
            if sales.acquisition_cost_usd is not None
            else "unavailable (unknown acquisition cost)"
        )
    )
    for currency in sorted(set(sales.reducing_by_currency) | set(sales.increasing_by_currency)):
        print(
            f"  Recorded reducing costs ({currency}): "
            f"{_report_money(currency, sales.reducing_by_currency.get(currency, Decimal(0)))}"
        )
        print(
            f"  Recorded increasing credits ({currency}): "
            f"{_report_money(currency, sales.increasing_by_currency.get(currency, Decimal(0)))}"
        )
    for currency, amount in sorted(sales.recorded_profit_by_currency.items()):
        print(f"  Recorded realized profit ({currency}): {_report_money(currency, amount)}")
        reconciled_profit = sales.fully_reconciled_profit_by_currency.get(currency, Decimal(0))
        incomplete_profit = sales.incomplete_recorded_profit_by_currency.get(currency, Decimal(0))
        print(
            f"  Fully reconciled realized profit ({currency}): "
            f"{_report_money(currency, reconciled_profit)}"
        )
        print(
            f"  Incomplete recorded profit ({currency}): "
            f"{_report_money(currency, incomplete_profit)}"
        )
        margin = sales.aggregate_margin_by_currency[currency]
        print(
            f"  Aggregate recorded margin ({currency}): "
            f"{_report_decimal(margin * 100, suffix='%') if margin is not None else 'unavailable'}"
        )
    counts = sales.reconciliation_counts
    print(
        "  Reconciliation: "
        f"{counts.get('incomplete', 0)} incomplete | "
        f"{counts.get('partially_reconciled', 0)} partially reconciled | "
        f"{counts.get('fully_reconciled', 0)} fully reconciled"
    )
    print(f"  Average days held: {_report_decimal(sales.average_days_held)}")
    print(f"  Median days held: {_report_decimal(sales.median_days_held)}")


def _print_valuation_report(report) -> None:
    print("Valuation Accuracy Report")
    if not report.rows:
        print("No baseline valuation snapshots found.")
    for row in report.rows:
        comparison = row.comparison
        if comparison is None:
            reason = "unsold" if row.sale is None else "currency mismatch"
            print(f"{row.inventory.inventory_id} | comparison unavailable ({reason})")
            continue
        print(
            f"{row.inventory.inventory_id} | estimated resale: "
            f"{_report_money(comparison.currency, comparison.estimated_resale)} | actual gross: "
            f"{_report_money(comparison.currency, comparison.actual_gross)} | resale error: "
            f"{_report_money(comparison.currency, comparison.resale_error)}"
        )
        if comparison.fully_reconciled_actual_profit is not None:
            reconciled_profit = _report_money(
                comparison.currency, comparison.fully_reconciled_actual_profit
            )
            print(
                f"  fully reconciled actual profit: {reconciled_profit} | "
                "profit error: unavailable (gross estimate is not comparable)"
            )
        elif comparison.recorded_actual_profit is not None:
            print(
                "  recorded actual profit (incomplete): "
                f"{_report_money(comparison.currency, comparison.recorded_actual_profit)} | "
                "profit error: unavailable until fully reconciled"
            )
    print(f"Comparable sales: {report.comparable_count}")
    print(
        "Average absolute resale error: "
        + (
            _report_money(report.aggregate_currency, report.average_absolute_resale_error)
            if report.average_absolute_resale_error is not None
            else "unavailable"
        )
    )


def run_reports_command(args: argparse.Namespace) -> int:
    try:
        store = InventoryStore(args.database)
        if args.report_command == "valuation":
            _print_valuation_report(valuation_accuracy_report(store))
            return 0
        start, end = _report_date_range(args)
        today = _utc_now().date()
        if args.report_command == "summary":
            report = build_summary_report(store, today=today, start=start, end=end)
            _print_summary_report(report, start=start, end=end)
        elif args.report_command == "inventory":
            report = inventory_report(
                store.list(), store.list_sales(), today=today, start=start, end=end
            )
            _print_inventory_report(report)
        else:
            report = sales_report(
                store.list(),
                store.list_sales(),
                store.list_all_sale_costs(),
                store.list_reconciliation_confirmations(),
                store.list_sourcing_travel(),
                start=start,
                end=end,
            )
            _print_sales_report(report)
        return 0
    except (ValueError, RuntimeError, sqlite3.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command == "acquire":
        return run_acquisition_command(args)
    if args.command == "deals":
        return run_deals_command(args)
    if args.command == "ebay":
        return run_ebay_command(args)
    if args.command == "inventory":
        return run_inventory_command(args)
    if args.command == "sales":
        return run_sales_command(args)
    if args.command == "reports":
        return run_reports_command(args)
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
