# Sales and recorded economics

Imported sales retain minimized identity, exact scaled gross money, currency, quantity, and sale
time. They exclude buyer PII. Exact re-import is a no-op; changed immutable data is a conflict.
The Sales page contains authoritative local sales only; unreconciled eBay orders appear in the
linked eBay Sales review until the user explicitly imports a uniquely matched order line.

Sale cost components include marketplace fees, seller-paid shipping, refunds, other reductions,
and supported credits/reversals. Manual components remain distinct from eBay Finances imports.
Amounts are exact and must use the sale currency. Acquisition cost is USD, so Flipper withholds a
profit result for non-USD sales rather than converting currency. It also withholds profit and ROI
when acquisition cost is unknown; missing cost never participates as zero.

Recorded realized profit is derived from gross proceeds, acquisition cost, recorded actual sourcing
fuel/additional travel expenses, and sale components. Actual sourcing travel is Q-linked acquisition-
side accounting, not a sale/shipping component; each recorded expense is subtracted exactly once.
It is not final merely because rows exist. Fees, shipping, refunds, and adjustments each require an
explicit confirmation; the sale is incomplete, partially reconciled, or fully reconciled based on
those confirmations. Payout-to-bank reconciliation and complete accounting remain out of scope.

Use `python main.py sales --help` and `python main.py ebay import-finances --help` for commands.
