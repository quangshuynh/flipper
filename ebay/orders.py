"""PII-minimizing domain models for read-only eBay seller orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


class OrderResponseError(RuntimeError):
    """The Fulfillment API returned an unexpected order representation."""


@dataclass(frozen=True)
class Money:
    """A monetary amount exactly as supplied by eBay."""

    value: Decimal
    currency: str


@dataclass(frozen=True)
class OrderPricingSummary:
    """Selected order totals; absent values remain absent."""

    subtotal: Money | None = None
    shipping: Money | None = None
    tax: Money | None = None
    discount: Money | None = None
    total: Money | None = None


@dataclass(frozen=True)
class EbayOrderLineItem:
    """A seller-relevant line item with no buyer information."""

    line_item_id: str
    legacy_item_id: str | None
    title: str
    sku: str | None
    quantity: int
    line_item_cost: Money | None
    discounts: tuple[Money, ...] = field(default_factory=tuple)
    refunds: tuple[Money, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class EbayOrder:
    """A normalized read-only eBay order with buyer PII deliberately omitted."""

    order_id: str
    creation_date: datetime
    last_modified_date: datetime | None
    fulfillment_status: str | None
    payment_status: str | None
    cancellation_status: str | None
    line_items: tuple[EbayOrderLineItem, ...]
    pricing: OrderPricingSummary
    source_line_count: int | None = None


def _money(value: Any) -> Money | None:
    if not isinstance(value, dict) or "value" not in value or "currency" not in value:
        return None
    try:
        amount = Decimal(str(value["value"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderResponseError("eBay returned a malformed monetary value") from exc
    currency = value["currency"]
    if not isinstance(currency, str) or not currency:
        raise OrderResponseError("eBay returned a malformed currency")
    return Money(amount, currency)


def _date(value: Any, *, required: bool = False) -> datetime | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise OrderResponseError("eBay returned a malformed order date")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrderResponseError("eBay returned a malformed order date") from exc


def _marketplace_tax(raw_items: list[Any]) -> Money | None:
    """Return exact marketplace-collected tax when line totals use one currency."""
    amounts: list[Money] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        taxes = raw_item.get("ebayCollectAndRemitTaxes") or []
        if not isinstance(taxes, list):
            raise OrderResponseError("eBay returned malformed marketplace tax")
        for tax in taxes:
            if not isinstance(tax, dict):
                raise OrderResponseError("eBay returned malformed marketplace tax")
            amount = _money(tax.get("amount"))
            if amount is not None:
                amounts.append(amount)
    if not amounts:
        return None
    currency = amounts[0].currency
    if any(amount.currency != currency for amount in amounts):
        return None
    return Money(sum((amount.value for amount in amounts), Decimal(0)), currency)


def normalize_order(raw: Any) -> EbayOrder:
    """Normalize an API order without retaining buyer/contact/address fields."""
    if not isinstance(raw, dict):
        raise OrderResponseError("eBay returned a malformed order")
    try:
        order_id = raw["orderId"]
        creation_date = _date(raw["creationDate"], required=True)
        raw_items = raw["lineItems"]
    except KeyError as exc:
        raise OrderResponseError("eBay order is missing a required field") from exc
    if not isinstance(order_id, str) or not order_id or not isinstance(raw_items, list):
        raise OrderResponseError("eBay order contains an invalid required field")

    items: list[EbayOrderLineItem] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise OrderResponseError("eBay returned a malformed line item")
        try:
            line_item_id = raw_item["lineItemId"]
            title = raw_item["title"]
            quantity = int(raw_item["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise OrderResponseError("eBay line item is missing a required field") from exc
        if not isinstance(line_item_id, str) or not isinstance(title, str) or quantity < 0:
            raise OrderResponseError("eBay line item contains an invalid required field")
        refunds: list[Money] = []
        for refund in raw_item.get("refunds", []):
            if isinstance(refund, dict):
                amount = _money(refund.get("refundAmount"))
                if amount is not None:
                    refunds.append(amount)
        discounts: list[Money] = []
        for promotion in raw_item.get("appliedPromotions", []):
            if isinstance(promotion, dict):
                amount = _money(promotion.get("discountAmount"))
                if amount is not None:
                    discounts.append(amount)
        items.append(
            EbayOrderLineItem(
                line_item_id=line_item_id,
                legacy_item_id=raw_item.get("legacyItemId"),
                title=title,
                sku=raw_item.get("sku"),
                quantity=quantity,
                line_item_cost=_money(raw_item.get("lineItemCost")),
                discounts=tuple(discounts),
                refunds=tuple(refunds),
            )
        )

    summary = raw.get("pricingSummary") or {}
    if not isinstance(summary, dict):
        raise OrderResponseError("eBay returned a malformed pricing summary")
    cancel_status = raw.get("cancelStatus") or {}
    if not isinstance(cancel_status, dict):
        raise OrderResponseError("eBay returned a malformed cancellation status")
    assert creation_date is not None
    summary_tax = _money(summary.get("tax"))
    return EbayOrder(
        order_id=order_id,
        creation_date=creation_date,
        last_modified_date=_date(raw.get("lastModifiedDate")),
        fulfillment_status=raw.get("orderFulfillmentStatus"),
        payment_status=raw.get("orderPaymentStatus"),
        cancellation_status=cancel_status.get("cancelState"),
        line_items=tuple(items),
        pricing=OrderPricingSummary(
            subtotal=_money(summary.get("priceSubtotal")),
            shipping=_money(summary.get("deliveryCost")),
            tax=summary_tax if summary_tax is not None else _marketplace_tax(raw_items),
            discount=_money(summary.get("deliveryDiscount") or summary.get("totalDiscount")),
            total=_money(summary.get("total")),
        ),
        source_line_count=len(raw_items),
    )
