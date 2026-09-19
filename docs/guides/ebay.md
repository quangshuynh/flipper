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

Fulfillment order retrieval does not prove a local sale from SKU alone. Explicit sale import accepts
only deterministic, one-to-one matches that satisfy the inventory state and money requirements.
Finances reconciliation uses stable order and line identifiers, never amount/title/buyer/date
guessing. Supported fee, refund, shipping-label, and related adjustment imports are idempotent;
unknown or ambiguous transactions remain unsupported.
