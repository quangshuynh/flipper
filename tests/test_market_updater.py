from pricing.market_updater import extract_prices, robust_market_price


def test_market_price_filters_bad_items_and_trims_outliers():
    """
    filter unusable market items and trim price outliers
    :returns: None
    """
    items = [
        {"title": "RTX 3060", "price": {"value": str(value)}}
        for value in [100, 200, 210, 220, 230, 1000]
    ]
    items.append({"title": "RTX 3060 broken for parts", "price": {"value": "50"}})

    prices = extract_prices(items)
    market, low, high, sample_size = robust_market_price(prices)

    assert prices == [100, 200, 210, 220, 230, 1000]
    assert market == 215
    assert low == 200
    assert high == 230
    assert sample_size == 4


def test_empty_market_data_is_explicit():
    """
    represent empty market data with zero values
    :returns: None
    """
    assert robust_market_price([]) == (0.0, 0.0, 0.0, 0)
