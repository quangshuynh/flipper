# Multi-source Deals workspace

Deals is a source-independent opportunity research workspace. It supports two entry paths:

- live, read-only eBay discovery through the Browse API; and
- manual opportunities from eBay, Facebook Marketplace, Mercari, Craigslist, estate sales, local
  or offline sources, and other sources.

Both paths normalize into the same deal model and use the same exact-Decimal economics, comparable
evidence, pairwise/Pareto comparison, and explicit acquisition workflow. Flipper does not calculate
an overall numeric winner.

Local-capable manual opportunities can model a trip from one-way mileage, vehicle MPG, manually
entered gas price, additional direct expense, and optional round-trip travel minutes. Derived fuel
and total travel cost feed landed cost once and remain research estimates. No map, route, location,
traffic, or gas-price service is contacted.

## Live eBay discovery

Discovery uses application OAuth and the configured Sandbox or Production Browse host. It searches
active fixed-price listings and performs no bidding, purchasing, messaging, listing modification,
or other marketplace write. Configure the environment-specific Client ID and Client Secret as
described in [eBay discovery](../guides/ebay-discovery.md).

eBay asking prices and available shipping data are API source facts. They are not sold prices,
expected resale values, or actual acquisition costs.

## Manual opportunities

Use **Add Opportunity** for listings or offline finds discovered elsewhere. Supported fields include
source, title, category, asking price and currency, inbound shipping, condition, HTTPS reference URL,
location text, and notes.

These values are user-provided source facts. Flipper does not authenticate them or represent them as
API-verified. A manually entered eBay listing therefore has different provenance from an eBay Browse
result. Reference URLs are validated as HTTPS URLs without embedded credentials, displayed as links,
and never fetched, scraped, resolved, or used for metadata or image downloads by the server.

Manual opportunities and their comparable evidence are process-local research state, isolated by a
browser cookie. The store retains at most 25 manual opportunities in each of at most 100 sessions;
older state is evicted and all state disappears when the process restarts. Inventory remains durable
and authoritative. Durable research history is intentionally deferred.

## Evidence and calculations

The workspace keeps these meanings separate:

- eBay Browse facts: source API provenance;
- manual listing details: user-provided source-fact provenance;
- resale, cost, and duration estimates: user assumptions;
- sold and active asking comparables: unverified user-provided market research; and
- landed cost, proceeds, profit, ROI, and velocity: Flipper calculations with input lineage.

Comparable medians never become expected resale automatically. Missing resale or cost assumptions
remain unknown, currencies stay separate, and evidence limitations remain visible.

## Comparison and acquisition

Two to four manual and/or eBay opportunities can be compared using the same available dimensions.
Missing or cross-currency dimensions are not invented. Pairwise dominance and the Pareto set describe
tradeoffs; they are not rankings or purchase recommendations.

Research becomes inventory only after **I bought this** is submitted with actual acquisition cost,
date, and source. Asking price, research date, estimated shipping, and expected resale never replace
those authoritative facts. Estimated fuel, mileage, travel time, and total travel cost also remain
research context; they do not become actual inventory or accounting expenses. Successful acquisition
uses the existing transactional Q-number allocator.

Future intervals may add local-sourcing intelligence, durable research history, and
decision-versus-outcome analysis. They are not part of the current workspace.
