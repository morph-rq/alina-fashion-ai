"""Incremental capture from Google Drive and Gmail into the source inbox.

Checkpoint rule: every observed item is committed (stored, recognised as already
stored, or recorded as a failed/ignored inbox item, with a retry job where a
retry can help) before the checkpoint that covers it is saved. Network work
happens with no write transaction open; each item is committed on its own, so
syncing never holds the database lock during downloads. A crash leaves the
previous checkpoint, and replays are recognised by remote identity, so nothing
is skipped or duplicated.

Remote changes never delete or overwrite accounting facts: a changed Drive file
becomes a new evidence version; a removed or unshared file is noted on its
earlier captures, which remain.
"""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs, source_store
from app.google_api import (
    EXPORT_FORMATS, SUPPORTED_DOWNLOADS, DriveClient, GmailClient, GoogleError, GoogleSession,
)
from app.mail_capture import ATTACHMENT_TYPES, parse_message
from app.models import Connection, ConnectionSource, SourceItem, SyncRun

TRANSPORT = None  # Tests replace this with an httpx.MockTransport.


def now() -> datetime:
    return datetime.now(timezone.utc)


def _version(meta: dict) -> str:
    return str(meta.get("headRevisionId") or meta.get("md5Checksum") or meta.get("version") or meta.get("modifiedTime") or "")


def _session_for(connection: Connection) -> GoogleSession:
    return GoogleSession(connection.secret_ref, transport=TRANSPORT)


def _supported(meta: dict) -> bool:
    return meta.get("mimeType") in SUPPORTED_DOWNLOADS or meta.get("mimeType") in EXPORT_FORMATS


def _commit(db: Session) -> None:
    db.commit()
    jobs.heartbeat()


def _record(db: Session, **values) -> None:
    """Record an inbox item that could not be stored normally (failed or ignored)."""
    source_store.capture(db, content=None, **values)


def capture_drive_file(db: Session, connection: Connection, drive: DriveClient, meta: dict, run: SyncRun, origin: str) -> None:
    version = _version(meta)
    with db.no_autoflush:  # Nothing may be written (and lock the database) before the download below.
        known = source_store.find_item(db, "drive", connection.account_email, meta["id"], version)
    run.seen += 1
    if known:
        run.duplicates += 1
        return
    common = dict(channel="drive", account=connection.account_email, remote_id=meta["id"], remote_version=version,
                  original_name=meta.get("name", ""), source_link=meta.get("webViewLink"))
    metadata = {"drive_name": meta.get("name"), "mime_type": meta.get("mimeType"), "modified_time": meta.get("modifiedTime"),
                "md5": meta.get("md5Checksum"), "origin": origin}
    if not _supported(meta):
        _record(db, media_type=meta.get("mimeType"), metadata=metadata, state="ignored",
                state_reason=f"Unsupported file type {meta.get('mimeType')}", **common)
        _commit(db)
        return
    try:
        content, media_type, extra = drive.download(meta)  # No write transaction is open here.
        if extra.get("export_format"):
            common["original_name"] = common["original_name"] + EXPORT_FORMATS[meta["mimeType"]][1]
        source_store.capture(db, content=content, media_type=media_type, metadata=metadata | extra, **common)
        run.captured += 1
    except source_store.CaptureError as exc:  # For example larger than the storage limit: not retryable.
        db.rollback()
        _record(db, media_type=meta.get("mimeType"), metadata=metadata, state="failed", state_reason=str(exc), **common)
        run.failed += 1
    except GoogleError as exc:
        if exc.reconnect:
            raise
        db.rollback()
        _record(db, media_type=meta.get("mimeType"), metadata=metadata, state="failed", state_reason=str(exc), **common)
        jobs.enqueue(db, "drive.fetch", {"connection_id": connection.id, "file_id": meta["id"], "version": version},
                     dedupe_key=f"drive.fetch:{connection.account_email}:{meta['id']}:{version}", delay_seconds=60)
        run.failed += 1
    _commit(db)


