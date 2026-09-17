# Flipper

**Local-first resale intelligence and operations.** Flipper helps find and evaluate opportunities,
record what was actually acquired, track inventory, reconcile marketplace activity, and measure
recorded results without turning a marketplace account into the system of record.

The Deals workspace searches active eBay listings through the official Browse API. The original
analyzer remains a secondhand-PC specialization. Inventory, sales, accounting, and reporting span
the broader resale workflow.

## Product lifecycle

```text
Find → Evaluate → Acquire → Track → Sell → Reconcile → Learn
```

| Start here | What it covers |
| --- | --- |
| [Getting Started](getting-started.md) | Installation, first analysis, and the local web app |
| [Deal Intelligence](concepts/deal-intelligence.md) | Exact expected economics, evidence, risk, and tradeoffs |
| [eBay Discovery](guides/ebay-discovery.md) | Live read-only search, assumptions, and acquisition boundary |
| [Inventory](guides/inventory-attachments.md) | Q-numbers, lifecycle, attachments, and valuations |
| [Selling](guides/sales-accounting.md) | Sales, costs, reconciliation, and reports |
| [Architecture](development/architecture.md) | Domain boundaries and service layout |
| [Development](development/testing-migrations.md) | Tests, formatting, and migrations |

Start with [installation and setup](getting-started.md), then choose a task from the Guides section.

## Boundaries

Flipper does not scrape marketplaces, mutate eBay, store searches as deal history, convert
currencies, or claim incomplete economics are final profit. Optional external integrations fail
independently from deterministic local paths. Browse results are active listings, not sold evidence.
