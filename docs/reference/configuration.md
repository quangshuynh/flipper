# Configuration

Configuration comes from environment variables and optional `.env`; never commit that file.

| Setting family | Role |
| --- | --- |
| `FLIPPER_INVENTORY_DB` | Web application's local SQLite path |
| `FLIPPER_ATTACHMENT_ROOT` | Optional controlled attachment directory |
| `OPENAI_API_KEY` | Optional analysis enrichment |
| market-data variables | Optional asking-price lookup |
| Discord webhook | Optional alerts |
| `EBAY_SELLER_*` | Seller OAuth client, environment, and RuName |
| account-deletion variables | eBay compliance endpoint verification |

Use the CLI help and `.env.example` if one is present for exact names. Settings displays only safe
environment/configuration/connection booleans. Secrets, refresh tokens, authorization codes, buyer
data, deletion tokens, and raw payloads must never appear in pages, logs, docs, or commits.
