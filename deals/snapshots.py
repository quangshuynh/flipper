"""Versioned, bounded serialization for durable deal-research snapshots."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from deals.comparables import ComparableEvidenceSet
from deals.models import CostComponent, EvidenceProvenance, Money

SNAPSHOT_PAYLOAD_VERSION = 1
MAX_SNAPSHOT_PAYLOAD_BYTES = 262_144


class SnapshotPayloadError(ValueError):
    """A persisted snapshot payload is malformed or unsupported."""


def _decimal(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _money(value: Money | None) -> dict[str, str] | None:
    return {"amount": str(value.amount), "currency": value.currency} if value else None


def _provenance(value: EvidenceProvenance) -> dict[str, Any]:
    return {"kind": value.kind.value, "label": value.label, "inputs": list(value.inputs)}


def _component(value: CostComponent) -> dict[str, Any]:
    return {
        "status": value.status.value,
        "money": _money(value.money),
        "provenance": _provenance(value.provenance),
    }


def build_snapshot_payload(
    *,
    opportunity,
    shipping,
    notes,
    assumption_components,
    evaluation,
    trip,
    evidence: ComparableEvidenceSet,
    deal_score=None,
) -> dict[str, Any]:
    """Copy the evaluated aggregate; no live object is retained or consulted on read."""
    comparable_rows = []
    for item in evidence.records:
        comparable_rows.append(
            {
                "evidence_type": item.evidence_type.value,
                "source": item.source,
                "source_identity": item.source_identity.value,
                "source_reference_id": item.source_reference_id,
                "title": item.title,
                "price": _money(item.price),
                "observed_date": item.observed_date.isoformat(),
                "event_date": item.event_date.isoformat() if item.event_date else None,
                "condition": item.condition.value,
                "source_url": item.source_url,
                "notes": item.notes,
                "category": item.category.slug if item.category else None,
                "provenance": _provenance(item.provenance),
                "verification": item.verification_status.value,
            }
        )
    economics = evaluation.economics
    payload = {
        "schema_version": SNAPSHOT_PAYLOAD_VERSION,
        "opportunity": {
            "source": opportunity.source.value,
            "source_listing_id": opportunity.source_listing_id,
            "title": opportunity.title,
            "category": opportunity.category.slug,
            "condition": opportunity.condition,
            "base_price": _money(opportunity.base_price),
            "url": opportunity.url,
            "seller_reference": opportunity.seller_reference,
            "location_text": opportunity.location_text,
            "observed_at": opportunity.observed_at.isoformat() if opportunity.observed_at else None,
            "normalized_attributes": dict(opportunity.normalized_attributes),
            "category_attributes": dict(opportunity.category_attributes),
            "base_price_provenance": _provenance(opportunity.base_price_provenance),
            "category_provenance": _provenance(opportunity.category_provenance),
            "notes": notes,
        },
        "source_facts": {"inbound_shipping": _component(shipping)},
        "assumptions": {name: _component(value) for name, value in assumption_components.items()},
        "time_to_sale": (
            {
                "minimum_days": evaluation.time_to_sale.minimum_days,
                "maximum_days": evaluation.time_to_sale.maximum_days,
                "source": evaluation.time_to_sale.source,
                "provenance": _provenance(evaluation.time_to_sale.provenance),
            }
            if evaluation.time_to_sale
            else None
        ),
        "trip": None,
        "comparables": comparable_rows,
        "comparable_limitations": list(evidence.limitations()),
        "derived": {
            "state": economics.state.value,
            "landed_cost": _money(economics.landed_cost),
            "expected_net_proceeds": _money(economics.expected_net_proceeds),
            "expected_net_profit": _money(economics.expected_net_profit),
            "capital_tied_up": _money(economics.capital_tied_up),
            "roi": _decimal(economics.roi),
            "roi_state": economics.roi_state.value,
            "unavailable_reasons": list(economics.unavailable_reasons),
            "provenance": {k: _provenance(v) for k, v in economics.provenance.items()},
            "profit_velocity": None,
            "risks": [{"code": r.code, "explanation": r.explanation} for r in evaluation.risks],
            "confidence": {
                "pricing": evaluation.confidence.pricing.value
                if evaluation.confidence.pricing
                else None,
                "time_to_sale": evaluation.confidence.time_to_sale.value
                if evaluation.confidence.time_to_sale
                else None,
                "condition": evaluation.confidence.condition.value
                if evaluation.confidence.condition
                else None,
                "category_match": evaluation.confidence.category_match.value
                if evaluation.confidence.category_match
                else None,
                "notes": list(evaluation.confidence.notes),
            },
            "deal_score": None,
        },
    }
    if deal_score is not None:
        from deals.scoring import deal_score_to_payload

        payload["derived"]["deal_score"] = deal_score_to_payload(deal_score)
    if economics.profit_velocity:
        velocity = economics.profit_velocity
        payload["derived"]["profit_velocity"] = {
            "conservative_profit_per_day": _money(velocity.conservative_profit_per_day),
            "optimistic_profit_per_day": _money(velocity.optimistic_profit_per_day),
            "conservative_roi_per_day": _decimal(velocity.conservative_roi_per_day),
            "optimistic_roi_per_day": _decimal(velocity.optimistic_roi_per_day),
        }
    if trip:
        payload["trip"] = {
            "one_way_miles": _decimal(trip.assumptions.one_way_miles),
            "round_trip_miles": _decimal(trip.round_trip_miles),
            "vehicle_mpg": _decimal(trip.assumptions.vehicle_mpg),
            "gas_price_per_gallon": _money(trip.assumptions.gas_price_per_gallon),
            "estimated_gallons": _decimal(trip.estimated_gallons),
            "estimated_fuel_cost": _money(trip.estimated_fuel_cost),
            "additional_travel_cost": _money(trip.assumptions.additional_travel_cost),
            "total_travel_cost": _component(trip.total_travel_cost),
            "round_trip_minutes": trip.assumptions.round_trip_minutes,
            "missing_inputs": list(trip.missing_inputs),
            "provenance": {k: _provenance(v) for k, v in trip.provenance.items()},
        }
    validate_snapshot_payload(payload)
    return payload


def validate_snapshot_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_PAYLOAD_VERSION:
        raise SnapshotPayloadError("research snapshot payload version is unsupported")
    required = {"opportunity", "source_facts", "assumptions", "comparables", "derived"}
    if not required <= payload.keys() or not isinstance(payload["opportunity"], dict):
        raise SnapshotPayloadError("research snapshot payload is malformed")
    opportunity = payload["opportunity"]
    if not isinstance(opportunity.get("title"), str) or not opportunity["title"].strip():
        raise SnapshotPayloadError("research snapshot title is missing")
    if not isinstance(payload["comparables"], list) or len(payload["comparables"]) > 25:
        raise SnapshotPayloadError("research snapshot comparable evidence is invalid")
    encoded = canonical_snapshot_json(payload)
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_PAYLOAD_BYTES:
        raise SnapshotPayloadError("research snapshot payload is too large")
    return payload


def canonical_snapshot_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_snapshot_json(value: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SnapshotPayloadError("research snapshot payload is malformed") from exc
    return validate_snapshot_payload(payload)
