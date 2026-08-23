from models import DealEvaluation, Listing, ParsedSpecs
from notifier.discord_notifier import build_embed, send_deal_to_discord


def make_deal():
    """
    create a representative evaluated deal
    :returns: evaluated deal
    """
    return DealEvaluation(
        listing=Listing("1", "test", "PC", "", 400, "https://example.com"),
        specs=ParsedSpecs(gpu="RTX 3060"),
        distance_miles=5,
        estimated_market_value=600,
        asking_price=400,
        ideal_buy_price=408,
        expected_resale_value=570,
        estimated_gross_profit=170,
        estimated_roi=0.425,
        score=80,
    )


def test_embed_reports_gross_profit_and_roi():
    """
    include gross profit and ROI in the Discord embed
    :returns: None
    """
    payload = build_embed(make_deal())
    assert "Gross Profit" in payload["description"]
    assert "$170.00" in payload["description"]
    assert "42.5%" in payload["description"]


def test_discord_failure_is_optional(monkeypatch):
    """
    return failure instead of raising when Discord is unavailable
    :param monkeypatch: pytest monkeypatch fixture
    :returns: None
    """

    def fail(*args, **kwargs):
        """
        raise a representative network failure
        :param args: positional request arguments
        :param kwargs: keyword request arguments
        :returns: never returns
        """
        raise __import__("requests").RequestException("offline")

    monkeypatch.setattr("notifier.discord_notifier.requests.post", fail)
    assert send_deal_to_discord("https://discord.com/api/webhooks/test", make_deal()) is False
