# Comparable evidence

Comparable evidence is deliberately entered research for one ephemeral eBay deal. It helps a user
judge a resale assumption without claiming that Flipper discovered, fetched, or verified the
market fact.

## Evidence types

Flipper supports two types with separate summaries:

- **Sold comparable** records a user-reported realized sale price. A sold/event date may be unknown
  rather than invented.
- **Active asking comparable** records a current asking price. It is never treated as a realized
  sale.

The Deals page mirrors that distinction with separate **Active listings** and **Sold comparables**
research modes. Active mode searches the official eBay Browse API and labels returned money as an
asking price. Sold comparables mode is a manual evidence workflow, not a sold-listing search: the
eBay access available to Flipper does not provide marketplace-wide completed-sale search. It directs
the user to evaluate an opportunity and deliberately attach a sold comparable. Switching modes
preserves applicable active search fields, but Sold comparables exposes no fake result sort, result
grid, or fallback active results.

Disappearing listings, inaccessible URLs, and active offers are not inferred to be sold. Completed
unsold, auction-result, dealer-specific, and user-estimate types are deferred until their semantics
and source requirements are clear.

Each record requires a type, named source, exact nonnegative price, three-letter currency, observed
date, and a condition choice that may explicitly be `unknown`. Optional fields are event date,
source identity and reference ID, title, HTTPS URL, notes, and the deal category. All manual records
are labeled **User-provided market research** and are not verified by Flipper.

## Exact summaries

Each deal can hold at most 25 records. Sold and active records are summarized independently, and
currencies never mix. For each type and currency Flipper reports count, minimum, maximum, and
median. With an even count, the median is the exact arithmetic mean of the two middle `Decimal`
amounts; binary floating point and currency conversion are not used.

The summary also exposes the known event-date window, unknown event-date count, and condition mix.
Limitations are plain statements rather than a score: small sample (fewer than three records), old
evidence (latest known event more than 180 days before the view date), mixed condition, unknown
event dates, incomplete condition metadata, active-only evidence, no sold evidence, and multiple
currencies.

Recency is descriptive. Flipper does not claim newer evidence is automatically better. A condition
mix likewise remains visible because a parts item and a tested item are not equivalent evidence.

## Relationship to analysis

Comparable evidence informs, but never fills, expected resale. The user assumption and sold median
are displayed separately. A median is not an acquisition fact or actual sale fact for the deal.
There is no automatic copy-to-resale action in this interval.

Counts and prices alone do not establish sampling coverage, sell-through, listing duration, or
time-to-sale. Flipper therefore does not derive liquidity or confidence scores from these records.
Comparison cards display the same separated summaries and limitations, but comparables do not add
a winner, rank, or hidden weight.

## Ephemeral ownership and safety

Research is process-memory state associated with a random, HTTP-only, same-site browser cookie and
the eBay item identity. It is bounded to 25 records per opportunity and 100 recent browser
workspaces. Restarting the web process clears it. This avoids creating durable deal rows merely to
attach research to temporary Browse results; inventory SQLite data remains authoritative.

Add, edit, and remove are POST-only actions using the existing bounded-body, same-origin, and 303
redirect pattern. Titles and notes render through Jinja escaping. Source URLs must be HTTPS, cannot
contain embedded credentials, open as external untrusted links, and are never requested by the
server. There is no scraping, browser automation, or bulk import.

## Future source and history boundary

A future permitted adapter can create the same typed records only when its API establishes the
evidence type, source identity, price/currency, dates, condition, and verification meaning. It must
retain source provenance and cannot reinterpret an unavailable listing as sold.

Flipper's own completed sales can eventually be one evidence source. Current records provide gross
sale price/currency, sold date, inventory title, acquisition facts, linkage, valuation snapshots,
and recorded costs. Meaningful similarity matching still lacks a normalized comparable-attribute
profile, consistently captured sale condition, listing-start history for older items, category on
all inventory, and a defensible sampling window. Gross sale is also not net proceeds. A historical
recommendation engine remains out of scope.
