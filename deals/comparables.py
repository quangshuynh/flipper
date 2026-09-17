"""Bounded, user-provided comparable market evidence."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import Enum
from urllib.parse import urlsplit

from deals.categories import DealCategory
from deals.models import EvidenceProvenance, Money, ProvenanceKind, SourceIdentity

MAX_COMPARABLES = 25
MAX_SOURCE_LENGTH = 100
MAX_REFERENCE_LENGTH = 200
MAX_TITLE_LENGTH = 300
MAX_URL_LENGTH = 2_000
MAX_NOTES_LENGTH = 1_000
STALE_AFTER_DAYS = 180


class ComparableType(str, Enum):
    SOLD = "sold"
    ACTIVE_ASKING = "active_asking"


class ComparableCondition(str, Enum):
    NEW = "new"
    OPEN_BOX = "open_box"
    USED_TESTED = "used_tested"
    USED_UNTESTED = "used_untested"
    FOR_PARTS = "for_parts"
    UNKNOWN = "unknown"


class VerificationStatus(str, Enum):
    USER_REPORTED = "user_reported"
    UNVERIFIED = "unverified"


USER_RESEARCH_PROVENANCE = EvidenceProvenance(
    ProvenanceKind.MARKET_COMPARABLE, "User-provided market research"
)


def _bounded(value: str | None, name: str, limit: int, *, required: bool = False) -> str | None:
    normalized = value.strip() if value else None
    if required and not normalized:
        raise ValueError(f"{name} is required")
    if normalized and len(normalized) > limit:
        raise ValueError(f"{name} must be at most {limit} characters")
    return normalized


@dataclass(frozen=True)
class Comparable:
    evidence_type: ComparableType
    source: str
    price: Money
    observed_date: date
    condition: ComparableCondition = ComparableCondition.UNKNOWN
    event_date: date | None = None
    source_identity: SourceIdentity = SourceIdentity.OTHER
    source_reference_id: str | None = None
    title: str | None = None
    source_url: str | None = None
    notes: str | None = None
    category: DealCategory | None = None
    provenance: EvidenceProvenance = USER_RESEARCH_PROVENANCE
    verification_status: VerificationStatus = VerificationStatus.USER_REPORTED

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_type, ComparableType):
            raise ValueError("evidence type must be sold or active asking")
        if not isinstance(self.observed_date, date):
            raise ValueError("observed date is required")
        if not isinstance(self.condition, ComparableCondition):
            raise ValueError("condition must be a recognized value or unknown")
        object.__setattr__(
            self, "source", _bounded(self.source, "source", MAX_SOURCE_LENGTH, required=True)
        )
        object.__setattr__(
            self,
            "source_reference_id",
            _bounded(self.source_reference_id, "source reference", MAX_REFERENCE_LENGTH),
        )
        object.__setattr__(self, "title", _bounded(self.title, "title", MAX_TITLE_LENGTH))
        object.__setattr__(self, "notes", _bounded(self.notes, "notes", MAX_NOTES_LENGTH))
        url = _bounded(self.source_url, "source URL", MAX_URL_LENGTH)
        if url:
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("source URL must be an HTTPS URL without credentials")
        object.__setattr__(self, "source_url", url)
        if self.price.amount < 0:
            raise ValueError("comparable price cannot be negative")
        if self.event_date and self.event_date > self.observed_date:
            raise ValueError("event date cannot be after observed date")
        if self.provenance != USER_RESEARCH_PROVENANCE:
            raise ValueError("manual comparable provenance must be user-provided market research")


@dataclass(frozen=True)
class EvidenceSummary:
    evidence_type: ComparableType
    currency: str
    count: int
    minimum: Money
    maximum: Money
    median: Money
    earliest_event_date: date | None
    latest_event_date: date | None
    unknown_event_dates: int
    condition_counts: tuple[tuple[ComparableCondition, int], ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class ComparableEvidenceSet:
    records: tuple[Comparable, ...] = ()
    as_of: date | None = None

    def __post_init__(self) -> None:
        if len(self.records) > MAX_COMPARABLES:
            raise ValueError(f"comparable evidence is limited to {MAX_COMPARABLES} records")

    @property
    def sold(self) -> tuple[Comparable, ...]:
        return tuple(row for row in self.records if row.evidence_type is ComparableType.SOLD)

    @property
    def active(self) -> tuple[Comparable, ...]:
        return tuple(
            row for row in self.records if row.evidence_type is ComparableType.ACTIVE_ASKING
        )

    def summaries(self, evidence_type: ComparableType) -> tuple[EvidenceSummary, ...]:
        selected = self.sold if evidence_type is ComparableType.SOLD else self.active
        currencies = sorted({row.price.currency for row in selected})
        return tuple(self._summary(evidence_type, currency, selected) for currency in currencies)

    def limitations(self) -> tuple[str, ...]:
        limits: list[str] = []
        if not self.sold:
            limits.append("No sold evidence")
            if self.active:
                limits.append("Active-only evidence")
        if len({row.price.currency for row in self.records}) > 1:
            limits.append("Multiple currencies; summaries remain separate")
        return tuple(limits)

    def _summary(self, evidence_type, currency, selected) -> EvidenceSummary:
        rows = [row for row in selected if row.price.currency == currency]
        amounts = sorted(row.price.amount for row in rows)
        middle = len(amounts) // 2
        median = (
            amounts[middle] if len(amounts) % 2 else (amounts[middle - 1] + amounts[middle]) / 2
        )
        dates = sorted(row.event_date for row in rows if row.event_date is not None)
        conditions = Counter(row.condition for row in rows)
        limits: list[str] = []
        if len(rows) < 3:
            limits.append("Small sample")
        if len(conditions) > 1:
            limits.append("Mixed condition")
        unknown_dates = len(rows) - len(dates)
        if unknown_dates:
            limits.append("Unknown event date")
        if any(row.condition is ComparableCondition.UNKNOWN for row in rows):
            limits.append("Incomplete metadata")
        if self.as_of and dates and (self.as_of - dates[-1]).days > STALE_AFTER_DAYS:
            limits.append("Old evidence")
        return EvidenceSummary(
            evidence_type,
            currency,
            len(rows),
            Money(amounts[0], currency),
            Money(amounts[-1], currency),
            Money(median, currency),
            dates[0] if dates else None,
            dates[-1] if dates else None,
            unknown_dates,
            tuple(sorted(conditions.items(), key=lambda item: item[0].value)),
            tuple(limits),
        )
