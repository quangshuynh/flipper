"""Deterministic, read-only matching of eBay order lines to local inventory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ebay.orders import EbayOrder, EbayOrderLineItem
from inventory.store import InventoryRecord


class ReconciliationStatus(str, Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    MISSING_SKU = "missing_sku"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class ReconciliationResult:
    order_id: str
    order_creation_date: datetime
    line_item: EbayOrderLineItem
    status: ReconciliationStatus
    inventory_matches: tuple[InventoryRecord, ...] = ()


@dataclass(frozen=True)
class ReconciliationSummary:
    examined: int
    matched: int
    unmatched: int
    missing_sku: int
    ambiguous: int


def reconcile_ebay_orders(
    orders: list[EbayOrder], inventory: list[InventoryRecord]
) -> list[ReconciliationResult]:
    """Match eBay lines by exact SKU within the eBay marketplace scope."""
    by_sku: dict[str, list[InventoryRecord]] = {}
    for record in inventory:
        if (
            record.marketplace is not None
            and record.marketplace.casefold() == "ebay"
            and record.marketplace_sku is not None
        ):
            by_sku.setdefault(record.marketplace_sku, []).append(record)

    results = []
    for order in orders:
        for line_item in order.line_items:
            if line_item.sku is None:
                status = ReconciliationStatus.MISSING_SKU
                matches = ()
            else:
                matches = tuple(
                    sorted(by_sku.get(line_item.sku, ()), key=lambda item: item.inventory_id)
                )
                if len(matches) == 1:
                    status = ReconciliationStatus.MATCHED
                elif matches:
                    status = ReconciliationStatus.AMBIGUOUS
                else:
                    status = ReconciliationStatus.UNMATCHED
            results.append(
                ReconciliationResult(
                    order.order_id, order.creation_date, line_item, status, matches
                )
            )
    return results


def summarize(results: list[ReconciliationResult]) -> ReconciliationSummary:
    """Count each explicit reconciliation state."""
    return ReconciliationSummary(
        examined=len(results),
        matched=sum(result.status is ReconciliationStatus.MATCHED for result in results),
        unmatched=sum(result.status is ReconciliationStatus.UNMATCHED for result in results),
        missing_sku=sum(result.status is ReconciliationStatus.MISSING_SKU for result in results),
        ambiguous=sum(result.status is ReconciliationStatus.AMBIGUOUS for result in results),
    )
