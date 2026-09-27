# Troubleshooting

## The analyzer skips every listing

Use a fresh deduplication database or new listing IDs. Seen IDs are intentionally persistent.

## The web app opens the wrong inventory

Set `FLIPPER_INVENTORY_DB` (or `FLIPPER_DATA_DIR`) before starting Uvicorn and confirm the path;
the server migrates the selected database at startup. Do not point tests at a developer database.

## My phone cannot reach Flipper

See the troubleshooting table in [Phone access](../guides/phone-access.md#troubleshooting).

## eBay says authorization is unavailable

Check safe status in Settings or `python main.py ebay status`, verify seller client configuration,
then disconnect/connect to grant current scopes. Tokens stay in the configured credential store
(the OS keyring by default).

## A sale or transaction will not import

Read the explicit state: missing/ambiguous identity, incompatible lifecycle/quantity, unsupported
transaction type, missing money, and immutable-data conflicts are protective failures. Do not “fix”
them by inventing facts.

## Documentation build fails

Install `requirements-dev.txt` and run `mkdocs build --strict` from the repository root. Strict mode
treats broken navigation, missing files, and documentation warnings as failures.
