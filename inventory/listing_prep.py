"""Deterministic helpers for Quick Listing Prep.

Everything here is a planning aid built from local facts and user-provided assumptions. Nothing is
inferred from photos, nothing is fetched or published, and estimates never become inventory,
valuation, or accounting facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from inventory.store import (
    InventoryRecord,
    ListingDraftRecord,
    ResearchSnapshotRecord,
    ValuationSnapshot,
)

CONDITION_LABELS = {
    "new": "New",
    "open_box": "Open box",
    "used": "Used",
    "for_parts": "For parts or not working",
}

TEMPLATE_PLACEHOLDER = "[confirm before listing]"


@dataclass(frozen=True)
class ProceedsEstimate:
    """Estimated selling fees and proceeds in integer USD cents.

    A ``None`` amount is unavailable because an input is unknown; it is never zero. ``missing``
    names the inputs that would make the estimate available.
    """

    fees_cents: int | None
    proceeds_cents: int | None
    net_after_cost_cents: int | None
    missing: tuple[str, ...]


def estimate_proceeds(
    draft: ListingDraftRecord | None, acquisition_cost_cents: int | None
) -> ProceedsEstimate:
    """Price minus percentage fee, fixed fee, and seller-paid shipping, using exact cents."""
    if draft is None:
        return ProceedsEstimate(
            None, None, None, ("asking price", "fee rate", "fixed fee", "shipping")
        )
    price = draft.asking_price_cents
    missing = [
        name
        for name, value in (
            ("asking price", price),
            ("fee rate", draft.fee_rate_bps),
            ("fixed fee", draft.fixed_fee_cents),
            ("shipping", draft.shipping_cost_cents),
        )
        if value is None
    ]
    fees = None
    if price is not None and draft.fee_rate_bps is not None and draft.fixed_fee_cents is not None:
        percentage = (Decimal(price) * draft.fee_rate_bps / Decimal(10_000)).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        )
        fees = int(percentage) + draft.fixed_fee_cents
    proceeds = None
    if fees is not None and draft.shipping_cost_cents is not None:
        proceeds = price - fees - draft.shipping_cost_cents  # type: ignore[operator]
    net = None
    if proceeds is not None and acquisition_cost_cents is not None:
        net = proceeds - acquisition_cost_cents
    return ProceedsEstimate(fees, proceeds, net, tuple(missing))


def default_title(record: InventoryRecord) -> str:
    """Start from the inventory title, collapsed and trimmed to the 80-character title limit."""
    return " ".join(record.title.split())[:80].rstrip()


def description_template(
    record: InventoryRecord, condition: str | None, defects: str, photo_count: int
) -> str:
    """Build an editable description from known inventory facts and bracketed placeholders.

    Unknown details stay as visible placeholders so they are confirmed by the seller rather than
    invented. Private inventory notes, source, and cost are deliberately excluded.
    """
    condition_text = CONDITION_LABELS.get(condition or "", TEMPLATE_PLACEHOLDER)
    defects_text = defects.strip() or f"None recorded {TEMPLATE_PLACEHOLDER}"
    photos = (
        f"See {photo_count} photo{'s' if photo_count != 1 else ''} for the exact item."
        if photo_count
        else "Photos: [add photos of the exact item]"
    )
    return "\n".join(
        (
            default_title(record),
            "",
            f"Condition: {condition_text}",
            f"Known defects: {defects_text}",
            "",
            "Specifications: [confirm model, processor, memory, and storage]",
            "Testing performed: [describe exactly what you tested]",
            "Included: [list everything in the box]",
            "Not included: [list anything a buyer might expect]",
            "",
            photos,
        )
    )


@dataclass(frozen=True)
class PricingEvidence:
    label: str
    amount: Decimal
    currency: str
    captured: str
    detail: str


def pricing_evidence(
    valuation: ValuationSnapshot | None, snapshots: list[ResearchSnapshotRecord]
) -> list[PricingEvidence]:
    """List existing, dated research estimates for this item. Nothing new is derived."""
    evidence: list[PricingEvidence] = []
    if valuation is not None:
        evidence.append(
            PricingEvidence(
                "Baseline research asking price",
                valuation.asking_price,
                valuation.currency,
                valuation.analyzed_at,
                valuation.pricing_method or "Pricing method unavailable",
            )
        )
        evidence.append(
            PricingEvidence(
                "Baseline expected resale",
                valuation.expected_resale_value,
                valuation.currency,
                valuation.analyzed_at,
                valuation.pricing_method or "Pricing method unavailable",
            )
        )
    for snapshot in snapshots:
        if snapshot.expected_resale is not None:
            evidence.append(
                PricingEvidence(
                    "Saved research expected resale",
                    snapshot.expected_resale,
                    snapshot.currency,
                    snapshot.saved_at,
                    snapshot.title,
                )
            )
    return evidence
