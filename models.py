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
    :param estimated_value: Estimated market value.
    :param ideal_buy_price: Best target buy price.
    :param ideal_sell_price: Good target sell price.
    :param estimated_profit: Estimated profit.
    :param score: Deal score.
    :returns: DealEvaluation object.
    """
    listing: Listing
    specs: ParsedSpecs
    distance_miles: Optional[float]
    estimated_value: float
    ideal_buy_price: float
    ideal_sell_price: float
    estimated_profit: float
    score: int