"""Explicit listing states for the Deals research workspace."""

from enum import Enum


class ListingResearchMode(str, Enum):
    ACTIVE = "active"
    SOLD = "sold"

    @classmethod
    def from_query(cls, value: str) -> "ListingResearchMode":
        try:
            return cls(value or cls.ACTIVE.value)
        except ValueError:
            return cls.ACTIVE
