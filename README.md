<p align="center">
  <img src="docs/images/flipper-logo.png" alt="flipper" width="256">
</p>

<p align="center">
  <a href="https://github.com/quangshuynh/flipper/actions/workflows/ci.yml"><img src="https://github.com/quangshuynh/flipper/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.13%2B-blue.svg" alt="Python 3.13+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License"></a>
</p>

Flipper is a local-first resale intelligence and operations tool for evaluating opportunities,
tracking acquired inventory, reconciling marketplace activity, and measuring realized results.
Its current analyzer began with exported secondhand-computer listings. The generalized core can now
model manual opportunities across categories; vendor ingestion remains future work.

```text
Find the flip → Understand the economics → Acquire it → Track inventory
             → Sell it → Reconcile costs → Measure what actually happened
```

## What Flipper does

- Analyzes user-provided JSON listing exports with deterministic parsing, valuation, and scoring.
- Creates durable Q-number inventory records from explicit acquisitions.
- Tracks lifecycle, attachments, valuations, sales, costs, and reconciliation in local SQLite.
- Reads eBay seller listings, orders, and Finances data through authorized APIs without marketplace writes.
- Presents local operational reports in a server-rendered web dashboard.

Flipper is not a marketplace crawler, a complete accounting system, or an eBay listing manager.

## Quick start

Python 3.13 or newer is recommended.

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python main.py
```

See the [getting-started guide](docs/getting-started.md) for platform-specific activation,
configuration, feed structure, and optional integrations.

## Web app

```bash
uvicorn web.app:app --reload
```

Open `http://127.0.0.1:8000`. The dashboard uses local data; only the eBay Listings page performs
an isolated live request.

## Documentation

The source documentation lives in [`docs/`](docs/index.md). Build it locally with:

```bash
python -m pip install -r requirements-dev.txt
mkdocs serve
```

GitHub Pages is enabled with **GitHub Actions** as its source. The documentation from this branch
will be published at <https://quangshuynh.github.io/flipper/> only after merge and a successful
deployment workflow.

## Local-first principles

The inventory database is authoritative. Runtime databases, attachments, credentials, tokens,
buyer details, and raw marketplace responses do not belong in Git. Optional AI, Discord,
market-data, and eBay integrations are separable from deterministic local workflows.

## Development

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
mkdocs build --strict
```

See [architecture](docs/development/architecture.md), [testing and migrations](docs/development/testing-migrations.md),
and the repository's `AGENTS.md` before changing persistent or marketplace behavior.

## License

MIT. See [LICENSE](LICENSE).
