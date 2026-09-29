"""Safe local filesystem ownership for inventory attachments."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from inventory.store import (
    ATTACHMENT_CATEGORIES,
    AttachmentNotFoundError,
    AttachmentRecord,
    AttachmentValidationError,
    InventoryStore,
)

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
SUPPORTED_ATTACHMENTS = {
    ".jpg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
    ".jpeg": ("image/jpeg", lambda value: value.startswith(b"\xff\xd8\xff")),
    ".png": ("image/png", lambda value: value.startswith(b"\x89PNG\r\n\x1a\n")),
    ".webp": (
        "image/webp",
        lambda value: len(value) >= 12 and value[:4] == b"RIFF" and value[8:12] == b"WEBP",
    ),
    ".pdf": ("application/pdf", lambda value: value.startswith(b"%PDF-")),
}


class AttachmentService:
    """Coordinate attachment metadata with files rooted in one owned directory."""

    def __init__(self, store: InventoryStore, root: str | Path | None = None) -> None:
        self.store = store
        self.root = Path(root) if root is not None else store.path.parent / "attachments"

    def _root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root.resolve()

    def _owned_path(self, stored_filename: str) -> Path:
        if not stored_filename or Path(stored_filename).name != stored_filename:
            raise AttachmentValidationError("attachment storage identity is unsafe")
        root = self._root()
        candidate = (root / stored_filename).resolve()
        if candidate.parent != root:
            raise AttachmentValidationError("attachment path escapes the local attachment root")
        return candidate

    @staticmethod
    def _validate_source(source: Path) -> tuple[str, int]:
        if not source.is_file():
            raise AttachmentValidationError("attachment source file does not exist")
        extension = source.suffix.lower()
        supported = SUPPORTED_ATTACHMENTS.get(extension)
        if supported is None:
            raise AttachmentValidationError(
                "unsupported attachment type; use JPEG, PNG, WebP, or PDF"
            )
        size = source.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise AttachmentValidationError("attachment exceeds the 25 MiB per-file limit")
        with source.open("rb") as handle:
            signature = handle.read(16)
        media_type, signature_matches = supported
        if not signature_matches(signature):
            raise AttachmentValidationError("attachment contents do not match its file extension")
        return media_type, size

    def add(
        self, inventory_id: str, source: str | Path, *, category: str = "other"
    ) -> AttachmentRecord:
        if category not in ATTACHMENT_CATEGORIES:
            raise AttachmentValidationError(
                f"category must be one of: {', '.join(ATTACHMENT_CATEGORIES)}"
            )
        self.store.get(inventory_id)
        source_path = Path(source)
        media_type, byte_size = self._validate_source(source_path)
        attachment_id = str(uuid.uuid4())
        stored_filename = f"{attachment_id}{source_path.suffix.lower()}"
        final_path = self._owned_path(stored_filename)
        staging_path = self._owned_path(f".{attachment_id}.tmp")
        try:
            with source_path.open("rb") as source_handle, staging_path.open("xb") as target:
                shutil.copyfileobj(source_handle, target)
                target.flush()
                os.fsync(target.fileno())
            if staging_path.stat().st_size != byte_size:
                raise OSError("attachment copy size changed during import")
            staging_path.replace(final_path)
            try:
                return self.store.add_attachment_metadata(
                    inventory_id,
                    attachment_id=attachment_id,
                    original_filename=source_path.name,
                    stored_filename=stored_filename,
                    media_type=media_type,
                    byte_size=byte_size,
                    category=category,
                )
            except Exception:
                final_path.unlink(missing_ok=True)
                raise
        except Exception:
            staging_path.unlink(missing_ok=True)
            raise

    def list(self, inventory_id: str) -> list[AttachmentRecord]:
        return self.store.list_attachments(inventory_id)

    def path_for(self, record: AttachmentRecord) -> Path:
        path = self._owned_path(record.stored_filename)
        if not path.is_file():
            raise AttachmentNotFoundError("attachment file is missing from local storage")
        return path

    def remove(self, inventory_id: str, attachment_id: str) -> AttachmentRecord:
        record = self.store.get_attachment(attachment_id, inventory_id=inventory_id)
        final_path = self.path_for(record)
        staged_path = self._owned_path(f".{attachment_id}.removing")
        final_path.replace(staged_path)
        try:
            removed = self.store.remove_attachment_metadata(inventory_id, attachment_id)
        except Exception:
            staged_path.replace(final_path)
            raise
        staged_path.unlink(missing_ok=True)
        return removed
