# Flipper

**Local-first resale intelligence and operations.** Flipper helps find and evaluate opportunities,
record what was actually acquired, track inventory, reconcile marketplace activity, and measure
recorded results without turning a marketplace account into the system of record.

The Deals workspace searches active eBay listings through the official Browse API. The original
analyzer remains a secondhand-PC specialization. Inventory, sales, accounting, and reporting span
the broader resale workflow.

## Product lifecycle

```text
Research → Save Snapshot → Acquire → Inventory → Sale / Reconciliation → Decision vs. Outcome → Insights
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

## Workflow

Research an opportunity in **Deals**, adding explicit assumptions, trip modeling, and comparable
evidence as needed. Working research remains temporary until you choose **Save research snapshot**.
After a purchase, record actual acquisition facts to create the authoritative inventory Q-item.

Track that item in **Inventory**, including actual sourcing travel and attachments. Record or import
the sale and reconcile its costs in **Sales**. When saved research is explicitly linked to the
Q-item, open **Decision vs. Outcome** from the snapshot, inventory item, or related sale. **Insights**
then summarizes eligible historical realized results; it is descriptive history, not a forecast or
recommendation.

## Boundaries

Flipper does not scrape marketplaces, mutate eBay, store searches as deal history, convert
currencies, or claim incomplete economics are final profit. Optional external integrations fail
independently from deterministic local paths. Browse results are active listings, not sold evidence.
