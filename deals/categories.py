"""Stable deal-category identities and display labels."""

from __future__ import annotations

import re
from enum import Enum


class DealCategory(Enum):
    """Representable categories; membership does not imply pricing support."""

    VEHICLE_PARTS = ("vehicle-parts", "Vehicle Parts")
    ELECTRONICS = ("electronics", "Electronics")
    COLLECTIBLES_ART = ("collectibles-art", "Collectibles & Art")
    HOME_GARDEN = ("home-garden", "Home & Garden")
    CLOTHING_SHOES_ACCESSORIES = (
        "clothing-shoes-accessories",
        "Clothing, Shoes & Accessories",
    )
    TOYS_HOBBIES = ("toys-hobbies", "Toys & Hobbies")
    SPORTING_GOODS = ("sporting-goods", "Sporting Goods")
    BOOKS_MOVIES_MUSIC = ("books-movies-music", "Books, Movies & Music")
    HEALTH_BEAUTY = ("health-beauty", "Health & Beauty")
    BUSINESS_INDUSTRIAL = ("business-industrial", "Business & Industrial")
    JEWELRY_WATCHES = ("jewelry-watches", "Jewelry & Watches")
    BABY_ESSENTIALS = ("baby-essentials", "Baby Essentials")
    PET_SUPPLIES = ("pet-supplies", "Pet Supplies")
    TICKETS_TRAVEL = ("tickets-travel", "Tickets & Travel")
    EVERYTHING_ELSE = ("everything-else", "Everything Else")
    REAL_ESTATE = ("real-estate", "Real Estate")
    GIFT_CARDS_COUPONS = ("gift-cards-coupons", "Gift Cards & Coupons")
    SPECIALTY_SERVICES = ("specialty-services", "Specialty Services")

    def __init__(self, slug: str, label: str) -> None:
        self.slug = slug
        self.label = label


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


_CATEGORY_LOOKUP = {
    key: category
    for category in DealCategory
    for key in {_key(category.slug), _key(category.label), _key(category.name)}
}
_CATEGORY_LOOKUP.update({"computers": DealCategory.ELECTRONICS, "pc": DealCategory.ELECTRONICS})


def normalize_category(value: str | DealCategory) -> DealCategory:
    """Normalize a stable slug, display label, enum name, or documented PC alias."""
    if isinstance(value, DealCategory):
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("category must be a non-empty string or DealCategory")
    try:
        return _CATEGORY_LOOKUP[_key(value)]
    except KeyError as exc:
        raise ValueError(f"unknown deal category: {value}") from exc
