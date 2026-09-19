"""Shared active-listing workflows for the CLI and server-rendered web UI."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ebay.listing_reconciliation import (
    ListingReconciliation,
    ListingReconciliationState,
    reconcile_active_listings,
)
from ebay.listings import EbayActiveListing
from inventory.store import InventoryRecord, InventoryStore


class ListingWorkflowError(ValueError):
    """A requested listing operation cannot be performed safely."""


@dataclass(frozen=True)
class ListingSyncSummary:
    checked: int
    already_synchronized: int
    updated: int
    conflicts: int


def reconcile_listings(
    store: InventoryStore, listings: list[EbayActiveListing]
) -> list[ListingReconciliation]:
    """Reconcile a live, in-memory listing snapshot with authoritative local records."""
    return reconcile_active_listings(listings, store.list())


def sync_listings(
    store: InventoryStore, results: list[ListingReconciliation]
) -> ListingSyncSummary:
    """Apply only safe local linkage and acquired-to-listed changes."""
    updated = 0
    already = 0
    for result in results:
        if result.state is ListingReconciliationState.MATCHED and result.inventory:
            _, changed = store.sync_ebay_listing(
                result.inventory.inventory_id,
                marketplace_item_id=result.listing.item_id,
                marketplace_sku=result.listing.sku or "",
            )
            updated += int(changed)
            already += int(not changed)
    return ListingSyncSummary(
        checked=len(results),
        already_synchronized=already,
        updated=updated,
        conflicts=sum(r.state is ListingReconciliationState.CONFLICT for r in results),
    )


def import_listing(
    store: InventoryStore,
    results: list[ListingReconciliation],
    *,
    item_id: str,
    sku: str,
    source: str | None,
    acquired_at: str | None,
    acquisition_cost: str | Decimal | None,
) -> tuple[InventoryRecord, bool]:
    """Adopt one missing-local listing without inventing unknown acquisition facts."""
    matches = [
        result
        for result in results
        if result.listing.item_id == item_id and result.listing.sku == sku
    ]
    if len(matches) != 1 or matches[0].state not in {
        ListingReconciliationState.MISSING_LOCAL,
        ListingReconciliationState.MATCHED,
    }:
        raise ListingWorkflowError("listing cannot be imported safely; refresh and review it")
    listing = matches[0].listing
    return store.adopt_ebay_listing(
        sku,
        title=listing.title,
        source=source,
        acquired_at=acquired_at,
        acquisition_cost=acquisition_cost,
        marketplace_item_id=listing.item_id,
        marketplace_sku=sku,
        quantity=1,
    )
