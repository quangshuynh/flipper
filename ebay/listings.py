"""Minimal, PII-free model for active eBay seller listings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from ebay.orders import Money


class ListingResponseError(RuntimeError):
    """eBay returned an unusable active-listing representation."""


@dataclass(frozen=True)
class EbayActiveListing:
    item_id: str
    sku: str | None
    title: str
    status: str
    asking_price: Money | None
    quantity_available: int | None
    listing_url: str | None = None
    started_at: datetime | None = None
    ends_at: datetime | None = None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _date(value: Any) -> datetime | None:
    value = _text(value)
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ListingResponseError("eBay returned a malformed listing date") from exc


def _safe_listing_url(value: Any) -> str | None:
    value = _text(value)
    if value is None:
        return None
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or not (hostname == "ebay.com" or hostname.endswith(".ebay.com")):
        return None
    return value


def normalize_listing(raw: dict[str, Any]) -> EbayActiveListing:
    """Normalize selected Trading API fields and discard the rest."""
    item_id = _text(raw.get("item_id"))
    title = _text(raw.get("title"))
    status = _text(raw.get("status"))
    if not item_id or not title or not status:
        raise ListingResponseError("eBay listing is missing a required field")
    price = None
    if raw.get("price_value") is not None or raw.get("price_currency") is not None:
        currency = _text(raw.get("price_currency"))
        try:
            value = Decimal(str(raw["price_value"]))
        except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
            raise ListingResponseError("eBay returned a malformed listing price") from exc
        if not value.is_finite() or value < 0 or not currency:
            raise ListingResponseError("eBay returned a malformed listing price")
        price = Money(value, currency)
    quantity = raw.get("quantity_available")
    if quantity is not None:
        try:
            quantity = int(quantity)
        except (TypeError, ValueError) as exc:
            raise ListingResponseError("eBay returned a malformed listing quantity") from exc
        if quantity < 0:
            raise ListingResponseError("eBay returned a malformed listing quantity")
    return EbayActiveListing(
        item_id=item_id,
        sku=_text(raw.get("sku")),
        title=title,
        status=status,
        asking_price=price,
        quantity_available=quantity,
        listing_url=_safe_listing_url(raw.get("listing_url")),
        started_at=_date(raw.get("started_at")),
        ends_at=_date(raw.get("ends_at")),
    )
