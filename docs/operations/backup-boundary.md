# Backup boundary

The local inventory database and its sibling attachment directory form one backup unit. Copy both
from a quiescent application state and restore them together. Attachment metadata without its file,
or a file without its matching metadata, is incomplete.

Runtime databases and attachments are ignored by Git and are not a source-control backup. Credential
store entries, `.env`, and OAuth secrets are outside this data boundary and need a separate secure
recovery plan. Test restores to a separate path selected with `FLIPPER_INVENTORY_DB`; never test
against the working database. A coordinated built-in backup command is future work.
