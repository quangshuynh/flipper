"""Exact, local-only trip assumptions and derived travel economics."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from deals.models import (
    CostComponent,
    EvidenceProvenance,
    Money,
    ProvenanceKind,
)

MAX_ONE_WAY_MILES = Decimal("10000")
MAX_MPG = Decimal("1000")
MAX_GAS_PRICE = Decimal("100")
MAX_ADDITIONAL_COST = Decimal("1000000")
MAX_TRAVEL_MINUTES = 100_000

USER_TRIP_ASSUMPTION = EvidenceProvenance(ProvenanceKind.USER_ASSUMPTION, "User trip assumption")
TRIP_CALCULATION = EvidenceProvenance(ProvenanceKind.FLIPPER_CALCULATION, "Flipper calculation")


def _optional_decimal(value: Decimal | int | str | None, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, int, or string input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a valid decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class TripAssumptions:
    one_way_miles: Decimal | None = None
    vehicle_mpg: Decimal | None = None
    gas_price_per_gallon: Money | None = None
    additional_travel_cost: Money | None = None
    round_trip_minutes: int | None = None
    provenance: Mapping[str, EvidenceProvenance] = field(default_factory=dict)

    def __post_init__(self) -> None:
        miles = _optional_decimal(self.one_way_miles, "one-way distance")
        mpg = _optional_decimal(self.vehicle_mpg, "vehicle MPG")
        if miles is not None and not 0 <= miles <= MAX_ONE_WAY_MILES:
            raise ValueError(f"one-way distance must be between 0 and {MAX_ONE_WAY_MILES}")
        if mpg is not None and not 0 < mpg <= MAX_MPG:
            raise ValueError(f"vehicle MPG must be greater than 0 and at most {MAX_MPG}")
        if self.gas_price_per_gallon is not None:
            if not 0 <= self.gas_price_per_gallon.amount <= MAX_GAS_PRICE:
                raise ValueError(f"gas price must be between 0 and {MAX_GAS_PRICE}")
        if self.additional_travel_cost is not None:
            if not 0 <= self.additional_travel_cost.amount <= MAX_ADDITIONAL_COST:
                raise ValueError(
                    f"additional travel cost must be between 0 and {MAX_ADDITIONAL_COST}"
                )
        if self.round_trip_minutes is not None:
            if isinstance(self.round_trip_minutes, bool) or not (
                0 <= self.round_trip_minutes <= MAX_TRAVEL_MINUTES
            ):
                raise ValueError(
                    f"round-trip travel minutes must be between 0 and {MAX_TRAVEL_MINUTES}"
                )
        currencies = {
            money.currency
            for money in (self.gas_price_per_gallon, self.additional_travel_cost)
            if money is not None
        }
        if len(currencies) > 1:
            raise ValueError("gas price and additional travel cost currencies must match")
        object.__setattr__(self, "one_way_miles", miles)
        object.__setattr__(self, "vehicle_mpg", mpg)
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))


@dataclass(frozen=True)
class TripEstimate:
    assumptions: TripAssumptions
    round_trip_miles: Decimal | None
    estimated_gallons: Decimal | None
    estimated_fuel_cost: Money | None
    total_travel_cost: CostComponent
    missing_inputs: tuple[str, ...]
    provenance: Mapping[str, EvidenceProvenance]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))


def calculate_trip(assumptions: TripAssumptions) -> TripEstimate:
    """Calculate trip facts without routing, geocoding, or external lookups."""
    supplied_fuel_inputs = any(
        value is not None
        for value in (
            assumptions.one_way_miles,
            assumptions.vehicle_mpg,
            assumptions.gas_price_per_gallon,
        )
    )
    missing: list[str] = []
    round_trip_miles = (
        assumptions.one_way_miles * 2 if assumptions.one_way_miles is not None else None
    )
    if supplied_fuel_inputs:
        if assumptions.one_way_miles is None:
            missing.append("one-way distance")
        if assumptions.vehicle_mpg is None:
            missing.append("vehicle MPG")
        if assumptions.gas_price_per_gallon is None:
            missing.append("gas price")

    gallons = fuel_cost = None
    if not missing and supplied_fuel_inputs:
        gallons = round_trip_miles / assumptions.vehicle_mpg
        gas_price = assumptions.gas_price_per_gallon
        fuel_cost = Money(gallons * gas_price.amount, gas_price.currency)

    additional = assumptions.additional_travel_cost
    if missing:
        total = CostComponent.unknown()
    elif fuel_cost is not None:
        amount = fuel_cost.amount + (additional.amount if additional else Decimal("0"))
        total = CostComponent.estimated(
            amount,
            fuel_cost.currency,
            provenance=EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper trip calculation",
                ("estimated fuel cost", "additional travel cost"),
            ),
        )
    elif additional is not None:
        total = CostComponent.estimated(
            additional.amount,
            additional.currency,
            provenance=EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper trip calculation",
                ("additional travel cost",),
            ),
        )
    else:
        total = CostComponent.not_applicable()

    return TripEstimate(
        assumptions=assumptions,
        round_trip_miles=round_trip_miles,
        estimated_gallons=gallons,
        estimated_fuel_cost=fuel_cost,
        total_travel_cost=total,
        missing_inputs=tuple(missing),
        provenance={
            "one_way_miles": USER_TRIP_ASSUMPTION,
            "vehicle_mpg": USER_TRIP_ASSUMPTION,
            "gas_price_per_gallon": USER_TRIP_ASSUMPTION,
            "additional_travel_cost": USER_TRIP_ASSUMPTION,
            "round_trip_minutes": USER_TRIP_ASSUMPTION,
            "round_trip_miles": TRIP_CALCULATION,
            "estimated_gallons": TRIP_CALCULATION,
            "estimated_fuel_cost": EvidenceProvenance(
                ProvenanceKind.FLIPPER_CALCULATION,
                "Flipper calculation",
                ("round-trip miles", "vehicle MPG", "gas price"),
            ),
            "total_travel_cost": total.provenance,
        },
    )


def trip_from_values(
    *,
    currency: str,
    one_way_miles: Decimal | int | str | None = None,
    vehicle_mpg: Decimal | int | str | None = None,
    gas_price_per_gallon: Decimal | int | str | None = None,
    additional_travel_cost: Decimal | int | str | None = None,
    round_trip_minutes: int | str | None = None,
) -> TripEstimate:
    """Parse interface values into typed assumptions and calculate one trip."""
    gas = _optional_decimal(gas_price_per_gallon, "gas price")
    additional = _optional_decimal(additional_travel_cost, "additional travel cost")
    minutes = None
    if round_trip_minutes not in (None, ""):
        try:
            minutes = int(round_trip_minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError("round-trip travel minutes must be a whole number") from exc
        if str(minutes) != str(round_trip_minutes).strip():
            raise ValueError("round-trip travel minutes must be a whole number")
    return calculate_trip(
        TripAssumptions(
            one_way_miles=_optional_decimal(one_way_miles, "one-way distance"),
            vehicle_mpg=_optional_decimal(vehicle_mpg, "vehicle MPG"),
            gas_price_per_gallon=Money(gas, currency) if gas is not None else None,
            additional_travel_cost=(
                Money(additional, currency) if additional is not None else None
            ),
            round_trip_minutes=minutes,
            provenance={
                name: USER_TRIP_ASSUMPTION
                for name, value in {
                    "one_way_miles": one_way_miles,
                    "vehicle_mpg": vehicle_mpg,
                    "gas_price_per_gallon": gas_price_per_gallon,
                    "additional_travel_cost": additional_travel_cost,
                    "round_trip_minutes": round_trip_minutes,
                }.items()
                if value not in (None, "")
            },
        )
    )
