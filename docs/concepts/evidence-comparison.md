# Evidence provenance and comparison

Flipper keeps four kinds of economic information visibly separate:

| Kind | Example | Meaning |
| --- | --- | --- |
| Source fact | eBay asking price | Returned by a named external API for the current listing. |
| Estimate | Expected resale entered during analysis | A temporary user assumption, not an observed sale. |
| Derived value | Expected net profit | A Flipper calculation over named facts and estimates. |
| Actual historical fact | Acquisition cost or sale proceeds | A durable fact recorded after an event. |

Provenance answers **where a value came from**. Confidence answers **how strong the evidence is**.
A value can have clear provenance and weak or unknown confidence.

## Derived economics

- Landed cost uses asking price, tax, inbound shipping, travel, and other acquisition cost.
- Expected net proceeds uses expected resale less selling fees, outbound shipping, and other costs.
- Expected net profit uses expected net proceeds less landed cost.
- ROI uses expected net profit and landed cost.
- Profit velocity uses expected net profit and the supplied time-to-sale range.

Unknown inputs remain unknown. Native currencies are never compared or combined as equivalent.

## Comparison workspace

Search results can select **two to four** opportunities for `/deals/compare`. Flipper sends only
bounded source identifiers, re-fetches each listing from eBay, and applies independently named
temporary assumptions to each deal. Submitted economics cannot overwrite an eBay source fact. The
workspace creates no deal-history, inventory, or accounting rows.

On narrow screens, comparisons stack as complete cards. Pairwise results list dimensions favoring
each opportunity. A non-dominated (Pareto) opportunity is not worse on available comparable
dimensions; it is not a winner, score, grade, or buy recommendation.

## Sold-market evidence

As reviewed in September 2026:

- [Browse API](https://developer.ebay.com/api-docs/buy/api-browse.html) returns purchasable listings,
  not a generalized completed-sales dataset.
- eBay describes Marketplace Insights as its sales-history API, but the
  [marketplace support page](https://developer.ebay.com/api-docs/buy/ref-marketplace-supported.html)
  says it is restricted and not open to new users.
- Production Buy access requires approval and agreements under the
  [Buy API requirements](https://developer.ebay.com/api-docs/buy/buy-requirements.html).
- Seller Fulfillment and local sales cover the connected seller's own history, not market-wide comps.
- A future user-provided export would need a defined schema, rights, provenance, and validation.
  Scraping sold/completed pages is not a fallback.

No realistically available official sold-data source currently passes the implementation gate.
Sold counts, sell-through, historical median prices, and market time-to-sale remain unavailable.

Flipper's inventory, valuations, lifecycle timestamps, sales, and reconciled costs are promising
future evidence. Later work needs comparable attributes, complete lifecycle windows, sample-size
disclosure, and currency compatibility before learning from those records.

User-provided [comparable evidence](comparable-evidence.md) can now accompany each ephemeral
opportunity. Sold and active asking summaries remain separate and currency-specific. They do not
enter Pareto ranking, expected resale, liquidity, or confidence automatically.
