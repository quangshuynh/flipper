# CSV export

Flipper exports authoritative local inventory and completed sales as CSV for spreadsheets or
archiving. Exports are read-only projections: they never write to the database, never call eBay,
and reuse the same calculations as the Sales and Reports views.

- Web: **Export CSV** on the Inventory and Sales pages (`/exports/inventory.csv`,
  `/exports/sales.csv`). Each download is the full dataset; page filters do not apply.
- CLI: `python main.py reports export {inventory,sales} [--output FILE]`. Without `--output` the
  CSV goes to standard output. A file target is replaced atomically, so a failed export leaves any
  previous file untouched.

## Format

- UTF-8 with a byte-order mark, RFC 4180 quoting, CRLF row endings, and a fixed column order.
- Inventory rows are ordered by Q-number; sales rows by sale time, then sale creation order.
  Identical data produces byte-identical files.
- **An empty cell means unknown, not recorded, or not applicable. `0.00` is a known zero.**
- Money is plain decimal text with at least two fractional digits, taken from exact stored values.
  Sub-cent stored precision is kept, and nothing is rounded, grouped, or converted between currencies.
- Timestamps are UTC `YYYY-MM-DDTHH:MM:SSZ`; acquisition dates are `YYYY-MM-DD`.
- Text that begins with `=`, `+`, `-`, `@`, a tab, or a line break is prefixed with `'` so
  spreadsheets show it as text instead of running it as a formula. Generated amounts such as `-2.00`
  are never prefixed.

## Inventory columns

`inventory_id`, `title`, `status`, `quantity`, `acquisition_source`, `acquired_at`,
`acquisition_cost_usd`, `sourcing_fuel_cost_usd`, `sourcing_additional_expense_usd`, `marketplace`,
`marketplace_item_id`, `marketplace_sku`, `listed_at`, `sold_at`, `sale_id`, `estimate_currency`,
`estimate_expected_resale_value`, `estimate_gross_profit`, `estimate_analyzed_at`, `notes`.

The `estimate_*` columns come from the item's baseline valuation snapshot. They are research
estimates, not accounting facts, and are never used in the sales export.

## Sales columns

`sale_id`, `inventory_id`, `title`, `marketplace`, `marketplace_sku`, `external_order_id`,
`external_line_item_id`, `quantity`, `acquired_at`, `sold_at`, `days_held`, `currency`,
`seller_revenue`, `item_revenue`, `buyer_shipping_revenue`, `acquisition_cost_usd`,
`sourcing_travel_expense_usd`, `marketplace_fees`, `seller_shipping_cost`, `refunds`,
`other_adjustments_net`, `reconciliation_state`, `unconfirmed_categories`, `recorded_profit`,
`realized_profit`, `profit_status`.

Sale amounts use the row's `currency`; acquisition and sourcing travel are USD. Buyer PII and
marketplace-collected tax are not exported.

- Each cost category column holds the net recorded amount for that category: reductions minus
  credits, so a net credit is negative. It is empty when the category is unconfirmed and nothing
  is recorded, and `0.00` when it was confirmed as zero. A recorded amount in a category listed in
  `unconfirmed_categories` has not been reviewed yet.
- `recorded_profit` is seller revenue minus acquisition cost, recorded sourcing travel, and net
  recorded costs. This is the same value the Sales page shows.
- `realized_profit` is filled only when the sale is `fully_reconciled`. Otherwise it is empty and
  `profit_status` is `provisional`.
- Profit is empty, with an `unavailable: …` status, when acquisition cost is unknown or the sale is
  not in USD.

Fully reconciled still does not mean the payout was matched to a bank deposit, and it is not a tax
figure. See [Sales & accounting](sales-accounting.md) and [Reconciliation](../concepts/reconciliation.md).
