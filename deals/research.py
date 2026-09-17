"""Process-local comparable research workspaces for ephemeral deal opportunities."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

from deals.comparables import Comparable, ComparableEvidenceSet, MAX_COMPARABLES

MAX_RESEARCH_SESSIONS = 100


@dataclass(frozen=True)
class ResearchComparable:
    comparable_id: str
    comparable: Comparable


class EphemeralResearchStore:
    """Bounded in-memory research; process restart intentionally clears it."""

    def __init__(self) -> None:
        self._sessions: OrderedDict[str, dict[str, list[ResearchComparable]]] = OrderedDict()
        self._lock = RLock()

    def list(self, session_id: str | None, opportunity_key: str) -> tuple[ResearchComparable, ...]:
        if not session_id:
            return ()
        with self._lock:
            return tuple(self._sessions.get(session_id, {}).get(opportunity_key, ()))

    def evidence_set(self, session_id: str | None, opportunity_key: str, *, as_of=None):
        rows = self.list(session_id, opportunity_key)
        return ComparableEvidenceSet(tuple(row.comparable for row in rows), as_of=as_of)

    def add(self, session_id: str, opportunity_key: str, comparable: Comparable) -> str:
        with self._lock:
            session = self._session(session_id)
            rows = session.setdefault(opportunity_key, [])
            if len(rows) >= MAX_COMPARABLES:
                raise ValueError(f"comparable evidence is limited to {MAX_COMPARABLES} records")
            comparable_id = uuid4().hex
            rows.append(ResearchComparable(comparable_id, comparable))
            return comparable_id

    def replace(
        self, session_id: str, opportunity_key: str, comparable_id: str, comparable: Comparable
    ):
        with self._lock:
            rows = self._sessions.get(session_id, {}).get(opportunity_key, [])
            for index, row in enumerate(rows):
                if row.comparable_id == comparable_id:
                    rows[index] = ResearchComparable(comparable_id, comparable)
                    return
            raise LookupError("comparable record was not found")

    def remove(self, session_id: str, opportunity_key: str, comparable_id: str) -> None:
        with self._lock:
            rows = self._sessions.get(session_id, {}).get(opportunity_key, [])
            for index, row in enumerate(rows):
                if row.comparable_id == comparable_id:
                    rows.pop(index)
                    return
            raise LookupError("comparable record was not found")

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _session(self, session_id: str) -> dict[str, list[ResearchComparable]]:
        session = self._sessions.setdefault(session_id, {})
        self._sessions.move_to_end(session_id)
        while len(self._sessions) > MAX_RESEARCH_SESSIONS:
            self._sessions.popitem(last=False)
        return session
