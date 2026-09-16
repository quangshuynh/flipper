"""Conservative accounting import for deterministically matched Finances data."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from ebay.finance_reconciliation import FinanceMatchStatus, reconcile_finance_transactions
from ebay.finance_transactions import FinanceTransaction, TransactionClassification
from inventory.store import (
    InventoryStore,
    SaleCostConflictError,
    SaleCostRecord,
    SaleCostValidationError,
    SaleRecord,
)


# These native types are documented selling fees attributable to an order line. Credits,
# subscriptions, listing upgrades, donations, shipping labels, and opaque OTHER_FEES are excluded.
MARKETPLACE_FEE_TYPES = frozenset(
    {
        "AD_FEE",
        "BELOW_STANDARD_FEE",
        "BELOW_STANDARD_SHIPPING_FEE",
        "FINAL_VALUE_FEE",
        "FINAL_VALUE_FEE_FIXED_PER_ORDER",
        "FINAL_VALUE_SHIPPING_FEE",
        "HIGH_ITEM_NOT_AS_DESCRIBED_FEE",
        "HIGH_ITEM_NOT_AS_DESCRIBED_SHIPPING_FEE",
        "INTERNATIONAL_FEE",
        "PAYMENT_PROCESSING_FEE",
        "PREMIUM_AD_FEES",
        "REGULATORY_OPERATING_FEE",
    }
)


class FinanceImportStatus(str, Enum):
    IMPORTED = "imported"
    ALREADY_IMPORTED = "already_imported"
    UNSUPPORTED = "unsupported"
    UNMATCHED = "unmatched"
    AMBIGUOUS = "ambiguous"
    INSUFFICIENT_IDENTIFIERS = "insufficient_identifiers"
    CONFLICT = "conflict"
    REJECTED = "rejected"


@dataclass(frozen=True)
class FinanceImportResult:
    transaction_id: str
    status: FinanceImportStatus
    sale: SaleRecord | None = None
    category: str | None = None
    cost: SaleCostRecord | None = None
    detail: str | None = None


@dataclass(frozen=True)
class _Candidate:
    category: str
    amount: Decimal
    currency: str
    component_key: str
    note: str
    effect: str = "reduce"
    reversal_of_key: str | None = None


def _candidates(
    transaction: FinanceTransaction, sale: SaleRecord | None = None
) -> list[_Candidate]:
    if transaction.classification is TransactionClassification.REFUND:
        if transaction.booking_entry != "DEBIT" or transaction.amount.value == 0:
            return []
        line_ids = {line.order_line_item_id for line in transaction.order_lines}
        if sale is not None and line_ids and line_ids != {sale.external_line_item_id}:
            return []
        return [
            _Candidate(
                "refund",
                abs(transaction.amount.value),
                transaction.amount.currency,
                "refund",
                "eBay buyer refund",
            )
        ]
    if transaction.classification is TransactionClassification.SHIPPING_LABEL:
        if transaction.amount.value == 0 or transaction.booking_entry not in {"DEBIT", "CREDIT"}:
            return []
        is_credit = transaction.booking_entry == "CREDIT"
        return [
            _Candidate(
                "shipping_cost",
                abs(transaction.amount.value),
                transaction.amount.currency,
                f"shipping-label:{transaction.transaction_id}",
                "eBay shipping label credit" if is_credit else "eBay shipping label purchase",
                "increase" if is_credit else "reduce",
                "shipping-label" if is_credit else None,
            )
        ]
    if transaction.classification not in {
        TransactionClassification.SALE,
        TransactionClassification.CREDIT,
    }:
        return []

    is_credit = transaction.classification is TransactionClassification.CREDIT
    if is_credit and transaction.booking_entry != "CREDIT":
        return []

    totals: dict[tuple[str, str, str], Decimal] = {}
    for line in transaction.order_lines:
        if sale is not None and line.order_line_item_id != sale.external_line_item_id:
            continue
        for fee in line.fees:
            if fee.native_type not in MARKETPLACE_FEE_TYPES or fee.amount is None:
                continue
            key = (line.order_line_item_id, fee.native_type, fee.amount.currency)
            totals[key] = totals.get(key, Decimal(0)) + abs(fee.amount.value)
    return [
        _Candidate(
            "marketplace_fee",
            amount,
            currency,
            f"fee-credit:{line_id}:{fee_type}" if is_credit else f"fee:{line_id}:{fee_type}",
            f"eBay {fee_type} credit" if is_credit else f"eBay {fee_type}",
            "increase" if is_credit else "reduce",
            f"fee:{line_id}:{fee_type}" if is_credit else None,
        )
        for (line_id, fee_type, currency), amount in sorted(totals.items())
        if amount != 0
    ]


def import_finance_transactions(
    transactions: list[FinanceTransaction], store: InventoryStore
) -> list[FinanceImportResult]:
    """Import supported components with one independent transaction per component."""
    matches = reconcile_finance_transactions(transactions, store.list_sales())
    results: list[FinanceImportResult] = []
    match_statuses = {
        FinanceMatchStatus.UNMATCHED: FinanceImportStatus.UNMATCHED,
        FinanceMatchStatus.AMBIGUOUS: FinanceImportStatus.AMBIGUOUS,
        FinanceMatchStatus.NOT_ENOUGH_IDENTIFIERS: FinanceImportStatus.INSUFFICIENT_IDENTIFIERS,
    }
    for match in matches:
        candidates = _candidates(match.transaction)
        if not candidates:
            results.append(
                FinanceImportResult(
                    match.transaction.transaction_id, FinanceImportStatus.UNSUPPORTED
                )
            )
            continue
        if match.status is not FinanceMatchStatus.MATCHED:
            results.append(
                FinanceImportResult(match.transaction.transaction_id, match_statuses[match.status])
            )
            continue
        sale = match.sales[0]
        candidates = _candidates(match.transaction, sale)
        if not candidates:
            results.append(
                FinanceImportResult(
                    match.transaction.transaction_id,
                    FinanceImportStatus.UNSUPPORTED,
                    sale,
                    detail="supported accounting data was not attributable to the matched sale",
                )
            )
            continue
        for candidate in candidates:
            related_cost_id = None
            if candidate.reversal_of_key is not None:
                existing_credit = next(
                    (
                        cost
                        for cost in store.list_sale_costs(sale.sale_id)
                        if cost.source == "ebay_finances"
                        and cost.external_transaction_id == match.transaction.transaction_id
                        and cost.external_component_key == candidate.component_key
                    ),
                    None,
                )
                if existing_credit is not None:
                    related_cost_id = existing_credit.related_cost_id
                    target = (
                        store.get_sale_cost(related_cost_id)
                        if related_cost_id is not None
                        else None
                    )
                else:
                    target = None
                target_key = candidate.reversal_of_key
                if target is None and target_key == "shipping-label":
                    targets = [
                        cost
                        for cost in store.list_sale_costs(sale.sale_id)
                        if cost.source == "ebay_finances"
                        and cost.effect == "reduce"
                        and cost.category == "shipping_cost"
                        and cost.external_component_key is not None
                        and cost.external_component_key.startswith("shipping-label:")
                    ]
                    reversed_ids = {
                        cost.related_cost_id
                        for cost in store.list_sale_costs(sale.sale_id)
                        if cost.effect == "increase"
                    }
                    targets = [cost for cost in targets if cost.cost_id not in reversed_ids]
                    target = targets[0] if len(targets) == 1 else None
                elif target is None:
                    target = store.find_imported_reversal_target(
                        sale.sale_id,
                        category=candidate.category,
                        external_component_key=target_key,
                    )
                if target is None:
                    results.append(
                        FinanceImportResult(
                            match.transaction.transaction_id,
                            FinanceImportStatus.UNSUPPORTED,
                            sale,
                            candidate.category,
                            detail="credit has no unique unreversed imported component",
                        )
                    )
                    continue
                related_cost_id = target.cost_id
            try:
                cost, created = store.import_ebay_finances_cost(
                    sale.sale_id,
                    category=candidate.category,
                    amount=candidate.amount,
                    currency=candidate.currency,
                    external_transaction_id=match.transaction.transaction_id,
                    external_component_key=candidate.component_key,
                    note=candidate.note,
                    effect=candidate.effect,
                    related_cost_id=related_cost_id,
                )
            except SaleCostConflictError as exc:
                results.append(
                    FinanceImportResult(
                        match.transaction.transaction_id,
                        FinanceImportStatus.CONFLICT,
                        sale,
                        candidate.category,
                        detail=str(exc),
                    )
                )
            except SaleCostValidationError as exc:
                results.append(
                    FinanceImportResult(
                        match.transaction.transaction_id,
                        FinanceImportStatus.REJECTED,
                        sale,
                        candidate.category,
                        detail=str(exc),
                    )
                )
            else:
                results.append(
                    FinanceImportResult(
                        match.transaction.transaction_id,
                        FinanceImportStatus.IMPORTED
                        if created
                        else FinanceImportStatus.ALREADY_IMPORTED,
                        sale,
                        candidate.category,
                        cost,
                    )
                )
    return results
