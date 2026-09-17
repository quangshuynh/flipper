# Generalized deal intelligence

Flipper's deal core compares opportunity economics without pretending that nominal profit is the
only useful dimension. It is source-neutral, ephemeral, and separate from acquired inventory and
realized sale accounting. Representing a category or source does not mean an integration or pricing
model exists for it.

## Model layers

1. A source or user supplies raw facts.
2. Source and category normalization produce a `DealOpportunity`.
3. Exact inputs produce `DealEconomics`.
4. Liquidity, confidence, and risk evidence add context.
5. Evaluation and comparison expose dimensions and tradeoffs without a weighted global score.

The existing PC pipeline remains a specialization: its deterministic component parser, optional AI
enrichment, heuristic pricing, and legacy score are unchanged. An adapter exposes known PC results
as an Electronics opportunity. The first live adapter now maps active eBay Browse results into this
model; other sources and generalized sold-market evidence are not implemented.

## Categories and sources

Stable category slugs are separate from display labels:

| Slug | Display label |
| --- | --- |
| `vehicle-parts` | Vehicle Parts |
| `electronics` | Electronics |
| `collectibles-art` | Collectibles & Art |
| `home-garden` | Home & Garden |
| `clothing-shoes-accessories` | Clothing, Shoes & Accessories |
| `toys-hobbies` | Toys & Hobbies |
| `sporting-goods` | Sporting Goods |
| `books-movies-music` | Books, Movies & Music |
| `health-beauty` | Health & Beauty |
| `business-industrial` | Business & Industrial |
| `jewelry-watches` | Jewelry & Watches |
| `baby-essentials` | Baby Essentials |
| `pet-supplies` | Pet Supplies |
| `tickets-travel` | Tickets & Travel |
| `everything-else` | Everything Else |
| `real-estate` | Real Estate |
| `gift-cards-coupons` | Gift Cards & Coupons |
| `specialty-services` | Specialty Services |

Normalized source identities include eBay, Facebook Marketplace, Mercari, Craigslist, Estate Sale,
Local, and Other. They are identifiers for future adapters, not claims of current support.

## Exact expected economics

Money uses `Decimal` plus a three-letter currency. Binary floating-point input is rejected. Flipper
does no currency conversion and returns a currency-mismatch state when required values cannot be
combined. Each optional component is actual, estimated, unknown, or not applicable. Unknown is not
zero; an explicitly unknown component prevents the affected aggregate from being calculated.

```text
landed acquisition cost = base price + tax + inbound shipping + travel + other acquisition costs
expected net proceeds = expected resale - selling fees - outbound shipping - other selling costs
expected net profit = expected net proceeds - landed acquisition cost
expected ROI = expected net profit / landed acquisition cost
capital tied up = landed acquisition cost
```

## Local trip economics

For an in-person opportunity, Deals can decompose the existing travel component from explicit
assumptions:

```text
round-trip miles = one-way miles × 2
estimated gallons = round-trip miles / vehicle MPG
estimated fuel cost = estimated gallons × gas price per gallon
total modeled travel cost = estimated fuel cost + additional direct travel expense
```

One-way distance is always labeled explicitly; Flipper never guesses whether mileage is one-way or
round-trip. Distance, MPG, gas price, additional expense, and round-trip travel minutes are user
assumptions. Round-trip miles, gallons, fuel cost, and total travel cost are Flipper calculations.
The total enters the existing landed-cost calculation exactly once.

Intermediate Decimal values retain their precision. Currency display rounds to the normal two-place
money presentation; the calculation does not round fuel or cost early. Gas price is manually entered,
not live or verified. Missing MPG or another required fuel input leaves fuel and total travel cost
unknown rather than treating fuel as free.

Travel time is sourcing-effort context. It is not time-to-sale and is not converted to wages or an
opportunity cost. Flipper performs no geocoding, routing, traffic lookup, GPS tracking, gas-price
lookup, or external request from trip inputs.

The older direct travel-cost input remains available as an alternative. It cannot be combined with
distance/MPG/gas/additional-expense components, preventing double-counting.

Tax is only source- or user-supplied. Inbound and outbound shipping remain distinct. Fee amounts are
explicit inputs rather than a universal marketplace percentage. Expected values never become
realized accounting entries. A zero-cost opportunity has an explicit ROI state, never infinity.

## Time, evidence, and risk

Time-to-sale is unknown or an attributed range. Profit velocity is calculated only when expected
profit and that range exist: conservative profit/day divides by maximum days; optimistic profit/day
divides by minimum days. No midpoint is invented.

Liquidity can carry sold and active comp counts, sell-through ratio, typical sold time, price
dispersion, recency, and comp quality. Confidence is decomposed into pricing, time-to-sale,
condition, and category-match evidence. Missing evidence stays missing. Risks are explicit
code-plus-explanation factors, not an opaque score.

## Comparison example

| Dimension | Opportunity A | Opportunity B |
| --- | ---: | ---: |
| Landed capital | $250 | $55 |
| Expected net profit | $80 | $40 |
| Expected ROI | 32% | 72.73% |
| Time-to-sale | 35 days | 6 days |
| Expected profit/day | $2.29 | $6.67 |

A has greater nominal profit. B uses less capital, has higher ROI, and has greater profit velocity.
The comparator reports a tradeoff rather than forcing a universal winner.

## Manual CLI input

This calculates a synthetic opportunity and writes nothing to inventory:

```bash
python main.py deals analyze --title "Test opportunity" --source local \
  --category electronics --base-price 55 --expected-resale 95 \
  --one-way-distance 12 --vehicle-mpg 28 --gas-price 3.45 \
  --additional-travel-cost 2 --travel-minutes 40 \
  --minimum-sale-days 4 --maximum-sale-days 7
```

The command performs no lookup, geocoding, persistence, or marketplace request.
