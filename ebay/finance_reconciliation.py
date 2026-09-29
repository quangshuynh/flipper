"""Deterministic, read-only matching of Finances transactions to local sales."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ebay.finance_transactions import FinanceTransaction
from inventory.store import SaleRecord


class FinanceMatchStatus(str, Enum):
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    NOT_ENOUGH_IDENTIFIERS = "not_enough_identifiers"


@dataclass(frozen=True)
class FinanceMatch:
    transaction: FinanceTransaction
    status: FinanceMatchStatus
    sales: tuple[SaleRecord, ...] = ()


def reconcile_finance_transactions(
    transactions: list[FinanceTransaction], sales: list[SaleRecord]
) -> list[FinanceMatch]:
    """Match exact eBay order/line identifiers; never use amounts or fuzzy attributes."""
    ebay_sales = [sale for sale in sales if sale.marketplace.casefold() == "ebay"]
    results = []
    for transaction in transactions:
        line_ids = {line.order_line_item_id for line in transaction.order_lines}
        if transaction.order_id is None and not line_ids:
            results.append(FinanceMatch(transaction, FinanceMatchStatus.NOT_ENOUGH_IDENTIFIERS))
            continue
        matches = tuple(
            sale
            for sale in ebay_sales
            if (transaction.order_id is None or sale.external_order_id == transaction.order_id)
            and (not line_ids or sale.external_line_item_id in line_ids)
        )
        if len(matches) == 1:
            status = FinanceMatchStatus.MATCHED
        elif len(matches) > 1:
            status = FinanceMatchStatus.AMBIGUOUS
        else:
            status = FinanceMatchStatus.UNMATCHED
        results.append(FinanceMatch(transaction, status, matches))
    return results
