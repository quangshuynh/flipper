# Quick Listing Prep

Quick Listing Prep organizes the decisions you make before listing an item you already own: what
to call it, which condition to choose, what to disclose, and what to ask. It is a local worksheet,
not a listing manager. Flipper never publishes, edits, or ends a marketplace listing.

## Open the workspace

From an acquired or listed item, choose **Prepare listing** on its inventory page, or use the
**Prepare listing** / **Listing prep** action in the Inventory table. The workspace lives at
`/inventory/Q0001/listing`. Sold and archived items show any saved draft read-only.

## What the workspace contains

| Area | Source | Notes |
| --- | --- | --- |
| Photos | Existing image attachments | Add photos with `python main.py inventory attach Q0001 photo.jpg --category product_photo`. |
| Listing title | Starts from the inventory title | Up to 80 characters. Editable. |
| Condition | You choose | `New`, `Open box`, `Used`, or `For parts or not working`. Flipper never infers condition from photos or titles. |
| Known defects | You enter | Inserted into the description template when you rebuild it. |
| Description | Deterministic template | Uses only the title, chosen condition, defects, and photo count. Bracketed placeholders such as `[confirm before listing]` mark what you must verify or delete. Inventory notes, source, and cost are never inserted. |
| Asking price and minimum offer | You enter | Labeled *User-provided*. The minimum offer cannot exceed the asking price. |
| Fee and shipping assumptions | You enter | Fee rate (%), fixed per-order fee, and seller-paid shipping. Enter your marketplace's current rates; blank means unknown. |
| Pricing evidence | Existing research only | The item's baseline valuation and explicitly linked saved research, each dated and labeled *Estimate*. |
| Estimated proceeds | Calculated | Shown only when every input is saved. |
| Copy-ready text | Current title and description | Read-only boxes to select and copy. |

**Save draft** stores the draft. **Rebuild description from template** replaces only the
description text on screen using the current condition and defects; nothing is saved until you
choose **Save draft**.

## Pricing evidence and the absence of it

Flipper shows a price reference only when dated research already exists for that Q-number. It does
not search for sold listings, invent comparables, or turn an estimate into a recommendation. With no
research on record, the workspace says so and leaves pricing to you. See
[Comparable evidence](../concepts/comparable-evidence.md) for recording verified sold evidence
during research.

## Estimated proceeds

```text
estimated fees      = round_half_up(asking price × fee rate) + fixed fee
estimated proceeds  = asking price − estimated fees − seller-paid shipping
estimated net       = estimated proceeds − recorded acquisition cost
```

All amounts are exact integer cents. If any input is blank, the affected result reads
*Unavailable* and the missing inputs are named; an explicit `0` is a known zero. The estimate
excludes taxes, promotions, refunds, and other unmodeled costs. It is never recorded as a sale, fee,
or profit. Realized results come only from [Sales & accounting](sales-accounting.md).

## Copying text

Flipper's [Content Security Policy](../operations/web-security.md) runs no scripts, so there is no
clipboard button. Tap or click a copy-ready box, select all, and copy.

## Data and boundaries

Each Q-number has at most one draft, stored in the `listing_drafts` table (schema v15). Saving a
draft never changes the item's status, Q-number, acquisition cost, valuations, research snapshots,
or sales. No marketplace permissions are requested, and no AI service is called. Drafts are part of
the local database, so they are included in [backups](../operations/backup-boundary.md).
