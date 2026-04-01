"""
Main entry point for Flipper MVP.
"""

import os
from dotenv import load_dotenv

from collectors.json_feed_collector import fetch_listings
from parser.extractor import extract_specs
from parser.ai_enricher import enrich_specs_with_ai
from pricing.estimator import estimate_market_value, calculate_pricing, score_deal
from notifier.discord_notifier import send_deal_to_discord
from utils.distance import compute_distance_miles
from utils.dedupe import init_db, has_seen, mark_seen
from models import DealEvaluation


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
        raise ValueError("DISCORD_WEBHOOK_URL is missing from environment variables.")

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
            title=listing.title,
            description=listing.description,
            base_specs=base_specs
        )

        print(f"AI summary: {ai_summary}")

        distance_miles = compute_distance_miles(
            home_lat=home_lat,
            home_lon=home_lon,
            listing_lat=listing.latitude,
            listing_lon=listing.longitude
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
        ideal_buy, ideal_sell = calculate_pricing(estimated_value)

        score, profit = score_deal(
            asking_price=listing.price,
            estimated_value=estimated_value,
            specs=specs,
            distance_miles=distance_miles
        )

        deal = DealEvaluation(
            listing=listing,
            specs=specs,
            distance_miles=distance_miles,
            estimated_value=estimated_value,
            ideal_buy_price=ideal_buy,
            ideal_sell_price=ideal_sell,
            estimated_profit=profit,
            score=score
        )

        print(
            f"[{listing.listing_id}] "
            f"listing_price=${listing.price:.2f}, "
            f"estimated_value=${estimated_value:.2f}, "
            f"ideal_buy=${ideal_buy:.2f}, "
            f"ideal_sell=${ideal_sell:.2f}, "
            f"profit=${profit:.2f}, "
            f"score={score}"
        )

        if profit >= min_profit and score >= min_score:
            print(
                f"Listing PASSED filters "
                f"(profit {profit:.2f} >= {min_profit:.2f}, score {score} >= {min_score})"
            )

            success = send_deal_to_discord(
                webhook_url=webhook_url,
                deal=deal,
                ai_summary=ai_summary
            )

            if success:
                print(f"Sent deal alert for {listing.listing_id}")
                mark_seen(listing.listing_id)
            else:
                print(f"Failed to send alert for {listing.listing_id}")
                print("Not marking as seen so it can retry next run.")
        else:
            print(
                f"Listing FAILED filters "
                f"(profit {profit:.2f} < {min_profit:.2f} "
                f"or score {score} < {min_score})"
            )
            mark_seen(listing.listing_id)


if __name__ == "__main__":
    run()