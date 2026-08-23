from collectors.json_feed_collector import _build_listing
from parser.extractor import extract_specs
from pricing.estimator import calculate_pricing_result, estimate_market_value, score_deal


def test_listing_to_valuation_to_scoring_pipeline():
    """
    process a representative listing through valuation and scoring
    :returns: None
    """
    listing = _build_listing(
        {
            "id": "fixture",
            "title": "RTX 3060 PC",
            "description": "Ryzen 5 5600X, 16GB RAM",
            "price": 300,
        },
        0,
    )
    specs = extract_specs(listing.title, listing.description)
    value = estimate_market_value(specs)
    pricing = calculate_pricing_result(value, listing.price)
    score, profit = score_deal(listing.price, value, specs, None)

    assert pricing.estimated_gross_profit == profit
    assert score > 0
    assert pricing.expected_resale_value > listing.price
