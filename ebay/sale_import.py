"""Explicit import of deterministically reconciled eBay lines into local sales."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ebay.orders import EbayOrder
from ebay.reconciliation import ReconciliationStatus, reconcile_ebay_orders
from inventory.store import (
    InventoryStore,
    SaleConflictError,
    SaleImportError,
    SaleRecord,
)


class SaleImportStatus(str, Enum):
    IMPORTED = "imported"
    ALREADY_IMPORTED = "already_imported"
    UNMATCHED = "unmatched"
    MISSING_SKU = "missing_sku"
    AMBIGUOUS = "ambiguous"
    MISSING_GROSS = "missing_gross"
    UNSUPPORTED_QUANTITY = "unsupported_quantity"
    INCOMPATIBLE_INVENTORY = "incompatible_inventory"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class SaleImportResult:
    order_id: str
    line_item_id: str
    sku: str | None
    status: SaleImportStatus
    inventory_id: str | None = None
    sale: SaleRecord | None = None
    detail: str | None = None


def import_ebay_sales(orders: list[EbayOrder], store: InventoryStore) -> list[SaleImportResult]:
    """Reconcile and import eligible lines one at a time with explicit outcomes."""
    reconciled = reconcile_ebay_orders(orders, store.list())
    results = []
    direct_statuses = {
        ReconciliationStatus.UNMATCHED: SaleImportStatus.UNMATCHED,
        ReconciliationStatus.MISSING_SKU: SaleImportStatus.MISSING_SKU,
        ReconciliationStatus.AMBIGUOUS: SaleImportStatus.AMBIGUOUS,
    }
    for match in reconciled:
        line = match.line_item
        if match.status is not ReconciliationStatus.MATCHED:
            results.append(
                SaleImportResult(
                    match.order_id,
                    line.line_item_id,
                    line.sku,
                    direct_statuses[match.status],
                )
            )
            continue
        inventory = match.inventory_matches[0]
        if line.quantity != 1 or inventory.quantity != 1:
            results.append(
                SaleImportResult(
                    match.order_id,
                    line.line_item_id,
                    line.sku,
                    SaleImportStatus.UNSUPPORTED_QUANTITY,
                    inventory.inventory_id,
                    detail="both order-line and local inventory quantities must equal 1",
                )
            )
            continue
        if line.line_item_cost is None:
            results.append(
                SaleImportResult(
                    match.order_id,
                    line.line_item_id,
                    line.sku,
                    SaleImportStatus.MISSING_GROSS,
                    inventory.inventory_id,
                )
            )
            continue
        try:
            sale, created = store.import_sale(
                inventory_id=inventory.inventory_id,
                marketplace="eBay",
                external_order_id=match.order_id,
                external_line_item_id=line.line_item_id,
                marketplace_sku=line.sku or "",
                quantity=line.quantity,
                gross_amount=line.line_item_cost.value,
                currency=line.line_item_cost.currency,
                sold_at=match.order_creation_date,
            )
        except SaleConflictError as exc:
            results.append(
                SaleImportResult(
                    match.order_id,
                    line.line_item_id,
                    line.sku,
                    SaleImportStatus.CONFLICT,
                    inventory.inventory_id,
                    detail=str(exc),
                )
            )
        except SaleImportError as exc:
            results.append(
                SaleImportResult(
                    match.order_id,
                    line.line_item_id,
                    line.sku,
                    SaleImportStatus.INCOMPATIBLE_INVENTORY,
                    inventory.inventory_id,
                    detail=str(exc),
                )
            )
        else:
            results.append(
                SaleImportResult(
                    match.order_id,
                    line.line_item_id,
                    line.sku,
                    SaleImportStatus.IMPORTED if created else SaleImportStatus.ALREADY_IMPORTED,
                    inventory.inventory_id,
                    sale,
                )
            )
    return results
