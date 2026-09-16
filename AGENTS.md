# Flipper repository guidance

Flipper is a local-first Python tool for evaluating exported secondhand-computer listings and
maintaining acquisition inventory. It is not a marketplace crawler, a complete accounting system,
or an eBay listing-management tool.

- Audit the repository and its tests before implementing. Preserve existing behavior and do not
  silently change valuation, pricing, or scoring while working on unrelated features.
- Keep deterministic parsing, pricing, inventory, and test paths usable locally. Optional AI,
  market-data, Discord, and eBay integrations must fail independently where practical.
- The CLI in `main.py` coordinates collectors, parsing, pricing, alerts, local inventory, and the
  read-only eBay seller integration. Inventory uses a versioned SQLite store under `inventory/`.
- Treat the local inventory database as authoritative. eBay is external: seller OAuth and
  Fulfillment access are read-only, order responses are not persisted, and SKU linkage alone does
  not prove a sale. Do not add marketplace mutations or scraping when APIs/local exports suffice.
- Never commit, log, or expose credentials, OAuth codes/tokens, webhook URLs, or secrets. Store no
  unnecessary eBay/customer PII (including buyer contact or address data).
- Persist money exactly (currently integer USD cents for acquisition cost). Protect quantity,
  status, stable identity, transactionality, and non-reuse of human-facing Q-numbers. Distinguish
  asking prices from realized sale proceeds and account for unmodeled fees/costs in claims.
- Add focused tests using temporary databases; never touch a developer's runtime database. Run
  `pytest`, `ruff check .`, and `ruff format --check .` before handoff.
- Make every persistent schema change through an ordered migration. Preserve old databases and
  test upgrades as migrations are added.
- Keep runtime databases and local `context.md` uncommitted. Treat `main` as the protected
  integration branch: normally start feature, fix, refactor, and documentation work on a descriptive
  branch from an up-to-date `main`. Use normal commits, push the branch to origin, and open a pull
  request targeting `main`. Do not merge the PR unless the user explicitly requests it. Never
  rewrite shared history or force-push unless the user explicitly authorizes it for a specific
  reason. Do not create a release or tag unless explicitly requested.
