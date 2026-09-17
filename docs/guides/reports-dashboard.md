# Reports and dashboard

`reports summary`, `reports inventory`, and `reports sales` derive views without persisting
aggregates. The web dashboard reuses those services and accounting calculations.

Active inventory means acquired or listed. Tied-up capital excludes sold and archived items. Sale
ranges use inclusive UTC sale dates; inventory snapshots are current and are not retrospectively
date-filtered. Amounts stay grouped by currency. Recorded USD profit is separated by reconciliation
state and margins use aggregate recorded profit over positive gross.

Holding times require explicit lifecycle timestamps. Historical sell-through is unavailable because
the database has no defensible period-opening inventory or full transition history. The dashboard's
attention queue is computed only from local durable records. Every local page stays usable when eBay
is unavailable; only **eBay Listings** makes a live request.
