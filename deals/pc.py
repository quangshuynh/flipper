"""Compatibility adapter from the existing PC analyzer into the generalized core."""

from decimal import Decimal

from deals.categories import DealCategory
from deals.economics import calculate_economics
from deals.models import CostComponent, DealOpportunity, Money, SourceIdentity
from models import DealEvaluation as PcDealEvaluation


def adapt_pc_deal(deal: PcDealEvaluation) -> tuple[DealOpportunity, object]:
    """Preserve legacy PC calculations while exposing their known generalized dimensions."""
    source = next(
        (item for item in SourceIdentity if item.value == deal.listing.source.casefold()),
        SourceIdentity.OTHER,
    )
    opportunity = DealOpportunity(
        source=source,
        source_listing_id=deal.listing.listing_id,
        title=deal.listing.title,
        category=DealCategory.ELECTRONICS,
        base_price=Money.of(str(deal.asking_price)),
        url=deal.listing.url or None,
        location_text=deal.listing.location_text or None,
        category_attributes={
            "cpu": deal.specs.cpu,
            "gpu": deal.specs.gpu,
            "ram": deal.specs.ram,
            "storage": deal.specs.storage,
        },
    )
    economics = calculate_economics(
        base_price=opportunity.base_price,
        expected_resale=CostComponent.estimated(Decimal(str(deal.expected_resale_value))),
    )
    return opportunity, economics
