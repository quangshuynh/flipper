"""Validation and normalization for user-entered opportunities."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from deals.categories import normalize_category
from deals.models import (
    CostComponent,
    DealOpportunity,
    EvidenceProvenance,
    Money,
    ProvenanceKind,
    SourceIdentity,
)

MAX_TITLE_LENGTH = 300
MAX_LOCATION_LENGTH = 200
MAX_NOTES_LENGTH = 2_000
MAX_URL_LENGTH = 2_000
MANUAL_CONDITIONS = (
    "new",
    "open_box",
    "used_tested",
    "used_untested",
    "for_parts",
    "unknown",
)
USER_SOURCE_PROVENANCE = EvidenceProvenance(
    ProvenanceKind.USER_SOURCE_FACT, "User-provided source fact"
)


def _bounded(value: str | None, name: str, limit: int, *, required: bool = False) -> str | None:
    normalized = value.strip() if value else None
    if required and not normalized:
        raise ValueError(f"{name} is required")
    if normalized and len(normalized) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return normalized


def validate_reference_url(value: str | None) -> str | None:
    url = _bounded(value, "source URL", MAX_URL_LENGTH)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("source URL must be an HTTPS URL without credentials")
    return url


@dataclass(frozen=True)
class ManualOpportunity:
    opportunity: DealOpportunity
    inbound_shipping: CostComponent
    notes: str | None = None


def create_manual_opportunity(fields: dict[str, str]) -> ManualOpportunity:
    """Create source-neutral inputs without contacting the supplied URL."""
    try:
        source = SourceIdentity(fields.get("source", ""))
    except ValueError as exc:
        raise ValueError("source must be a recognized opportunity source") from exc
    category = normalize_category(fields.get("category", ""))
    title = _bounded(fields.get("title"), "title", MAX_TITLE_LENGTH, required=True)
    location = _bounded(fields.get("location_text"), "location", MAX_LOCATION_LENGTH)
    notes = _bounded(fields.get("notes"), "notes", MAX_NOTES_LENGTH)
    condition = fields.get("condition", "").strip()
    if condition not in MANUAL_CONDITIONS:
        raise ValueError("condition must be a recognized value")
    currency = fields.get("currency", "USD")
    try:
        price = Money.of(fields.get("base_price", ""), currency)
    except (TypeError, ValueError) as exc:
        raise ValueError("asking price must be an exact decimal amount") from exc
    if price.amount < 0:
        raise ValueError("asking price cannot be negative")
    shipping_value = fields.get("inbound_shipping", "").strip()
    shipping = CostComponent.unknown()
    if shipping_value:
        try:
            shipping = CostComponent.estimated(
                shipping_value, currency, provenance=USER_SOURCE_PROVENANCE
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("inbound shipping must be an exact nonnegative amount") from exc
    opportunity = DealOpportunity(
        source=source,
        source_listing_id=None,
        title=title or "",
        category=category,
        base_price=price,
        url=validate_reference_url(fields.get("source_url")),
        location_text=location,
        condition=condition,
        base_price_provenance=USER_SOURCE_PROVENANCE,
        category_provenance=USER_SOURCE_PROVENANCE,
    )
    return ManualOpportunity(opportunity, shipping, notes)
