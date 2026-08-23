"""
Sample collector that returns hardcoded listings.
Replace this with a real source collector later.
"""

from typing import List
from models import Listing


def fetch_listings() -> List[Listing]:
    """
    Return sample listings for local testing.

    :returns: List of Listing objects.
    """
    return [
        Listing(
            listing_id="fb_001",
            source="sample",
            title="Gaming PC RTX 3060 Ryzen 5",
            description=(
                "Gaming PC for sale. RTX 3060, Ryzen 5 5600X, 16GB RAM, "
                "1TB SSD, Windows 11. Comes with monitor and keyboard. "
                "Don't know much about computers, just want gone."
            ),
            price=400,
            url="https://example.com/listing/1",
            latitude=43.1566,
            longitude=-77.6088,
            location_text="Rochester, NY",
        ),
        Listing(
            listing_id="fb_002",
            source="sample",
            title="Custom Desktop",
            description=(
                "Custom desktop with i7-10700k, RTX 3070, 32GB ram, "
                "2TB NVMe, 750W PSU, B550 motherboard, AIO cooler, "
                "RGB case, Windows 11 Pro, includes mouse."
            ),
            price=850,
            url="https://example.com/listing/2",
            latitude=43.0481,
            longitude=-76.1474,
            location_text="Syracuse, NY",
        ),
        Listing(
            listing_id="fb_003",
            source="sample",
            title="PC works great",
            description=(
                "PC works great. GTX 1660 Super, Ryzen 5 3600, 16gb ram. "
                "Not sure what storage it has. Includes monitor, mouse, keyboard."
            ),
            price=500,
            url="https://example.com/listing/3",
            latitude=42.8864,
            longitude=-78.8784,
            location_text="Buffalo, NY",
        ),
    ]