def _note_removed(db: Session, connection: Connection, file_id: str, run: SyncRun) -> None:
    previous = db.scalars(select(SourceItem).where(SourceItem.channel == "drive", SourceItem.account == connection.account_email,
                                                   SourceItem.remote_id == file_id)).all()
    for item in previous:
        metadata = json.loads(item.metadata_json or "{}")
        metadata["remote_removed_at"] = now().isoformat()
        item.metadata_json = json.dumps(metadata)
    if previous:
        run.note = ((run.note or "") + f" File {file_id} was removed or unshared at the source; its stored evidence is kept.").strip()
    _commit(db)


def sync_drive(db: Session, source: ConnectionSource, drive: DriveClient, run: SyncRun) -> None:
    connection = source.connection
    if source.kind == "drive_file":
        try:
            meta = drive.get(source.remote_id)
        except GoogleError as exc:
            if exc.status == 404:
                source.last_error = "The file is no longer shared with this app or was deleted. Earlier captures are kept."
                _note_removed(db, connection, source.remote_id, run)
                return
            raise
        if meta.get("trashed"):
            _note_removed(db, connection, source.remote_id, run)
        else:
            capture_drive_file(db, connection, drive, meta, run, f"selected file {source.name}")
        return
    if not source.checkpoint:
        # Take the change token first, so nothing that changes during the listing is missed.
        token = drive.start_page_token()
        for meta in drive.list_folder(source.remote_id):
            capture_drive_file(db, connection, drive, meta, run, f"folder {source.name}")
        source.checkpoint, source.checkpoint_kind = token, "drive_changes"
        _commit(db)
        return
    token = source.checkpoint
    while token:
        page = drive.changes_page(token)
        for change in page.get("changes", []):
            meta = change.get("file") or {}
            if change.get("removed") or meta.get("trashed"):
                _note_removed(db, connection, change.get("fileId"), run)
            elif source.remote_id in (meta.get("parents") or []):
                capture_drive_file(db, connection, drive, meta, run, f"folder {source.name}")
        # Every change on this page is committed; the token may now move past it.
        token = page.get("nextPageToken")
        source.checkpoint = token or page.get("newStartPageToken") or source.checkpoint
        _commit(db)


def capture_gmail_message(db: Session, connection: Connection, gmail: GmailClient, message_id: str, run: SyncRun, query: str) -> bool:
    """Store one message and its attachments. Returns False only if fetching failed (retry queued)."""
    with db.no_autoflush:
        known = source_store.find_item(db, "gmail", connection.account_email, message_id, "")
    run.seen += 1
    if known:
        run.duplicates += 1
        return True
    common = dict(channel="gmail", account=connection.account_email, remote_id=message_id)
    try:
        body = gmail.raw_message(message_id)
        raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
    except GoogleError as exc:
        if exc.reconnect:
            raise
        _record(db, original_name=f"message {message_id}", media_type="message/rfc822", state="failed", state_reason=str(exc),
                metadata={"query": query}, **common)
        jobs.enqueue(db, "gmail.fetch", {"connection_id": connection.id, "message_id": message_id, "query": query},
                     dedupe_key=f"gmail.fetch:{connection.account_email}:{message_id}", delay_seconds=60)
        run.failed += 1
        _commit(db)
        return False
    try:
        parsed = parse_message(raw)
    except Exception as exc:  # noqa: BLE001 - a malformed message must not block the source
        parsed, parse_problem = None, f"Message could not be parsed ({type(exc).__name__})"
    else:
        parse_problem = None
    metadata = {"thread_id": body.get("threadId"), "label_ids": body.get("labelIds", []), "internal_date": body.get("internalDate"),
                "query": query, "size_bytes": len(raw)}
    if parsed is not None:
        metadata |= {"subject": parsed.subject, "sender": parsed.sender, "recipients": parsed.recipients, "date": parsed.date,
                     "message_id_header": parsed.message_id_header, "body_text": parsed.body_text,
                     "attachments": [attachment.filename for attachment in parsed.attachments]}
    name = ((parsed.subject if parsed else "") or f"message {message_id}")[:300]
    link = f"https://mail.google.com/mail/u/0/#all/{message_id}"
    try:
        message = source_store.capture(db, content=raw, original_name=name, media_type="message/rfc822", metadata=metadata,
                                       source_link=link, state="failed" if parse_problem else "captured",
                                       state_reason=parse_problem, **common).item
    except source_store.CaptureError as exc:  # Too large to store: visible, not retried endlessly.
        db.rollback()
        _record(db, original_name=name, media_type="message/rfc822", metadata=metadata, source_link=link, state="failed",
                state_reason=str(exc), **common)
        run.failed += 1
        _commit(db)
        return True
    for index, attachment in enumerate(parsed.attachments if parsed else []):
        child = dict(channel="gmail", account=connection.account_email, remote_id=f"{message_id}:{index}",
                     original_name=attachment.filename, media_type=attachment.media_type, parent_id=message.id,
                     metadata={"message_id": message_id, "content_id": attachment.content_id, "inline": attachment.inline})
        if attachment.problem:
            _record(db, state="failed", state_reason=attachment.problem, **child)
            run.note = ((run.note or "") + f" An attachment of message {message_id} could not be decoded.").strip()
            continue
        state, reason = "captured", None
        if attachment.media_type not in ATTACHMENT_TYPES:
            state, reason = "ignored", f"Unsupported attachment type {attachment.media_type}"
        try:
            source_store.capture(db, content=attachment.content, state=state, state_reason=reason, **child)
        except source_store.CaptureError as exc:
            _record(db, state="failed", state_reason=str(exc), **child)
    run.captured += 1
    _commit(db)
    return True


