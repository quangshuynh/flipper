from models import DealEvaluation, Listing, ParsedSpecs


def test_legacy_evaluation_aliases_remain_readable():
    """
    preserve readable aliases for existing evaluation fields
    :returns: None
    """
    deal = DealEvaluation(
        listing=Listing("1", "test", "PC", "", 100, ""),
        specs=ParsedSpecs(),
        distance_miles=None,
        estimated_market_value=200,
        asking_price=100,
        ideal_buy_price=136,
        expected_resale_value=190,
        estimated_gross_profit=90,
        estimated_roi=0.9,
        score=50,
    )

    assert deal.estimated_value == 200
    assert deal.ideal_sell_price == 190
    assert deal.estimated_profit == 90
