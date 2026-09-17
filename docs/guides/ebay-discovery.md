# eBay deal discovery

The web [Deals workspace](http://127.0.0.1:8000/deals) is Flipper's first live discovery adapter.
It uses eBay's official Browse API and performs no scraping or marketplace writes. Search begins only
when a user submits keywords; there is no polling or background ingestion.

## Taxonomy mapping

eBay category trees are marketplace-specific. Flipper keeps its 18 stable categories and maps an
eBay leaf through official ancestry rather than copying eBay's taxonomy into the product model.

Commerce Taxonomy supplies the default tree ID, version, and hierarchy. Flipper builds one ancestry
index for a result set, caches at most eight environment/marketplace entries for 24 hours, and reuses
an unchanged version. See eBay's official
[Categories for Buy APIs](https://developer.ebay.com/api-docs/buy/buy-categories.html).

If Taxonomy is unavailable or malformed, Browse discovery still works. A known direct US top-level
ID is a weak fallback; otherwise Flipper uses **Everything Else** with unknown mapping confidence and
explicit fallback provenance. It never presents an unknown leaf mapping as authoritative.

## Authentication and configuration

Discovery uses an OAuth **Application access token** minted by the client-credentials grant. It is
separate from the eBay seller authorization-code flow and its keyring-held refresh token.

```dotenv
EBAY_DISCOVERY_ENV=production
EBAY_DISCOVERY_CLIENT_ID=...
EBAY_DISCOVERY_CLIENT_SECRET=...
EBAY_DISCOVERY_MARKETPLACE=EBAY_US
```

Production Browse access is subject to eBay's Buy API eligibility and approval requirements. The
Sandbox uses `api.sandbox.ebay.com`; Production uses `api.ebay.com`. Credentials and tokens must
never be committed or exposed in logs.

## Search and normalization

Search supports bounded keyword queries, one broad Flipper category, price range, condition, and
pagination. Flipper requests fixed-price active listings and maps supported eBay top-level category
IDs centrally into the 18 stable Flipper categories. Unrecognized IDs become `everything-else`.
That mapping is organizational evidence, not category-specific pricing expertise.

Available source facts can include the item ID, safe eBay URL, title, asking price and currency,
shipping quote, condition, source location, and listing timestamp. Missing shipping remains unknown.
An asking price never becomes an expected resale value or actual acquisition cost.

## Expected economics and assumptions

On a deal detail page, the user may temporarily supply estimated tax, travel, resale, fees,
outbound shipping, other costs, and a time-to-sale range. Every omitted component remains unknown;
Flipper does not silently replace it with zero. The shared generalized core calculates landed cost,
net proceeds, profit, ROI, capital tied up, and endpoint-based profit velocity only when inputs permit.

These are expected deal economics. They are distinct from:

- the eBay asking price, which is a source fact;
- actual acquisition cost and date, which the user supplies after a purchase;
- inventory valuation snapshots;
- realized sale proceeds and recorded accounting components.

Assumptions and search results are ephemeral and are not written to SQLite.

## Evidence limitation

Browse returns active purchasable listings. It does not provide a reliable generalized sold/completed
search dataset for this workflow. Flipper therefore does not fabricate sold comparable counts,
sell-through, median sold price, or time-to-sale. Active listings are not sold evidence. The detail
view states this limitation and treats time-to-sale as unknown unless the user supplies an estimate.

## Recording a purchase

`I bought this` is the durable boundary. Its POST form requires actual acquisition cost, actual
acquisition date, and actual source. The existing inventory service allocates the Q-number and retains
the eBay item identity. Asking price and analysis estimates are never substituted. This action changes
only the local inventory database; Flipper never buys, watches, offers, messages, or edits on eBay.
