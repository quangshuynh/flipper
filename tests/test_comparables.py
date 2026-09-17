from datetime import date
from decimal import Decimal

import pytest

from deals.comparables import (
    MAX_COMPARABLES,
    Comparable,
    ComparableCondition,
    ComparableEvidenceSet,
    ComparableType,
    USER_RESEARCH_PROVENANCE,
)
from deals.models import Money, ProvenanceKind


def _comp(price="100", *, kind=ComparableType.SOLD, currency="USD", **updates):
    values = {
        "evidence_type": kind,
        "source": "Manual research",
        "price": Money.of(price, currency),
        "observed_date": date(2026, 9, 17),
        "condition": ComparableCondition.USED_TESTED,
        "event_date": date(2026, 9, 10) if kind is ComparableType.SOLD else None,
    }
    values.update(updates)
    return Comparable(**values)


def test_comparable_preserves_typed_sold_active_money_dates_and_provenance():
    sold = _comp("0.10", source_url="https://example.com/item", title="Camera")
    active = _comp("0.20", kind=ComparableType.ACTIVE_ASKING, condition=ComparableCondition.UNKNOWN)
    assert sold.price.amount + active.price.amount == Decimal("0.30")
    assert sold.event_date == date(2026, 9, 10)
    assert active.event_date is None and active.condition is ComparableCondition.UNKNOWN
    assert sold.provenance == USER_RESEARCH_PROVENANCE
    assert sold.provenance.kind is ProvenanceKind.MARKET_COMPARABLE
    with pytest.raises(TypeError, match="floats"):
        _comp(0.1)


@pytest.mark.parametrize(
    "url", ["http://example.com", "javascript:alert(1)", "https://u:p@example.com"]
)
def test_comparable_rejects_unsafe_url(url):
    with pytest.raises(ValueError, match="HTTPS"):
        _comp(source_url=url)


def test_comparable_validation_rejects_bad_fields_and_future_event():
    with pytest.raises(ValueError, match="evidence type"):
        _comp(evidence_type="sold")
    with pytest.raises(ValueError, match="source is required"):
        _comp(source="")
    with pytest.raises(ValueError, match="at most"):
        _comp(notes="x" * 1001)
    with pytest.raises(ValueError, match="after observed"):
        _comp(event_date=date(2026, 9, 18))
    with pytest.raises(ValueError, match="currency"):
        _comp(currency="US")


def test_summaries_separate_types_and_currencies_with_exact_medians():
    evidence = ComparableEvidenceSet(
        (
            _comp("100"),
            _comp("100"),
            _comp("200"),
            _comp("300"),
            _comp("90", currency="CAD"),
            _comp("250", kind=ComparableType.ACTIVE_ASKING),
        ),
        as_of=date(2026, 9, 17),
    )
    usd, cad = evidence.summaries(ComparableType.SOLD)
    assert (usd.currency, cad.currency) == ("CAD", "USD")
    assert usd.count == 1 and usd.median.amount == Decimal("90")
    assert cad.count == 4 and cad.minimum.amount == Decimal("100")
    assert cad.maximum.amount == Decimal("300")
    assert cad.median.amount == Decimal("150")
    assert evidence.summaries(ComparableType.ACTIVE_ASKING)[0].count == 1
    assert len(evidence.sold) == 5 and len(evidence.active) == 1
    assert "Multiple currencies" in evidence.limitations()[0]


def test_odd_duplicate_median_recency_condition_mix_and_limitations():
    evidence = ComparableEvidenceSet(
        (
            _comp("10", event_date=date(2025, 1, 1)),
            _comp("10", event_date=None, condition=ComparableCondition.UNKNOWN),
            _comp("30", event_date=date(2025, 2, 1), condition=ComparableCondition.FOR_PARTS),
        ),
        as_of=date(2026, 9, 17),
    )
    summary = evidence.summaries(ComparableType.SOLD)[0]
    assert summary.median.amount == Decimal("10")
    assert summary.earliest_event_date == date(2025, 1, 1)
    assert summary.latest_event_date == date(2025, 2, 1)
    assert summary.unknown_event_dates == 1
    assert {"Mixed condition", "Unknown event date", "Incomplete metadata", "Old evidence"} <= set(
        summary.limitations
    )


def test_active_only_and_empty_sets_disclose_no_sold_evidence():
    active = ComparableEvidenceSet((_comp(kind=ComparableType.ACTIVE_ASKING),))
    assert active.summaries(ComparableType.SOLD) == ()
    assert active.limitations() == ("No sold evidence", "Active-only evidence")
    assert ComparableEvidenceSet().limitations() == ("No sold evidence",)


def test_comparable_set_is_bounded():
    ComparableEvidenceSet(tuple(_comp(index) for index in range(MAX_COMPARABLES)))
    with pytest.raises(ValueError, match="limited"):
        ComparableEvidenceSet(tuple(_comp(index) for index in range(MAX_COMPARABLES + 1)))
