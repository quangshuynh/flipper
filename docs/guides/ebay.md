# eBay seller integration

Flipper's eBay integration is read-only toward eBay. OAuth refresh tokens are kept in the operating
system credential store. Browser pages never handle OAuth codes or tokens.

```bash
python main.py ebay connect
python main.py ebay status
python main.py ebay disconnect
```

Reconnect after scope changes. Fulfillment orders, Finances transactions, and active listings are
normalized in memory and raw responses are discarded. Buyer contact, address, and payment details
are not stored.

## Active listings

Trading API active listings use an exact uppercase Q-number Custom Label/SKU. Read-only comparison
reports `MATCHED`, `MISSING_LOCAL`, `MISSING_SKU`, `INVALID_SKU`, or `CONFLICT`. Explicit sync may
fill compatible local linkage and move acquired inventory to listed; it never edits eBay. Missing
listings do not mutate local inventory. Importing a missing local item accepts acquisition source,
date, and cost when they are known, but permits genuinely unknown historical facts to remain
unknown. Blank cost is unknown; explicit `0.00` is a known free acquisition. The eBay asking price
never becomes acquisition cost.

## Orders and Finances

Active seller listings and completed seller orders come from different API families. Active-listing
sync uses Trading API `GetMyeBaySelling` with the base eBay OAuth scope; it does not use Browse or
Inventory API. Public deal discovery separately uses Buy Browse with an application token and is not
seller reconciliation. Flipper retrieves recent seller orders with Sell Fulfillment
`GET /sell/fulfillment/v1/order` and the `sell.fulfillment.readonly` seller scope. Existing seller
tokens issued before that scope was configured must be reauthorized. The web **eBay Sales** review fetches a transient 30-day
window when opened; there is no background synchronization. CLI order commands accept explicit date
ranges up to the API's two-year history window. The order response is independent of the active
listings feed, so a sold listing disappearing from ActiveList does not prevent reconciliation.

Fulfillment order retrieval does not prove a local sale from SKU alone. The review shows payment,
fulfillment, and cancellation states and matches each order line only when its SKU exactly equals the
Custom Label/Q-number of one eBay-linked local inventory record. Missing, unmatched, and ambiguous
SKUs remain visible and cannot be imported from the review. Q-number matching is case-sensitive.

The user must select **Import matched sale**. Explicit sale import accepts only deterministic,
one-to-one matches whose local and external quantities are both one, whose item money and
attributable buyer-paid shipping are present, and whose local item is acquired or listed. It then
atomically creates the authoritative
local Sale and marks the item sold. An acquired item keeps `listed_at` unknown rather than inventing
a listing timestamp. Stable marketplace + order + line-item identity makes exact repeats no-ops and
turns changed immutable facts into conflicts. Opening or refreshing the review never changes local
inventory or accounting.

Fulfillment `lineItems[].lineItemCost` supplies item revenue. `pricingSummary.deliveryCost` supplies
buyer-paid shipping at order level, while `pricingSummary.tax` is marketplace-collected tax and
`pricingSummary.total` is customer checkout context. Flipper imports item plus buyer-paid shipping
as seller revenue only when shipping attribution is exact: a single-line order, or an explicitly
zero-shipping multi-line order. It does not allocate nonzero order shipping across lines. Tax never
enters revenue, profit, expense, or adjustments. Delivery discounts and line promotions remain
transient order context; no amount is inferred by subtracting totals.

The Fulfillment response is not Flipper's source for marketplace fees. Fees remain unknown until
entered manually with `sales add-cost --category marketplace_fee` or imported from the existing
read-only Finances workflow. Seller-paid label/shipping expense is independent of buyer-paid
shipping and likewise remains unknown until separately imported or entered. Order responses and
buyer data are not persisted.

Finances reconciliation uses stable order and line identifiers, never amount/title/buyer/date
guessing. Supported fee, refund, shipping-label, and related adjustment imports are idempotent;
unknown or ambiguous transactions remain unsupported.
