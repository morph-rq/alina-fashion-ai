"""Outgoing files: versioned exports and encrypted recovery archives.

Saving records, generating a file and uploading it are separate outcomes. A
file is queued locally first (usable offline); the upload job checks the
destination for the same content hash before uploading (so an uncertain earlier
upload is not duplicated) and counts the upload only when Drive's MD5 matches.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import jobs, recovery, recovery_crypto, secret_store
from app.config import ROOT_DIR, settings
from app.google_api import DriveClient, GoogleError, GoogleSession
from app.models import AuditLog, Connection, ConnectionSource, OutboxItem, Season, SourceItem

OUTBOX_DIR = ROOT_DIR / "data" / "outbox"
ENCRYPTED_DIR = ROOT_DIR / "backups" / "recovery" / "encrypted"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STATE_REF = "recovery-state"


def now() -> datetime:
    return datetime.now(timezone.utc)


def _destination(db: Session, connection: Connection | None, kind: str) -> ConnectionSource | None:
    query = select(ConnectionSource).join(Connection).where(ConnectionSource.kind == kind, ConnectionSource.active.is_(True),
                                                            Connection.status != "disconnected")
    if connection is not None:
        query = query.where(ConnectionSource.connection_id == connection.id)
    return db.scalar(query.order_by(ConnectionSource.id).limit(1))


def _queue_file(db: Session, destination: ConnectionSource, kind: str, path: Path) -> OutboxItem | None:
    digest = recovery.file_sha256(path)
    existing = db.scalar(select(OutboxItem).where(OutboxItem.destination_id == destination.id, OutboxItem.content_sha256 == digest))
    if existing is not None:
        return existing
    item = OutboxItem(destination_id=destination.id, kind=kind, filename=path.name, local_path=path.relative_to(ROOT_DIR).as_posix()
                      if path.is_relative_to(ROOT_DIR) else str(path), content_sha256=digest, size_bytes=path.stat().st_size)
    db.add(item)
    db.flush()
    jobs.enqueue(db, "outbox.upload", {"outbox_id": item.id}, dedupe_key=f"outbox:{item.id}")
    return item


def queue_exports(db: Session, connection: Connection) -> int:
    from app.excel_export import build_business_workbook
    destination = _destination(db, connection, "drive_outputs")
    if destination is None:
        raise ValueError("Add an exports destination for this account first.")
    folder = OUTBOX_DIR / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    count = 0
    for season in db.scalars(select(Season).order_by(Season.id)).all():
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", season.name)
        path = folder / f"alina-{safe}-workbook-{stamp}.xlsx"
        path.write_bytes(build_business_workbook(db, season_id=season.id))
        if _queue_file(db, destination, "export", path):
            count += 1
    return count


def queue_named_export(db: Session, filename: str, content: bytes) -> OutboxItem:
    """Queue one generated workbook (e.g. an upload check) for the first active exports folder."""
    destination = _destination(db, None, "drive_outputs")
    if destination is None:
        raise ValueError("Connect Google Drive and add an exports folder in Settings › Connections first.")
    folder = OUTBOX_DIR / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    path = folder / f"{now():%Y%m%dT%H%M%SZ}-{safe}"
    path.write_bytes(content)
    return _queue_file(db, destination, "export", path)


def _resolve(item: OutboxItem) -> Path:
    path = Path(item.local_path)
    return path if path.is_absolute() else ROOT_DIR / path


def _fail(db: Session, item_id: int, state: str, message: str, *, reconnect: bool = False) -> None:
    """Record an upload failure durably (the job's own transaction is rolled back on error)."""
    db.rollback()
    item = db.get(OutboxItem, item_id)
    item.state, item.last_error = state, message
    if reconnect:
        connection = db.get(ConnectionSource, item.destination_id).connection
        connection.status, connection.status_reason = "needs_reconnect", message
    db.commit()


@jobs.handler("outbox.upload")
def upload_job(db: Session, payload: dict) -> dict:
    item = db.get(OutboxItem, payload["outbox_id"])
    if item is None or item.state == "verified":
        return {"skipped": True}
    item_id = item.id
    destination = db.get(ConnectionSource, item.destination_id)
    connection = destination.connection
    if connection.status != "connected":
        _fail(db, item_id, "queued", f"Waiting: connection is {connection.status.replace('_', ' ')}.")
        raise RuntimeError("Destination connection is not available.")
    path = _resolve(item)
    if not path.is_file() or recovery.file_sha256(path) != item.content_sha256:
        _fail(db, item_id, "failed", "The local file is missing or changed; regenerate it.")
        raise jobs.PermanentJobError("Local outbox file missing or changed.")
    from app import sync
    session = GoogleSession(connection.secret_ref, transport=sync.TRANSPORT)
    item.attempts += 1
    item.state = "uploading"
    db.commit()
    try:
        drive = DriveClient(session)
        folder = drive.ensure_folder(destination.name, destination.remote_id)
        destination.remote_id = folder
        db.commit()  # Keep the folder even if the upload below fails, so a retry does not create another.
        remote = drive.find_by_hash(folder, item.content_sha256)
        if remote is None:
            remote = drive.upload(folder, item.filename, path, XLSX if item.kind == "export" else "application/octet-stream", item.content_sha256)
        elif remote.get("md5Checksum") != hashlib.md5(path.read_bytes()).hexdigest():
            raise GoogleError("A remote file with this hash exists but its checksum differs; not counted.")
    except GoogleError as exc:
        _fail(db, item_id, "queued" if exc.retryable else "failed", str(exc), reconnect=exc.reconnect)
        if exc.reconnect:
            raise jobs.PermanentJobError(str(exc)) from None
        raise
    finally:
        session.close()
    item = db.get(OutboxItem, item_id)
    item.state, item.remote_id, item.verified_at, item.last_error = "verified", remote["id"], now(), None
    if item.kind == "recovery":
        state = secret_store.load(STATE_REF)
        state.update(last_remote_verified_at=now().isoformat(), last_remote_file=item.filename)
        secret_store.save(STATE_REF, state)
    return {"remote_id": remote["id"]}


# --- Recovery ------------------------------------------------------------------------

def recovery_key(create: bool = False) -> bytes | None:
    stored = secret_store.load(recovery_crypto.KEY_REF)
    if stored.get("key"):
        return bytes.fromhex(stored["key"])
    if not create:
        return None
    key = recovery_crypto.new_recovery_key()
    secret_store.save(recovery_crypto.KEY_REF, {"key": key.hex(), "created_at": now().isoformat(), "acknowledged": False})
    return key


def data_fingerprint(db: Session) -> str:
    """Hash of every business row and of managed files (used to skip identical backups).

    Content is hashed rather than counted, so in-place edits (draft fields, corrections without an audit
    row) are noticed. The database is small enough for this to be cheap.
    """
    from app.database import Base
    digest = hashlib.sha256()
    for table in Base.metadata.sorted_tables:
        if table.name in {"jobs", "worker_leases", "sync_runs", "outbox_items"}:
            continue
        digest.update(table.name.encode())
        for row in db.execute(select(table).order_by(*table.primary_key.columns)).all():
            digest.update(repr(tuple(row)).encode())
    parts = [digest.hexdigest()]
    for folder in recovery.INCLUDED_FOLDERS:
        base = recovery.ROOT_DIR / folder
        files = [path for path in base.rglob("*") if path.is_file()] if base.is_dir() else []
        parts.append([len(files), max((path.stat().st_mtime_ns for path in files), default=0)])
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()


def make_backup(db: Session, reason: str, *, force: bool = False) -> dict:
    """Verified local snapshot; an encrypted, verified copy for off-site upload when a key exists."""
    state = secret_store.load(STATE_REF)
    fingerprint = data_fingerprint(db)
    if not force and state.get("last_fingerprint") == fingerprint and state.get("last_local_file"):
        return {"skipped": "no changes since the last verified snapshot"}
    db.commit()
    snapshot = recovery.create_snapshot(db, reason=reason)
    state.update(last_local_at=now().isoformat(), last_local_file=snapshot.path.name, last_local_sha256=snapshot.sha256,
                 last_fingerprint=fingerprint, last_error=None)
    result = {"snapshot": snapshot.path.name}
    key = recovery_key()
    if key is not None:
        ENCRYPTED_DIR.mkdir(parents=True, exist_ok=True)
        encrypted = ENCRYPTED_DIR / (snapshot.path.stem + ".afra")
        recovery_crypto.encrypt_file(snapshot.path, encrypted, key, metadata={"snapshot": snapshot.path.name, "reason": reason})
        # Verify the encrypted copy decrypts to the exact snapshot before anyone relies on it.
        check = encrypted.with_suffix(".verify")
        try:
            recovery_crypto.decrypt_file(encrypted, check, key)
            if recovery.file_sha256(check) != snapshot.sha256:
                raise ValueError("Encrypted archive did not decrypt to the snapshot.")
        finally:
            check.unlink(missing_ok=True)
        state.update(last_encrypted_at=now().isoformat(), last_encrypted_file=encrypted.name)
        result["encrypted"] = encrypted.name
        destination = _destination(db, None, "drive_recovery")
        acknowledged = secret_store.load(recovery_crypto.KEY_REF).get("acknowledged")
        if destination is not None and not acknowledged:
            result["upload_waiting"] = "Confirm that the recovery code is stored before archives leave this computer."
        elif destination is not None:
            _queue_file(db, destination, "recovery", encrypted)
            result["queued_upload"] = True
    secret_store.save(STATE_REF, state)
    prune_local()
    return result


def prune_local(keep: int | None = None) -> list[str]:
    """Keep the newest verified snapshots; never removes the only copy."""
    keep = max(keep or settings.recovery_keep_local, 2)
    removed = []
    for folder, pattern in ((ROOT_DIR / "backups" / "recovery", "snapshot-*.zip"), (ENCRYPTED_DIR, "snapshot-*.afra")):
        files = sorted(folder.glob(pattern), key=lambda path: path.name, reverse=True)
        for path in files[keep:]:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


def recovery_status(db: Session) -> dict:
    state = secret_store.load(STATE_REF)
    key_info = secret_store.load(recovery_crypto.KEY_REF)
    pending = db.scalars(select(OutboxItem).where(OutboxItem.kind == "recovery", OutboxItem.state != "verified")).all()
    unprotected = None
    if state.get("last_fingerprint") and state["last_fingerprint"] != data_fingerprint(db):
        unprotected = "Changes since the last verified snapshot are not yet in any backup."
    return {**state, "key_configured": bool(key_info.get("key")), "key_acknowledged": bool(key_info.get("acknowledged")),
            "key_id": recovery_crypto.key_id(bytes.fromhex(key_info["key"])) if key_info.get("key") else None,
            "pending_uploads": len(pending), "pending_errors": [item.last_error for item in pending if item.last_error][:3],
            "unprotected": unprotected, "remote_destination": bool(_destination(db, None, "drive_recovery"))}


@jobs.handler("recovery.backup")
def backup_job(db: Session, payload: dict) -> dict:
    return make_backup(db, payload.get("reason", "Scheduled backup"), force=bool(payload.get("force")))


@jobs.periodic
def schedule_backups(db: Session) -> None:
    state = secret_store.load(STATE_REF)
    last = datetime.fromisoformat(state["last_local_at"]) if state.get("last_local_at") else None
    if last is None or now() - last >= timedelta(hours=max(settings.recovery_interval_hours, 1)):
        jobs.enqueue(db, "recovery.backup", {"reason": "Scheduled backup"}, dedupe_key="recovery:scheduled")
