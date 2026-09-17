from decimal import Decimal

import pytest

from deals.economics import calculate_economics
from deals.models import AmountStatus, CostComponent, Money, ProvenanceKind
from deals.travel import TripAssumptions, calculate_trip, trip_from_values


def test_trip_calculates_exact_distance_fuel_and_total_cost():
    trip = trip_from_values(
        currency="USD",
        one_way_miles="19",
        vehicle_mpg="25",
        gas_price_per_gallon="3.625",
        additional_travel_cost="5",
        round_trip_minutes="55",
    )

    assert trip.round_trip_miles == Decimal("38")
    assert trip.estimated_gallons == Decimal("1.52")
    assert trip.estimated_fuel_cost == Money.of("5.51000")
    assert trip.total_travel_cost.money == Money.of("10.51000")
    assert trip.assumptions.round_trip_minutes == 55
    assert trip.provenance["round_trip_miles"].kind is ProvenanceKind.FLIPPER_CALCULATION
    assert trip.provenance["estimated_fuel_cost"].inputs == (
        "round-trip miles",
        "vehicle MPG",
        "gas price",
    )
    assert trip.assumptions.provenance["vehicle_mpg"].kind is ProvenanceKind.USER_ASSUMPTION


def test_trip_keeps_precision_and_leaves_display_rounding_to_interfaces():
    trip = trip_from_values(
        currency="USD",
        one_way_miles="1",
        vehicle_mpg="3",
        gas_price_per_gallon="1",
    )
    assert trip.estimated_gallons == Decimal("2") / Decimal("3")
    assert trip.estimated_fuel_cost.amount == Decimal("2") / Decimal("3")


def test_zero_distance_and_zero_gas_price_are_explicit_valid_assumptions():
    trip = trip_from_values(
        currency="USD",
        one_way_miles="0",
        vehicle_mpg="30",
        gas_price_per_gallon="0",
        additional_travel_cost="2.25",
    )
    assert trip.round_trip_miles == 0
    assert trip.estimated_gallons == 0
    assert trip.estimated_fuel_cost.amount == 0
    assert trip.total_travel_cost.money.amount == Decimal("2.25")


def test_missing_fuel_input_is_unknown_not_zero():
    trip = trip_from_values(currency="USD", one_way_miles="12", gas_price_per_gallon="3.50")
    assert trip.estimated_gallons is None
    assert trip.estimated_fuel_cost is None
    assert trip.total_travel_cost.status is AmountStatus.UNKNOWN
    assert trip.missing_inputs == ("vehicle MPG",)


def test_additional_expense_alone_is_a_complete_modeled_travel_cost():
    trip = trip_from_values(currency="USD", additional_travel_cost="7.25")
    assert trip.estimated_fuel_cost is None
    assert trip.total_travel_cost.money == Money.of("7.25")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"one_way_miles": "-1"}, "one-way distance"),
        ({"one_way_miles": "10001"}, "one-way distance"),
        ({"vehicle_mpg": "0"}, "vehicle MPG"),
        ({"vehicle_mpg": "-1"}, "vehicle MPG"),
        ({"gas_price_per_gallon": "-1"}, "gas price"),
        ({"additional_travel_cost": "-1"}, "additional travel cost"),
        ({"one_way_miles": "not-a-number"}, "one-way distance"),
        ({"round_trip_minutes": "1.5"}, "whole number"),
    ],
)
def test_trip_rejects_invalid_or_unreasonable_inputs(kwargs, message):
    with pytest.raises((TypeError, ValueError), match=message):
        trip_from_values(currency="USD", **kwargs)


def test_trip_total_enters_existing_economics_exactly_once():
    trip = trip_from_values(
        currency="USD",
        one_way_miles="10",
        vehicle_mpg="20",
        gas_price_per_gallon="4",
        additional_travel_cost="1",
    )
    result = calculate_economics(
        base_price=Money.of("40"),
        pickup_travel_cost=trip.total_travel_cost,
        expected_resale=CostComponent.estimated("100"),
    )

    assert trip.total_travel_cost.money.amount == Decimal("5")
    assert result.landed_cost.amount == Decimal("45")
    assert result.expected_net_profit.amount == Decimal("55")
    assert result.roi == Decimal("55") / Decimal("45")
    assert result.provenance["landed_cost"].inputs.count("travel") == 1


def test_no_trip_assumptions_do_not_add_a_cost():
    trip = calculate_trip(TripAssumptions())
    result = calculate_economics(
        base_price=Money.of("40"), pickup_travel_cost=trip.total_travel_cost
    )
    assert trip.total_travel_cost.status is AmountStatus.NOT_APPLICABLE
    assert result.landed_cost == Money.of("40")
