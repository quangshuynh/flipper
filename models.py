"""
Core data models for Flipper.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Listing:
    """
    Raw listing collected from a source.

    :param listing_id: Unique ID from source.
    :param source: Source name, like facebook or craigslist.
    :param title: Listing title.
    :param description: Listing description/body text.
    :param price: Asking price in USD.
    :param url: Direct link to listing.
    :param latitude: Latitude of listing, if known.
    :param longitude: Longitude of listing, if known.
    :param location_text: Human-readable location string.
    :returns: Listing object.
    """

    listing_id: str
    source: str
    title: str
    description: str
    price: float
    url: str
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_text: str = ""


@dataclass
class ParsedSpecs:
    """
    Structured computer specs extracted from a listing.

    :param gpu: GPU model or 'not listed'.
    :param cpu: CPU model or 'not listed'.
    :param ram: RAM amount or 'not listed'.
    :param storage: Storage info or 'not listed'.
    :param psu: PSU info or 'not listed'.
    :param motherboard: Motherboard info or 'not listed'.
    :param case: Case info or 'not listed'.
    :param cpu_cooler: CPU cooler info or 'not listed'.
    :param os: Operating system or 'not listed'.
    :param extras: Extra included items.
    :param flags: Seller/deal signals.
    :returns: ParsedSpecs object.
    """

    gpu: str = "not listed"
    cpu: str = "not listed"
    ram: str = "not listed"
    storage: str = "not listed"
    psu: str = "not listed"
    motherboard: str = "not listed"
    case: str = "not listed"
    cpu_cooler: str = "not listed"
    os: str = "not listed"
    extras: List[str] = field(default_factory=list)
    flags: List[str] = field(default_factory=list)


@dataclass
class DealEvaluation:
    """
    Final evaluated deal.

    :param listing: Raw listing object.
    :param specs: Parsed specs.
    :param distance_miles: Distance from home base.
    :param estimated_market_value: Estimated market value.
    :param asking_price: Seller asking price.
    :param ideal_buy_price: Best target buy price.
    :param expected_resale_value: Expected resale value after a conservative adjustment.
    :param estimated_gross_profit: Expected resale value minus asking price.
    :param estimated_roi: Gross profit divided by asking price, when valid.
    :param score: Deal score.
    :returns: DealEvaluation object.
    """

    listing: Listing
    specs: ParsedSpecs
    distance_miles: Optional[float]
    estimated_market_value: float
    asking_price: float
    ideal_buy_price: float
    expected_resale_value: float
    estimated_gross_profit: float
    estimated_roi: Optional[float]
    score: int
    reasons: List[str] = field(default_factory=list)

    @property
    def estimated_value(self) -> float:
        """
        return the backward-compatible market value alias
        :returns: estimated market value
        """
        return self.estimated_market_value

    @property
    def ideal_sell_price(self) -> float:
        """
        return the backward-compatible resale value alias
        :returns: expected resale value
        """
        return self.expected_resale_value

    @property
    def estimated_profit(self) -> float:
        """
        return the backward-compatible gross profit alias
        :returns: estimated gross profit
        """
        return self.estimated_gross_profit
