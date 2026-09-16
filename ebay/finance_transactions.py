"""PII-minimized domain model for eBay Finances transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


class FinanceResponseError(RuntimeError):
    """The Finances API returned an unexpected transaction representation."""


class TransactionClassification(str, Enum):
    """Documented native eBay transaction types, without accounting remapping."""

    SALE = "sale"
    REFUND = "refund"
    CREDIT = "credit"
    DISPUTE = "dispute"
    SHIPPING_LABEL = "shipping_label"
    TRANSFER = "transfer"
    NON_SALE_CHARGE = "non_sale_charge"
    ADJUSTMENT = "adjustment"
    WITHDRAWAL = "withdrawal"
    PURCHASE = "purchase"
    LOAN_REPAYMENT = "loan_repayment"
    UNKNOWN = "unknown"


_CLASSIFICATIONS = {
    classification.name: classification
    for classification in TransactionClassification
    if classification is not TransactionClassification.UNKNOWN
}


@dataclass(frozen=True)
class FinanceMoney:
    value: Decimal
    currency: str


@dataclass(frozen=True)
class FinanceOrderLine:
    order_line_item_id: str
    fee_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class FinanceTransaction:
    transaction_id: str
    native_type: str
    classification: TransactionClassification
    transaction_date: datetime
    amount: FinanceMoney
    booking_entry: str | None
    transaction_status: str | None
    order_id: str | None
    order_lines: tuple[FinanceOrderLine, ...]
    reference_id: str | None
    reference_type: str | None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _money(raw: Any) -> FinanceMoney:
    if not isinstance(raw, dict):
        raise FinanceResponseError("eBay returned a malformed transaction amount")
    currency = raw.get("currency")
    try:
        value = Decimal(str(raw["value"]))
    except (KeyError, InvalidOperation, TypeError, ValueError) as exc:
        raise FinanceResponseError("eBay returned a malformed transaction amount") from exc
    if not value.is_finite() or not isinstance(currency, str) or not currency:
        raise FinanceResponseError("eBay returned a malformed transaction amount")
    return FinanceMoney(value, currency)


def normalize_transaction(raw: Any) -> FinanceTransaction:
    """Select documented reconciliation fields and deliberately discard buyer data."""
    if not isinstance(raw, dict):
        raise FinanceResponseError("eBay returned a malformed financial transaction")
    transaction_id = raw.get("transactionId")
    native_type = raw.get("transactionType")
    date_value = raw.get("transactionDate")
    required = (transaction_id, native_type, date_value)
    if not all(isinstance(value, str) and value for value in required):
        raise FinanceResponseError("eBay financial transaction is missing a required field")
    try:
        transaction_date = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinanceResponseError("eBay returned a malformed transaction date") from exc
    if transaction_date.tzinfo is None or transaction_date.utcoffset() is None:
        raise FinanceResponseError("eBay returned a transaction date without a timezone")

    raw_lines = raw.get("orderLineItems", [])
    if not isinstance(raw_lines, list):
        raise FinanceResponseError("eBay returned malformed order-line details")
    lines = []
    for raw_line in raw_lines:
        if not isinstance(raw_line, dict):
            raise FinanceResponseError("eBay returned malformed order-line details")
        line_id = raw_line.get("orderLineItemId")
        if not isinstance(line_id, str) or not line_id:
            continue
        raw_fees = raw_line.get("fees", [])
        if not isinstance(raw_fees, list):
            raise FinanceResponseError("eBay returned malformed fee details")
        fee_types = tuple(
            fee_type
            for fee in raw_fees
            if isinstance(fee, dict)
            and isinstance((fee_type := fee.get("feeType")), str)
            and fee_type
        )
        lines.append(FinanceOrderLine(line_id, fee_types))

    reference = raw.get("references") or raw.get("reference") or {}
    if isinstance(reference, list):
        reference = next((item for item in reference if isinstance(item, dict)), {})
    if not isinstance(reference, dict):
        reference = {}
    assert isinstance(transaction_id, str)
    assert isinstance(native_type, str)
    return FinanceTransaction(
        transaction_id=transaction_id,
        native_type=native_type,
        classification=_CLASSIFICATIONS.get(native_type, TransactionClassification.UNKNOWN),
        transaction_date=transaction_date,
        amount=_money(raw.get("amount")),
        booking_entry=_optional_string(raw.get("bookingEntry")),
        transaction_status=_optional_string(raw.get("transactionStatus")),
        order_id=_optional_string(raw.get("orderId")),
        order_lines=tuple(lines),
        reference_id=_optional_string(reference.get("referenceId")),
        reference_type=_optional_string(reference.get("referenceType")),
    )
