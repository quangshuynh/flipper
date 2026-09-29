# Research history

Working research under **Deals** is intentionally temporary: it is browser-session isolated,
process-local, bounded, and may disappear after a restart or eviction. Nothing is persisted merely
because you searched, opened, edited, compared, or evaluated an opportunity.

Choose **Save research snapshot** on an evaluated opportunity to create a durable local historical
record. Each intentional save creates a new record; repeating the same submitted request is
idempotent. A snapshot preserves normalized source facts, user assumptions, comparable records,
modeled trip, risks and limitations, provenance/lineage, and material calculated results at save
time. Historical calculations are displayed from the captured result and are not silently
recomputed with future formulas or heuristics.

Snapshots distinguish API source facts, user-provided source facts, user assumptions, market
comparables, and Flipper calculations. User-provided comparable evidence remains unverified after it
is saved. An eBay asking price is the asking price captured at snapshot time; it is not represented
as current, and saving never checks whether the listing remains active.

Reference URLs remain inert metadata. Flipper does not fetch, resolve, scrape, or download them when
saving or displaying history. The snapshot contains bounded normalized fields, not raw marketplace
responses, OAuth tokens, authorization headers, or credentials.

Travel inputs and results remain modeled estimates. Research estimates never become actual
acquisition cost, expenses, or sale proceeds. A snapshot may be explicitly linked to an existing
inventory Q-number, but inventory remains authoritative and independent; linking copies no estimated
money into accounting.

For an explicitly linked Q-number, **Decision vs. Outcome** compares compatible facts per decision.
Expected values come only from the saved historical snapshot; actual values come from authoritative
inventory and recorded sale accounting. Recorded fees, shipping, refunds, adjustments, and credits
are included once through the existing accounting calculation. Missing data remains unknown, while
a known zero remains zero.

Variance is actual minus expected (ROI uses percentage points). It is descriptive, not a score,
success label, or recommendation. Snapshots are never rewritten or recomputed when working research
or formulas change. When actual sourcing travel is recorded on the linked Q-number, the comparison
also covers round-trip miles, fuel, additional expense, total expense, and travel minutes. Partial
actual data stays unknown where no fact exists.

Research history is stored in the selected local inventory SQLite database. Schema version 11 adds
a snapshot header plus a validated, canonical, versioned JSON payload. The header supports bounded
history queries and an optional inventory foreign key; the payload preserves the historical
aggregate without references to mutable working objects. Payload version 1 is validated on write and
read, and unsupported or malformed payloads fail safely. The migration is forward-only under
Flipper's normal ordered migration process; back up the database before upgrading because automatic
downgrade is not supported.

Snapshot deletion, editing captured content, CLI snapshot creation, and bulk export are deferred.
Use **Deals → Research History** to list and inspect saved records.
