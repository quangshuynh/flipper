# Sales and recorded economics

Imported sales retain minimized identity, exact scaled seller revenue, currency, quantity, and sale
time. For attributable eBay orders, seller revenue is stored as separate item revenue and buyer-paid
shipping revenue components. Marketplace-collected tax and the customer checkout total are retained
only as non-revenue context. Sales exclude buyer PII. Exact re-import is a no-op; changed immutable
data is a conflict.
The Sales page contains authoritative local sales only; unreconciled eBay orders appear in the
linked eBay Sales review until the user explicitly imports a uniquely matched order line.

Sale cost components include marketplace fees, seller-paid shipping, refunds, other reductions,
and supported credits/reversals. Manual components remain distinct from eBay Finances imports.
Amounts are exact and must use the sale currency. Acquisition cost is USD, so Flipper withholds a
profit result for non-USD sales rather than converting currency. It also withholds profit and ROI
when acquisition cost is unknown; missing cost never participates as zero.

Recorded realized profit is derived from seller revenue, acquisition cost, recorded actual sourcing
fuel/additional travel expenses, and sale components. Actual sourcing travel is Q-linked acquisition-
side accounting, not a sale/shipping component; each recorded expense is subtracted exactly once.
It is not final merely because rows exist. Fees, shipping, refunds, and adjustments each require an
explicit confirmation; the sale is incomplete, partially reconciled, or fully reconciled based on
those confirmations. Payout-to-bank reconciliation and complete accounting remain out of scope.

While accounting is incomplete, web views label calculated recorded profit and margin as
**provisional** and keep final realized profit and ROI unavailable. A displayed zero acquisition
cost is a known recorded fact; absent fee, shipping, refund, or adjustment confirmations remain
unknown and are not claims that those costs were zero.

Buyer-paid shipping is revenue; seller-paid postage or label cost is a separate expense. Missing is
not zero. For example, an item price of USD 75 plus USD 8.07 buyer-paid shipping produces USD 83.07
seller revenue. If the marketplace also collects USD 3.98 tax, the USD 87.05 checkout total is not
seller revenue. A USD 12.24 marketplace fee reduces the amount after that known fee to USD 70.83,
but the result remains provisional while seller-paid shipping or other categories are unresolved.

Historical sales migrate without invented components: their existing aggregate seller-revenue
amount is preserved, while item revenue, buyer-paid shipping, marketplace tax, and checkout total
remain unknown. A later explicit eBay re-import may enrich a legacy item-only sale only when its
external identity and lifecycle facts match, its new components are all unknown, and its stored
amount exactly equals eBay's item revenue. Other changed economics remain conflicts; background
fetches never rewrite authoritative local sales.

Use `python main.py sales --help` and `python main.py ebay import-finances --help` for commands.
