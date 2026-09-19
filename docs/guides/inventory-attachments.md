# Inventory and attachments

Inventory is authoritative local state. `python main.py inventory --help` shows add, list, show,
update, and lifecycle commands. Records retain source, acquisition date, exact integer USD cents,
quantity, notes, optional marketplace linkage, and timestamps. A missing-local eBay import may have
unknown historical source, acquisition date, or cost; these remain visibly unknown and are never
treated as zero. Notes can be edited or cleared from inventory detail. Generic updates cannot bypass
lifecycle rules.

## Attachments

Attachment metadata lives in SQLite and bytes live in an ignored sibling `attachments/` directory.
JPEG, PNG, WebP, and PDF files up to 25 MiB are accepted only when extension and signature agree.
Original names are display metadata; controlled UUID-based names prevent path collisions.

```bash
python main.py inventory attachment-add Q0001 receipt.jpg --category receipt
python main.py inventory attachment-list Q0001
python main.py inventory attachment-remove Q0001 ATTACHMENT_ID
```

The web detail page previews or downloads only files owned by the requested Q-number. Browser upload
and deletion are intentionally unavailable. Sold and archived items retain their attachments. Back
up the database and attachment directory together; see [Backup boundary](../operations/backup-boundary.md).