def _gmail_query(source: ConnectionSource) -> str:
    query = (source.query or "").strip()
    if source.start_date:
        query = f"{query} after:{source.start_date:%Y/%m/%d}".strip()
    return query


def sync_gmail(db: Session, source: ConnectionSource, gmail: GmailClient, run: SyncRun) -> None:
    connection = source.connection
    query = _gmail_query(source)
    if source.checkpoint_kind == "gmail_history" and source.checkpoint:
        try:
            candidates: list[str] = []
            page_token, latest = None, source.checkpoint
            while True:
                page = gmail.history_page(source.checkpoint, page_token)
                latest = page.get("historyId", latest)
                for record in page.get("history", []):
                    # New mail, and mail that gains a label later (for example label:alina-orders added by hand).
                    for entry in record.get("messagesAdded", []) + record.get("labelsAdded", []):
                        candidates.append(entry["message"]["id"])
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
            if candidates:
                # History is not filtered by the query: keep candidates that match it within the import boundary.
                matching = {message_id for message_id, _token in gmail.list_ids(query)}
                for message_id in dict.fromkeys(candidates):
                    if message_id in matching:
                        capture_gmail_message(db, connection, gmail, message_id, run, query)
            source.checkpoint = str(latest)
            _commit(db)
            return
        except GoogleError as exc:
            if exc.status != 404:
                raise
            # History is no longer available: a bounded, visible full resync; captured messages are recognised.
            run.note = "Gmail history expired; performed a full resync bounded by the start date. Already captured messages were skipped."
            source.checkpoint, source.checkpoint_kind = None, None
    history_id = gmail.profile()["historyId"]  # Taken before listing so later mail is caught by history.
    for message_id, _token in gmail.list_ids(query):
        capture_gmail_message(db, connection, gmail, message_id, run, query)
    source.checkpoint, source.checkpoint_kind = str(history_id), "gmail_history"
    _commit(db)


def _record_failure(db: Session, source: ConnectionSource, exc: GoogleError) -> None:
    source.last_error = str(exc)
    if exc.reconnect:
        source.connection.status = "needs_reconnect"
        source.connection.status_reason = str(exc)


