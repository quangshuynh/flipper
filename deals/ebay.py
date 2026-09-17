"""Normalize public eBay Browse responses into source-neutral opportunities."""

from __future__ import annotations

from datetime import datetime
from decimal import InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from deals.categories import DealCategory
from deals.models import CostComponent, DealOpportunity, Money, SourceIdentity

# US top-level category identifiers. Unknowns deliberately retain the explicit fallback.
EBAY_CATEGORY_MAP = {
    "6000": DealCategory.VEHICLE_PARTS,
    "293": DealCategory.ELECTRONICS,
    "1": DealCategory.COLLECTIBLES_ART,
    "11700": DealCategory.HOME_GARDEN,
    "11450": DealCategory.CLOTHING_SHOES_ACCESSORIES,
    "220": DealCategory.TOYS_HOBBIES,
    "888": DealCategory.SPORTING_GOODS,
    "267": DealCategory.BOOKS_MOVIES_MUSIC,
    "26395": DealCategory.HEALTH_BEAUTY,
    "12576": DealCategory.BUSINESS_INDUSTRIAL,
    "281": DealCategory.JEWELRY_WATCHES,
    "2984": DealCategory.BABY_ESSENTIALS,
    "1281": DealCategory.PET_SUPPLIES,
    "1305": DealCategory.TICKETS_TRAVEL,
    "99": DealCategory.EVERYTHING_ELSE,
    "10542": DealCategory.REAL_ESTATE,
    "172008": DealCategory.GIFT_CARDS_COUPONS,
    "316": DealCategory.SPECIALTY_SERVICES,
}


def map_ebay_category(category_id: str | None) -> DealCategory:
    return EBAY_CATEGORY_MAP.get(str(category_id or ""), DealCategory.EVERYTHING_ELSE)


def _money(container: Any) -> Money:
    if not isinstance(container, dict):
        raise ValueError("missing price")
    try:
        return Money.of(container["value"], container["currency"])
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid price") from exc


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or (host != "ebay.com" and not host.endswith(".ebay.com")):
        return None
    return value


def normalize_ebay_item(item: dict[str, Any]) -> tuple[DealOpportunity, CostComponent]:
    item_id = item.get("itemId")
    title = item.get("title")
    if (
        not isinstance(item_id, str)
        or not item_id
        or not isinstance(title, str)
        or not title.strip()
    ):
        raise ValueError("missing item identity or title")
    price = _money(item.get("price"))
    category_id = item.get("categoryId")
    location = item.get("itemLocation") if isinstance(item.get("itemLocation"), dict) else {}
    location_text = (
        ", ".join(
            str(value)
            for value in (
                location.get("city"),
                location.get("stateOrProvince"),
                location.get("country"),
            )
            if value
        )
        or None
    )
    observed = item.get("itemCreationDate") or item.get("itemOriginDate")
    observed_at = None
    if isinstance(observed, str):
        try:
            observed_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        except ValueError:
            pass
    shipping = CostComponent.unknown()
    options = item.get("shippingOptions")
    if isinstance(options, list) and options:
        try:
            shipping_money = _money(options[0].get("shippingCost"))
            shipping = CostComponent.estimated(shipping_money.amount, shipping_money.currency)
        except (AttributeError, ValueError):
            pass
    opportunity = DealOpportunity(
        source=SourceIdentity.EBAY,
        source_listing_id=item_id,
        title=title.strip(),
        category=map_ebay_category(str(category_id) if category_id is not None else None),
        base_price=price,
        url=_safe_url(item.get("itemWebUrl") or item.get("itemAffiliateWebUrl")),
        location_text=location_text,
        condition=str(item["condition"]).strip() if item.get("condition") else None,
        observed_at=observed_at,
        category_attributes={"ebay_category_id": str(category_id)} if category_id else {},
    )
    return opportunity, shipping


def normalize_search_results(
    body: dict[str, Any],
) -> tuple[tuple[DealOpportunity, CostComponent], ...]:
    items = body.get("itemSummaries", [])
    if not isinstance(items, list):
        return ()
    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            results.append(normalize_ebay_item(item))
        except ValueError:
            continue
    return tuple(results)
