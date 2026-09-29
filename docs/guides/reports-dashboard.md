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

## Phone and small screens

The same server-rendered pages adapt to phone widths (checked at 375, 390, and 430 CSS pixels) and
to desktop; there is no separate mobile app, script, or menu. Below 900 pixels the primary
navigation wraps into a grid so every destination stays visible without sideways swiping, and
**Sign out** (hosted mode) sits beside the Flipper brand. Dense tables keep all of their columns:
they scroll sideways inside their own panel, keep the first identity column pinned, and can be
focused and scrolled with the keyboard. Long titles, identifiers, filenames, and exact amounts wrap
instead of widening the page.

On phone or touch layouts, form controls render text at 16 pixels or larger, so iOS Safari does
not zoom in when a field is focused, and primary controls are at least 44 pixels tall. Money fields
request a decimal keypad and whole-number fields a numeric keypad; these are keyboard hints only,
and the server still validates every value exactly. Pinch-to-zoom is deliberately left enabled for
accessibility. Reaching Flipper from a phone requires the security setup in
[Web security](../operations/web-security.md); hosted deployment is not yet available.
