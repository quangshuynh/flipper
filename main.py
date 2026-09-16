"""Main entry point for Flipper."""

import argparse
import os
import sqlite3
import sys
import webbrowser
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from getpass import getpass

from dotenv import load_dotenv

from collectors.json_feed_collector import fetch_listings
from ebay.fulfillment import FulfillmentApiError, FulfillmentClient
from ebay.orders import EbayOrder, Money
from ebay.seller_oauth import SellerOAuthClient, SellerOAuthConfig, SellerOAuthError
from inventory.store import (
    VALID_STATUSES,
    InventoryNotFoundError,
    InventoryStore,
    InventoryValidationError,
)
from models import DealEvaluation
from parser.ai_enricher import enrich_specs_with_ai
from notifier.discord_notifier import send_deal_to_discord
from parser.extractor import extract_specs
from pricing.estimator import calculate_pricing_result, estimate_market_value, score_deal
from utils.dedupe import init_db, has_seen, mark_seen
from utils.distance import compute_distance_miles


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

        base_specs = extract_specs(listing.title, listing.description)

        specs, ai_summary = enrich_specs_with_ai(
            title=listing.title, description=listing.description, base_specs=base_specs
        )

        print(f"AI summary: {ai_summary}")

        distance_miles = compute_distance_miles(
            home_lat=home_lat,
            home_lon=home_lon,
            listing_lat=listing.latitude,
            listing_lon=listing.longitude,
        )

        print(f"Distance: {distance_miles}")

        if distance_miles is not None and distance_miles > max_radius_miles:
            print(
                f"Skipping {listing.listing_id}: "
                f"out of radius ({distance_miles:.1f} mi > {max_radius_miles:.1f} mi)"
            )
            mark_seen(listing.listing_id)
            continue

        estimated_value = estimate_market_value(specs)
        pricing = calculate_pricing_result(estimated_value, listing.price)
        score, profit = score_deal(
            asking_price=listing.price,
            estimated_value=estimated_value,
            specs=specs,
            distance_miles=distance_miles,
        )
        deal = DealEvaluation(
            listing=listing,
            specs=specs,
            distance_miles=distance_miles,
            estimated_market_value=estimated_value,
            asking_price=listing.price,
            ideal_buy_price=pricing.ideal_buy_price,
            expected_resale_value=pricing.expected_resale_value,
            estimated_gross_profit=profit,
            estimated_roi=pricing.estimated_roi,
            score=score,
        )
        print(
            f"[{listing.listing_id}] listing_price=${listing.price:.2f}, "
            f"estimated_value=${estimated_value:.2f}, ideal_buy=${pricing.ideal_buy_price:.2f}, "
            f"expected_resale=${pricing.expected_resale_value:.2f}, gross_profit=${profit:.2f}, "
            f"score={score}"
        )
        if profit >= min_profit and score >= min_score:
            print(
                f"Listing PASSED filters (profit {profit:.2f} >= {min_profit:.2f}, "
                f"score {score} >= {min_score})"
            )
            if not webhook_url:
                print("Alert skipped because Discord is not configured.")
                continue
            success = send_deal_to_discord(
                webhook_url=webhook_url, deal=deal, ai_summary=ai_summary
            )
            if success:
                print(f"Sent deal alert for {listing.listing_id}")
                mark_seen(listing.listing_id)
            else:
                print(f"Failed to send alert for {listing.listing_id}")
                print("Not marking as seen so it can retry next run.")
        else:
            print(
                f"Listing FAILED filters (profit {profit:.2f} < {min_profit:.2f} "
                f"or score {score} < {min_score})"
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
    ebay = commands.add_parser("ebay", help="eBay seller integration")
    ebay_commands = ebay.add_subparsers(dest="ebay_command", required=True)
    ebay_commands.add_parser("connect", help="authorize a seller account")
    ebay_commands.add_parser("disconnect", help="remove local seller authorization")
    orders = ebay_commands.add_parser("orders", help="show recent seller orders")
    orders.add_argument("--from", dest="start", type=_parse_date, help="start date (YYYY-MM-DD)")
    orders.add_argument("--to", dest="end", type=_parse_date, help="end date (YYYY-MM-DD)")
    inventory = commands.add_parser("inventory", help="manage local inventory")
    inventory.add_argument("--database", default=INVENTORY_DB_PATH, help=argparse.SUPPRESS)
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
    return parser


def run_inventory_command(args: argparse.Namespace) -> int:
    store = InventoryStore(args.database)
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
                    f"${record.acquisition_cost:.2f} | {record.title}"
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
                f"${record.acquisition_cost:.2f} | {record.title}"
            )
            return 0

        if args.inventory_command == "status":
            record = store.transition_status(args.inventory_id, args.status)
            print(f"Inventory item {record.inventory_id}: {record.status}")
            return 0

        record = store.get(args.inventory_id)
        print(f"{record.inventory_id}: {record.title}")
        print("Acquisition")
        print(f"  Source: {record.source}")
        print(f"  Date: {record.acquired_at}")
        print(f"  Cost (USD): ${record.acquisition_cost:.2f}")
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
    except (InventoryValidationError, InventoryNotFoundError, RuntimeError, sqlite3.Error) as exc:
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

        start, end = _order_date_range(args.start, args.end)
        if end - start > timedelta(days=730):
            raise SellerOAuthError("Order date range cannot exceed eBay's two-year history window")
        orders = FulfillmentClient(oauth).get_orders(start, end)
        _print_orders(orders)
        return 0
    except (SellerOAuthError, FulfillmentApiError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    if args.command == "ebay":
        return run_ebay_command(args)
    if args.command == "inventory":
        return run_inventory_command(args)
    run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
