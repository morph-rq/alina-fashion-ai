"""Durable capture of evidence into the unified source inbox.

Order of operations for every capture:

1. Write the bytes to a temporary file next to their final content-addressed
   location, flush and fsync, re-read and compare the SHA-256.
2. Atomically rename into ``data/sources/<aa>/<bb>/<sha256>``.
3. Only then insert the ``SourceBlob``/``SourceItem`` rows.

A capture is never reported successful before step 2 succeeds. If the database
insert fails afterwards the stored file is harmless: it is content-addressed and
is reused by the next capture of the same bytes. Remote identity
(channel, account, remote id, version) makes replays return the existing item.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ROOT_DIR
from app.models import SourceBlob, SourceItem

SOURCE_ROOT_RELATIVE = "data/sources"
MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_SPREADSHEET_BYTES = 1024 * 1024 * 1024   # supplier workbooks with many photos


def source_size_limit(filename: str) -> int:
    return MAX_SPREADSHEET_BYTES if Path(filename).suffix.lower() in {".xlsx", ".xlsm"} else MAX_SOURCE_BYTES

INBOX_STATES = ("captured", "queued", "analysing", "needs_answers", "ready_for_review", "applied", "failed", "ignored")
_SIGNATURES = [
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"PK\x03\x04", "application/zip"),
]
_ZIP_FAMILY = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel.sheet.macroEnabled.12",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/zip",
}


class CaptureError(ValueError):
    pass


@dataclass
class CaptureResult:
    item: SourceItem
    created: bool
    blob_reused: bool


def source_root(root: Path | None = None) -> Path:
    return (root or ROOT_DIR) / SOURCE_ROOT_RELATIVE


def sniff_media_type(content: bytes, supplied: str | None, filename: str = "") -> str:
    """Prefer the content's own signature over a supplied (untrusted) type."""
    supplied = (supplied or "application/octet-stream").split(";", 1)[0].strip().casefold()
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    for signature, media_type in _SIGNATURES:
        if content.startswith(signature):
            if media_type == "application/zip":
                if supplied in _ZIP_FAMILY:
                    return supplied
                if filename.casefold().endswith((".xlsx", ".xlsm")):
                    return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return media_type
    if supplied.startswith(("image/", "application/pdf")) or supplied in _ZIP_FAMILY:
        return "application/octet-stream"  # Claimed binary type without its signature.
    return supplied


def _store_bytes(content: bytes, root: Path | None) -> tuple[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    relative = f"{SOURCE_ROOT_RELATIVE}/{digest[:2]}/{digest[2:4]}/{digest}"
    final = (root or ROOT_DIR) / relative
    if final.is_file() and hashlib.sha256(final.read_bytes()).hexdigest() == digest:
        return digest, relative
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.with_name(f".{digest}.{secrets.token_hex(4)}.partial")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
            raise CaptureError("Stored bytes did not match their checksum; nothing was captured.")
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return digest, relative


def find_item(db: Session, channel: str, account: str, remote_id: str, remote_version: str = "") -> SourceItem | None:
    return db.scalar(select(SourceItem).where(
        SourceItem.channel == channel, SourceItem.account == account,
        SourceItem.remote_id == remote_id, SourceItem.remote_version == remote_version))


def capture(db: Session, *, channel: str, content: bytes | None, original_name: str = "", media_type: str | None = None,
            account: str = "", remote_id: str | None = None, remote_version: str = "", metadata: dict | None = None,
            source_link: str | None = None, session_id: int | None = None, parent_id: int | None = None,
            state: str = "captured", state_reason: str | None = None, root: Path | None = None) -> CaptureResult:
    """Store evidence durably and record it in the inbox (flushes, does not commit).

    ``content`` may be None for an item that could not be downloaded; it is then
    recorded with ``state='failed'`` so it stays visible for retry instead of
    being dropped.
    """
    if state not in INBOX_STATES:
        raise CaptureError(f"Unknown inbox state {state}.")
    if remote_id is None:
        if content is None:
            raise CaptureError("A capture without content needs a remote identity.")
        remote_id = hashlib.sha256(content).hexdigest()  # Local uploads: identity is the content itself.
        remote_version = remote_version or secrets.token_hex(8)  # A deliberate re-upload is a new arrival.
    existing = find_item(db, channel, account, remote_id, remote_version)
    if existing is not None:
        return CaptureResult(item=existing, created=False, blob_reused=True)
    blob = None
    reused = False
    if content is not None:
        if len(content) > source_size_limit(original_name):
            raise CaptureError(f"{original_name or 'Attachment'} is larger than {source_size_limit(original_name) // (1024 * 1024)} MB.")
        detected = sniff_media_type(content, media_type, original_name)
        digest, relative = _store_bytes(content, root)
        blob = db.scalar(select(SourceBlob).where(SourceBlob.sha256 == digest))
        reused = blob is not None
        if blob is None:
            blob = SourceBlob(sha256=digest, size_bytes=len(content), media_type=detected, storage_path=relative)
            db.add(blob)
            db.flush()
        media_type = detected
    item = SourceItem(channel=channel, account=account, remote_id=remote_id, remote_version=remote_version,
                      parent_id=parent_id, blob_id=blob.id if blob else None, original_name=original_name[:500],
                      media_type=media_type or "application/octet-stream", source_link=source_link,
                      metadata_json=json.dumps(metadata or {}, ensure_ascii=False, default=str),
                      state="failed" if content is None and state == "captured" else state,
                      state_reason=state_reason, session_id=session_id)
    db.add(item)
    db.flush()
    return CaptureResult(item=item, created=True, blob_reused=reused)


def read_blob(blob: SourceBlob, root: Path | None = None) -> bytes:
    path = (root or ROOT_DIR) / blob.storage_path
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != blob.sha256:
        raise CaptureError("Stored evidence failed its integrity check. Restore it from a recovery snapshot.")
    return content


def verify_all(db: Session, root: Path | None = None) -> list[str]:
    """Return descriptions of blobs whose file is missing or corrupt."""
    problems = []
    for blob in db.scalars(select(SourceBlob)).all():
        try:
            read_blob(blob, root)
        except FileNotFoundError:
            problems.append(f"Missing stored file for source blob {blob.id}")
        except CaptureError:
            problems.append(f"Checksum mismatch for source blob {blob.id}")
    return problems


def set_state(item: SourceItem, state: str, reason: str | None = None) -> None:
    if state not in INBOX_STATES:
        raise CaptureError(f"Unknown inbox state {state}.")
    item.state = state
    item.state_reason = reason


def attach_content(db: Session, item: SourceItem, content: bytes, media_type: str | None = None, root: Path | None = None) -> SourceItem:
    """Store bytes for an inbox item that was recorded without content (a failed download)."""
    if len(content) > source_size_limit(item.original_name):
        raise CaptureError(f"{item.original_name or 'Item'} is larger than {source_size_limit(item.original_name) // (1024 * 1024)} MB.")
    detected = sniff_media_type(content, media_type or item.media_type, item.original_name)
    digest, relative = _store_bytes(content, root)
    blob = db.scalar(select(SourceBlob).where(SourceBlob.sha256 == digest))
    if blob is None:
        blob = SourceBlob(sha256=digest, size_bytes=len(content), media_type=detected, storage_path=relative)
        db.add(blob)
        db.flush()
    item.blob_id, item.media_type = blob.id, detected
    item.attempts += 1
    set_state(item, "captured", "Captured on retry")
    db.flush()
    return item
