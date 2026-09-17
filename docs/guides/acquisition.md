# Record an acquisition

Acquisition joins one selected feed analysis to a new inventory item and baseline valuation in one
transaction. Provide the amount actually paid in USD and the actual acquisition date.

```bash
python main.py acquire --feed data/listings.json --listing-id LISTING_ID \
  --cost 241.37 --acquired-at 2026-09-16
```

Success allocates one non-reused Q-number. A failure rolls back both inventory and allocation. Feed
asking price, ideal-buy estimate, and estimated resale never substitute for acquisition cost.
Exported listing IDs are not assumed globally durable, so retry only when the first attempt failed.

## Record actual sourcing travel

Open the acquired Q-number in **Inventory** and use **Actual sourcing travel** after the trip. You
may record actual round-trip miles, fuel expense, additional travel expense, travel minutes, and a
note independently. These are authoritative user-entered facts; Flipper does not copy modeled
research values, infer fuel from MPG or gas prices, or use GPS, geocoding, routing, traffic, or gas
APIs. The feature offers no tax-deduction interpretation.

Blank means unknown and zero means a known zero. Total actual travel expense is shown only when
both fuel and additional expense are known, and equals their exact sum. Each recorded expense fact
is nevertheless included once in USD realized accounting. Editing replaces the authoritative
record and recalculates reports; clearing removes only the travel record.
