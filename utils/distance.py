"""
Distance utilities.
"""

from typing import Optional
from geopy.distance import geodesic


def compute_distance_miles(
    home_lat: float,
    home_lon: float,
    listing_lat: Optional[float],
    listing_lon: Optional[float]
) -> Optional[float]:
    """
    Compute geodesic distance in miles.

    :param home_lat: Home latitude.
    :param home_lon: Home longitude.
    :param listing_lat: Listing latitude.
    :param listing_lon: Listing longitude.
    :returns: Distance in miles or None.
    """
    if listing_lat is None or listing_lon is None:
        return None

    return round(
        geodesic((home_lat, home_lon), (listing_lat, listing_lon)).miles,
        2
    )