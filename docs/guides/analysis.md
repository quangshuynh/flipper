# Analyze exported listings

Flipper reads user-provided JSON; it is not a crawler. The default pipeline collects the feed,
deduplicates seen IDs, parses secondhand-PC components, optionally enriches uncertain text, estimates
value, scores opportunities, and optionally sends alerts.

```bash
python main.py --feed data/listings.json
```

Deterministic parsing and heuristic pricing remain usable without network services. Asking price is
the seller's request. Ideal buy and resale values are estimates, not acquisition cost or realized
proceeds. Market-data observations are active asking prices rather than evidence of completed sales.

Use `--no-ai`, `--no-market`, or `--no-alert` to disable optional stages. Run `python main.py --help`
for the authoritative option list. A normal analysis never creates inventory; use the explicit
[acquisition workflow](acquisition.md) after a purchase decision.
