import sqlite3

from models import ParsedSpecs
from pricing import estimator


def test_known_components_and_listing_price_sensitive_economics():
    """
    make gross profit and ROI respond to asking price
    :returns: None
    """
    specs = ParsedSpecs(gpu="RTX 3060", cpu="Ryzen 5 5600X", ram="16 GB DDR4")
    value = estimator.estimate_market_value(specs)
    cheaper = estimator.calculate_pricing_result(value, 400)
    expensive = estimator.calculate_pricing_result(value, 700)

    assert value > 0
    assert cheaper.estimated_gross_profit > expensive.estimated_gross_profit
    assert cheaper.estimated_roi > expensive.estimated_roi


def test_unknown_component_uses_low_information_fallback():
    """
    use low-information fallback values for unknown components
    :returns: None
    """
    specs = ParsedSpecs(gpu="Mystery GPU", cpu="Mystery CPU", ram="Mystery RAM")
    assert estimator.estimate_market_value(specs) == 125


def test_dynamic_market_price_overrides_static_value(tmp_path, monkeypatch):
    """
    prefer a matching dynamic market price over a static value
    :param tmp_path: pytest temporary directory fixture
    :param monkeypatch: pytest monkeypatch fixture
    :returns: None
    """
    database = tmp_path / "prices.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE part_prices (part_name TEXT PRIMARY KEY, market_price REAL)"
        )
        connection.execute("INSERT INTO part_prices VALUES ('RTX 3060', 999)")
    monkeypatch.setattr(estimator, "DB_PATH", str(database))

    specs = ParsedSpecs(gpu="RTX 3060")
    assert estimator.estimate_market_value(specs) == 1039


def test_zero_or_invalid_listing_price_has_no_roi():
    """
    handle zero and invalid asking prices without an ROI
    :returns: None
    """
    result = estimator.calculate_pricing_result(500, 0)
    invalid = estimator.calculate_pricing_result(500, "bad")

    assert result.estimated_roi is None
    assert invalid.asking_price == 0
    assert invalid.estimated_gross_profit == result.expected_resale_value


def test_score_caps_at_one_hundred_and_ignores_seller_knowledge():
    """
    cap scores and ignore seller knowledge as an economic factor
    :returns: None
    """
    specs = ParsedSpecs(
        gpu="RTX 4090", cpu="i9-14900K", ram="32 GB DDR5", flags=["low_knowledge_seller"]
    )
    without_flag = ParsedSpecs(gpu=specs.gpu, cpu=specs.cpu, ram=specs.ram)

    flagged = estimator.score_deal(400, 2000, specs, 5)
    plain = estimator.score_deal(400, 2000, without_flag, 5)

    assert flagged == plain
    assert flagged[0] <= 100
