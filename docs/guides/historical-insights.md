# Historical reseller insights

Open **Insights** in the local web application to review factual aggregate outcomes from completed
sales. The page provides an overall summary plus neutral tables by acquisition source and normalized
Flipper category. It is descriptive history only: it does not rank groups, recommend purchases, or
forecast results.

## Eligibility and date ranges

A completed item is one with an authoritative durable sale record. Gross revenue uses the sale's
recorded native currency and is never converted. The all-time, 30-day, 90-day, and 365-day views
filter inclusively on the UTC `sold_at` date; acquisition date is not used for the filter.

Realized profit and ROI require both fully reconciled accounting and a USD sale, because acquisition
cost is recorded in USD and Flipper does not perform currency conversion. Sales with incomplete or
partially reconciled fees, shipping, refunds, or adjustments remain in completed and revenue counts,
but are explicitly counted as incomplete and excluded from profit and ROI. Fully reconciled non-USD
sales are separately identified as currency-incompatible. Missing values are never replaced with
zero; a known zero remains eligible.

## Metric definitions

- Realized profit reuses Flipper's authoritative sale economics: gross revenue minus acquisition
  cost, actual sourcing travel, and reducing sale components, plus recorded credits.
- Realized ROI is realized profit divided by positive recorded cost basis. Results with a zero or
  negative denominator do not produce an ROI.
- Holding time is the number of whole days from the authoritative acquisition date through the UTC
  sale date. Invalid chronology is unavailable.
- Typical values use exact `Decimal` medians. Every grouped result and every median exposes its
  sample count, with samples below three visibly noted as small.

Actual sourcing travel is already part of authoritative realized-profit accounting and is included
exactly once. Modeled travel and research expectations are not included.

## Grouping

Source grouping uses the acquisition source stored on inventory. Category grouping uses only a
normalized category from research explicitly linked to the inventory item. Items without one clear
linked category remain **Unknown**; titles are never reclassified for analytics. Groups are ordered
by completed count and then name, not by profit or ROI.
