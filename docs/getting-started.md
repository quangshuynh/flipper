# Getting started

## Requirements

Use Python 3.13 or newer and Git. Clone the repository, create a virtual environment, and install
the runtime dependencies.

=== "Windows PowerShell"

    ```powershell
    py -3.13 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt
    ```

=== "macOS / Linux"

    ```bash
    python3.13 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt
    ```

## First analysis

`python main.py` reads `data/listings.json`. A feed is a JSON array of listing objects with an ID,
title, description, asking price, URL, location, and optional distance. The bundled fixture is safe
for trying the deterministic pipeline. See [Analysis](guides/analysis.md) before using your own data.

## Web application

```bash
uvicorn web.app:app --reload
```

Open `http://127.0.0.1:8000`. Set `FLIPPER_INVENTORY_DB` to select another local SQLite database.
The process initializes and migrates that database. Use a copy when experimenting.

## Optional integrations

Environment variables can enable OpenAI enrichment, market-data lookup, Discord alerts, and eBay
seller APIs. None is required for local deterministic parsing, heuristic pricing, inventory, or
reports. Consult [Configuration](reference/configuration.md) and never commit `.env` or tokens.
