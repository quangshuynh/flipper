# Architecture

Flipper is one Python application with a CLI coordinator (`main.py`) and a server-rendered FastAPI /
Jinja web interface. Domain modules own parsing, pricing, inventory, eBay normalization and
reconciliation, sales economics, and reports. Web routes reuse those services; templates contain no
SQL or accounting formulas. CSS is static and requires no Node build.

SQLite under `inventory/` is authoritative and versioned by ordered migrations. Attachments are
controlled local files. External eBay data is normalized in memory and only supported explicit
imports create minimized records. The dashboard's local pages do not depend on live eBay; the eBay
Listings route deliberately isolates its live read.

Keep optional AI, market-data, Discord, and marketplace paths independently fallible. Preserve exact
money, stable identity, transactionality, privacy boundaries, and existing valuation/scoring when
working on unrelated features.

The ephemeral `deals/` domain layers category/source normalization, `DealOpportunity`, exact expected
economics, evidence/risk, and dimension-preserving comparison. The legacy PC parser and estimator
adapt into that domain but retain their existing behavior. Deal analysis does not touch the
inventory schema or realized-accounting services.
