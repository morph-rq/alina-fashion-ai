"""Incremental capture from Google Drive and Gmail into the source inbox.

Checkpoint rule: a checkpoint (Drive page token, Gmail history id) is saved in
the same transaction as the items observed up to it. Every observed item is
either stored, recognised as already stored, or recorded as a failed inbox item
with a retry job. A crash before commit leaves the old checkpoint, and replays
are recognised by remote identity, so nothing is skipped or duplicated.

Remote changes never delete or overwrite accounting facts: a changed Drive file
becomes a new evidence version; a removed or unshared file is noted on the
source, and its earlier captures remain.
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


def capture_drive_file(db: Session, connection: Connection, drive: DriveClient, meta: dict, run: SyncRun, origin: str) -> None:
    run.seen += 1
    version = _version(meta)
    if source_store.find_item(db, "drive", connection.account_email, meta["id"], version):
        run.duplicates += 1
        return
    metadata = {"drive_name": meta.get("name"), "mime_type": meta.get("mimeType"), "modified_time": meta.get("modifiedTime"),
                "md5": meta.get("md5Checksum"), "origin": origin}
    if not _supported(meta):
        source_store.capture(db, channel="drive", content=None, account=connection.account_email, remote_id=meta["id"],
                             remote_version=version, original_name=meta.get("name", ""), media_type=meta.get("mimeType"),
                             metadata=metadata, source_link=meta.get("webViewLink"), state="ignored",
                             state_reason=f"Unsupported file type {meta.get('mimeType')}")
        return
    try:
        content, media_type, extra = drive.download(meta)
        name = meta.get("name", "drive-file")
        if extra.get("export_format"):
            name += EXPORT_FORMATS[meta["mimeType"]][1]
        source_store.capture(db, channel="drive", content=content, account=connection.account_email, remote_id=meta["id"],
                             remote_version=version, original_name=name, media_type=media_type, metadata=metadata | extra,
                             source_link=meta.get("webViewLink"))
        run.captured += 1
    except (GoogleError, source_store.CaptureError) as exc:
        if isinstance(exc, GoogleError) and exc.reconnect:
            raise
        source_store.capture(db, channel="drive", content=None, account=connection.account_email, remote_id=meta["id"],
                             remote_version=version, original_name=meta.get("name", ""), media_type=meta.get("mimeType"),
                             metadata=metadata, source_link=meta.get("webViewLink"), state="failed", state_reason=str(exc))
        jobs.enqueue(db, "drive.fetch", {"connection_id": connection.id, "file_id": meta["id"], "version": version},
                     dedupe_key=f"drive.fetch:{connection.account_email}:{meta['id']}:{version}", delay_seconds=60)
        run.failed += 1


def _note_removed(db: Session, connection: Connection, file_id: str, run: SyncRun) -> None:
    previous = db.scalars(select(SourceItem).where(SourceItem.channel == "drive", SourceItem.account == connection.account_email,
                                                   SourceItem.remote_id == file_id)).all()
    for item in previous:
        metadata = json.loads(item.metadata_json or "{}")
        metadata["remote_removed_at"] = now().isoformat()
        item.metadata_json = json.dumps(metadata)
    if previous:
        run.note = ((run.note or "") + f" File {file_id} was removed or unshared at the source; its stored evidence is kept.").strip()


def sync_drive(db: Session, source: ConnectionSource, drive: DriveClient) -> SyncRun:
    connection = source.connection
    run = SyncRun(source_id=source.id)
    db.add(run)
    db.flush()
    if source.kind == "drive_file":
        try:
            meta = drive.get(source.remote_id)
        except GoogleError as exc:
            if exc.status == 404:
                source.last_error = "The file is no longer shared with this app or was deleted. Earlier captures are kept."
                _note_removed(db, connection, source.remote_id, run)
                run.status = "partial"
                return run
            raise
        if meta.get("trashed"):
            _note_removed(db, connection, source.remote_id, run)
        else:
            capture_drive_file(db, connection, drive, meta, run, f"selected file {source.name}")
    elif source.kind == "drive_folder":
        if not source.checkpoint:
            # Take the change token first, so nothing that changes during the listing is missed.
            token = drive.start_page_token()
            for meta in drive.list_folder(source.remote_id):
                capture_drive_file(db, connection, drive, meta, run, f"folder {source.name}")
            source.checkpoint, source.checkpoint_kind = token, "drive_changes"
        else:
            token = source.checkpoint
            while token:
                page = drive.changes_page(token)
                for change in page.get("changes", []):
                    meta = change.get("file") or {}
                    if change.get("removed") or meta.get("trashed"):
                        _note_removed(db, connection, change.get("fileId"), run)
                    elif source.remote_id in (meta.get("parents") or []):
                        capture_drive_file(db, connection, drive, meta, run, f"folder {source.name}")
                if page.get("nextPageToken"):
                    token = page["nextPageToken"]
                    source.checkpoint = token  # Resumable mid-listing checkpoint, saved with this page's captures.
                    db.commit()
                else:
                    source.checkpoint = page.get("newStartPageToken") or token
                    token = None
    run.status = "partial" if run.failed else "ok"
    return run


def capture_gmail_message(db: Session, connection: Connection, gmail: GmailClient, message_id: str, run: SyncRun, query: str) -> None:
    run.seen += 1
    if source_store.find_item(db, "gmail", connection.account_email, message_id, ""):
        run.duplicates += 1
        return
    try:
        body = gmail.raw_message(message_id)
        raw = base64.urlsafe_b64decode(body["raw"] + "=" * (-len(body["raw"]) % 4))
    except GoogleError as exc:
        if exc.reconnect:
            raise
        source_store.capture(db, channel="gmail", content=None, account=connection.account_email, remote_id=message_id,
                             original_name=f"message {message_id}", media_type="message/rfc822", state="failed", state_reason=str(exc),
                             metadata={"query": query})
        jobs.enqueue(db, "gmail.fetch", {"connection_id": connection.id, "message_id": message_id, "query": query},
                     dedupe_key=f"gmail.fetch:{connection.account_email}:{message_id}", delay_seconds=60)
        run.failed += 1
        return
    parsed = parse_message(raw)
    metadata = {"subject": parsed.subject, "sender": parsed.sender, "recipients": parsed.recipients, "date": parsed.date,
                "message_id_header": parsed.message_id_header, "thread_id": body.get("threadId"), "label_ids": body.get("labelIds", []),
                "internal_date": body.get("internalDate"), "query": query, "body_text": parsed.body_text,
                "attachments": [attachment.filename for attachment in parsed.attachments]}
    message = source_store.capture(db, channel="gmail", content=raw, account=connection.account_email, remote_id=message_id,
                                   original_name=(parsed.subject or f"message {message_id}")[:300], media_type="message/rfc822",
                                   metadata=metadata, source_link=f"https://mail.google.com/mail/u/0/#all/{message_id}").item
    for index, attachment in enumerate(parsed.attachments):
        remote_id = f"{message_id}:{index}"
        common = dict(channel="gmail", account=connection.account_email, remote_id=remote_id, original_name=attachment.filename,
                      media_type=attachment.media_type, parent_id=message.id,
                      metadata={"message_id": message_id, "content_id": attachment.content_id, "inline": attachment.inline})
        if attachment.problem:
            source_store.capture(db, content=None, state="failed", state_reason=attachment.problem, **common)
            run.failed += 1
        elif attachment.media_type not in ATTACHMENT_TYPES:
            source_store.capture(db, content=attachment.content, state="ignored", state_reason=f"Unsupported attachment type {attachment.media_type}", **common)
        else:
            source_store.capture(db, content=attachment.content, **common)
    run.captured += 1


def _gmail_query(source: ConnectionSource) -> str:
    query = (source.query or "").strip()
    if source.start_date:
        query = f"{query} after:{source.start_date:%Y/%m/%d}".strip()
    return query


def sync_gmail(db: Session, source: ConnectionSource, gmail: GmailClient) -> SyncRun:
    connection = source.connection
    run = SyncRun(source_id=source.id)
    db.add(run)
    db.flush()
    query = _gmail_query(source)
    if source.checkpoint_kind == "gmail_history" and source.checkpoint:
        try:
            candidate_ids: list[str] = []
            page_token, latest = None, source.checkpoint
            while True:
                page = gmail.history_page(source.checkpoint, page_token)
                latest = page.get("historyId", latest)
                for record in page.get("history", []):
                    for added in record.get("messagesAdded", []):
                        candidate_ids.append(added["message"]["id"])
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
            if candidate_ids:
                # History is not filtered by query; keep only messages that match the operator's query.
                since = (now() - timedelta(days=30)).strftime("%Y/%m/%d")
                matching = {message_id for message_id, _token in gmail.list_ids(f"{query} after:{since}".strip())}
                for message_id in dict.fromkeys(candidate_ids):
                    if message_id in matching:
                        capture_gmail_message(db, connection, gmail, message_id, run, query)
            source.checkpoint = str(latest)
        except GoogleError as exc:
            if exc.status != 404:
                raise
            # History is no longer available: a bounded, visible full resync; captured messages are recognised.
            run.note = "Gmail history expired; performed a full resync bounded by the start date. Already captured messages were skipped."
            source.checkpoint, source.checkpoint_kind = None, None
    if source.checkpoint_kind != "gmail_history":
        history_id = gmail.profile()["historyId"]  # Taken before listing so later mail is caught by history.
        for message_id, _token in gmail.list_ids(query):
            capture_gmail_message(db, connection, gmail, message_id, run, query)
        source.checkpoint, source.checkpoint_kind = str(history_id), "gmail_history"
    run.status = "partial" if run.failed else "ok"
    return run


def _record_failure(db: Session, source: ConnectionSource, exc: GoogleError) -> None:
    source.last_error = str(exc)
    if exc.reconnect:
        source.connection.status = "needs_reconnect"
        source.connection.status_reason = str(exc)


def run_source_sync(db: Session, source_id: int) -> dict:
    source = db.get(ConnectionSource, source_id)
    if source is None or not source.active or source.connection.status != "connected":
        return {"skipped": True}
    session = _session_for(source.connection)
    try:
        if source.kind.startswith("drive_"):
            run = sync_drive(db, source, DriveClient(session))
        elif source.kind == "gmail_query":
            run = sync_gmail(db, source, GmailClient(session))
        else:
            return {"skipped": True}
    except GoogleError as exc:
        db.rollback()
        source = db.get(ConnectionSource, source_id)
        _record_failure(db, source, exc)
        db.add(SyncRun(source_id=source.id, status="failed", finished_at=now(), note=str(exc)))
        db.commit()
        if exc.reconnect:
            raise jobs.PermanentJobError(str(exc)) from None
        raise
    finally:
        session.close()
    run.finished_at = now()
    source.last_sync_at = now()
    if run.status in {"ok", "partial"}:
        source.last_success_at = now()
        source.connection.last_success_at = now()
        source.last_error = None if run.status == "ok" else f"{run.failed} item(s) failed and are queued for retry."
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
    session = _session_for(connection)
    try:
        run = SyncRun(source_id=db.scalar(select(ConnectionSource.id).where(ConnectionSource.connection_id == connection.id,
                                                                            ConnectionSource.kind == "gmail_query").limit(1)))
        db.add(run)
        db.flush()
        capture_gmail_message(db, connection, GmailClient(session), payload["message_id"], run, payload.get("query", ""))
        run.status, run.finished_at = ("ok" if not run.failed else "partial"), now()
    finally:
        session.close()
    if run.failed:
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
