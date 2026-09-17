"""Deterministic reconciliation of active listings to local Q inventory."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum

from ebay.listings import EbayActiveListing
from inventory.store import InventoryRecord, q_number_value


class ListingReconciliationState(str, Enum):
    MATCHED = "MATCHED"
    MISSING_LOCAL = "MISSING_LOCAL"
    MISSING_SKU = "MISSING_SKU"
    INVALID_SKU = "INVALID_SKU"
    CONFLICT = "CONFLICT"


@dataclass(frozen=True)
class ListingReconciliation:
    listing: EbayActiveListing
    state: ListingReconciliationState
    inventory: InventoryRecord | None = None
    reason: str | None = None


def reconcile_active_listings(
    listings: list[EbayActiveListing], inventory: list[InventoryRecord]
) -> list[ListingReconciliation]:
    """Match only exact Q-number SKUs and durable eBay identifiers."""
    by_q = {item.inventory_id: item for item in inventory}
    sku_counts = Counter(listing.sku for listing in listings if listing.sku)
    id_counts = Counter(listing.item_id for listing in listings)
    by_item_id: dict[str, list[InventoryRecord]] = {}
    for item in inventory:
        if item.marketplace and item.marketplace.casefold() == "ebay" and item.marketplace_item_id:
            by_item_id.setdefault(item.marketplace_item_id, []).append(item)

    results = []
    for listing in listings:
        if listing.sku is None:
            results.append(ListingReconciliation(listing, ListingReconciliationState.MISSING_SKU))
            continue
        if q_number_value(listing.sku) is None:
            results.append(ListingReconciliation(listing, ListingReconciliationState.INVALID_SKU))
            continue
        item = by_q.get(listing.sku)
        if sku_counts[listing.sku] > 1 or id_counts[listing.item_id] > 1:
            results.append(
                ListingReconciliation(
                    listing,
                    ListingReconciliationState.CONFLICT,
                    item,
                    "duplicate active identifier",
                )
            )
            continue
        linked = by_item_id.get(listing.item_id, [])
        if item is None:
            reason = "listing item ID is linked to another Q-number" if linked else None
            state = (
                ListingReconciliationState.CONFLICT
                if linked
                else ListingReconciliationState.MISSING_LOCAL
            )
            results.append(ListingReconciliation(listing, state, reason=reason))
            continue
        disagreement = (
            (item.marketplace is not None and item.marketplace.casefold() != "ebay")
            or (item.marketplace_sku is not None and item.marketplace_sku != listing.sku)
            or (
                item.marketplace_item_id is not None and item.marketplace_item_id != listing.item_id
            )
            or any(other.inventory_id != item.inventory_id for other in linked)
            or item.status in {"sold", "archived"}
        )
        if disagreement:
            results.append(
                ListingReconciliation(
                    listing,
                    ListingReconciliationState.CONFLICT,
                    item,
                    "durable linkage or lifecycle disagrees",
                )
            )
        else:
            results.append(ListingReconciliation(listing, ListingReconciliationState.MATCHED, item))
    return results


def summarize(results: list[ListingReconciliation]) -> dict[ListingReconciliationState, int]:
    counts = Counter(result.state for result in results)
    return {state: counts[state] for state in ListingReconciliationState}
