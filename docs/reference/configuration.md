# Configuration

## eBay public discovery

The Deals workspace uses application OAuth, independently of seller authorization:

| Variable | Meaning |
| --- | --- |
| `EBAY_DISCOVERY_ENV` | `production` or `sandbox` |
| `EBAY_DISCOVERY_CLIENT_ID` | Environment-specific application client ID |
| `EBAY_DISCOVERY_CLIENT_SECRET` | Environment-specific application secret |
| `EBAY_DISCOVERY_MARKETPLACE` | Marketplace header; defaults to `EBAY_US` |

Browse production access requires eBay approval. Never expose the secret or minted Application token.

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
