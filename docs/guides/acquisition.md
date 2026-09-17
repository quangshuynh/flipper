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