def run_source_sync(db: Session, source_id: int) -> dict:
    source = db.get(ConnectionSource, source_id)
    if source is None or not source.active or source.connection.status != "connected":
        return {"skipped": True}
    run = SyncRun(source_id=source.id)
    db.add(run)
    _commit(db)
    session = _session_for(source.connection)
    try:
        if source.kind.startswith("drive_"):
            sync_drive(db, source, DriveClient(session), run)
        elif source.kind == "gmail_query":
            sync_gmail(db, source, GmailClient(session), run)
        else:
            return {"skipped": True}
    except GoogleError as exc:
        db.rollback()
        source, run = db.get(ConnectionSource, source_id), db.get(SyncRun, run.id)
        _record_failure(db, source, exc)
        run.status, run.finished_at, run.note = "failed", now(), ((run.note or "") + " " + str(exc)).strip()
        db.commit()
        if exc.reconnect:
            raise jobs.PermanentJobError(str(exc)) from None
        raise
    finally:
        session.close()
    run.status = "partial" if run.failed else "ok"
    run.finished_at = now()
    source.last_sync_at = now()
    source.last_success_at = now()
    source.connection.last_success_at = now()
    source.last_error = None if run.status == "ok" else f"{run.failed} item(s) could not be stored; see the source inbox."
    db.commit()
    return {"run": run.id, "seen": run.seen, "captured": run.captured, "duplicates": run.duplicates, "failed": run.failed}


@jobs.handler("sync.source")
def sync_job(db: Session, payload: dict) -> dict:
    return run_source_sync(db, payload["source_id"])


@jobs.handler("drive.fetch")
def drive_fetch_job(db: Session, payload: dict) -> dict:
    connection = db.get(Connection, payload["connection_id"])
    item = source_store.find_item(db, "drive", connection.account_email, payload["file_id"], payload["version"])
    session = _session_for(connection)
    try:
        drive = DriveClient(session)
        meta = drive.get(payload["file_id"])
        if _version(meta) != payload["version"]:
            if item is not None:
                source_store.set_state(item, "ignored", "Superseded by a newer version at the source.")
            return {"superseded": True}
        content, media_type, extra = drive.download(meta)
    finally:
        session.close()
    if item is None:
        source_store.capture(db, channel="drive", content=content, account=connection.account_email, remote_id=payload["file_id"],
                             remote_version=payload["version"], original_name=meta.get("name", ""), media_type=media_type,
                             metadata=extra, source_link=meta.get("webViewLink"))
    else:
        source_store.attach_content(db, item, content, media_type)
    return {"captured": True}


@jobs.handler("gmail.fetch")
def gmail_fetch_job(db: Session, payload: dict) -> dict:
    connection = db.get(Connection, payload["connection_id"])
    failed = source_store.find_item(db, "gmail", connection.account_email, payload["message_id"], "")
    if failed is not None and failed.blob_id is None:
        db.delete(failed)
        db.flush()
    source_id = db.scalar(select(ConnectionSource.id).where(ConnectionSource.connection_id == connection.id,
                                                            ConnectionSource.kind == "gmail_query").limit(1))
    run = SyncRun(source_id=source_id, note="Retry of one message")
    db.add(run)
    db.flush()
    session = _session_for(connection)
    try:
        fetched = capture_gmail_message(db, connection, GmailClient(session), payload["message_id"], run, payload.get("query", ""))
    finally:
        session.close()
    run.status, run.finished_at = ("ok" if fetched else "partial"), now()
    if not fetched:
        raise RuntimeError("Message still could not be fetched; retrying later.")
    return {"captured": True}


@jobs.periodic
def schedule_syncs(db: Session) -> None:
    """Queue syncs that are due. Polling suits a localhost app; nothing runs while the app is stopped."""
    for source in db.scalars(select(ConnectionSource).join(Connection).where(
            ConnectionSource.active.is_(True), Connection.status == "connected",
            ConnectionSource.kind.in_(["drive_file", "drive_folder", "gmail_query"]))).all():
        last = source.last_sync_at.replace(tzinfo=timezone.utc) if source.last_sync_at and source.last_sync_at.tzinfo is None else source.last_sync_at
        if last is None or now() - last >= timedelta(minutes=max(source.interval_minutes, 1)):
            jobs.enqueue(db, "sync.source", {"source_id": source.id}, dedupe_key=f"sync:{source.id}")
