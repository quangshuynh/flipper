# CLI reference

Run `python main.py --help` for the current command tree and `python main.py COMMAND --help` for exact
arguments. The main areas are:

| Area | Purpose |
| --- | --- |
| default pipeline / `acquire` | Analyze exported listings; explicitly record an acquisition |
| `inventory` | Inspect and maintain local items, lifecycle, valuations, and attachments |
| `sales` | Inspect sales, costs, and reconciliation confirmations |
| `reports` | Summary, inventory, and sales reports |
| `ebay` | Seller OAuth, read-only listing/order/Finances retrieval, reconciliation, and explicit local imports |

Commands use the local SQLite database selected by their documented option/environment default.
Always inspect `--help` before a mutation. The web application supports active-listing sync and
reviewed sale import; Finances import and advanced sale accounting remain CLI workflows.
