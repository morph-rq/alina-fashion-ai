"""Consistent recovery snapshots of the business database and its evidence files.

A snapshot is a ZIP containing:

* ``data/fashion_orders.db`` — an online SQLite backup (never a copy of the live
  file), checked with ``PRAGMA integrity_check``;
* managed evidence and output folders (source attachments, images, imports,
  library, Excel exchange baselines, reports);
* ``manifest.json`` — SHA-256 and size of every file, table row counts, applied
  data migrations, creation time and the reason for the snapshot.

Secrets are excluded: ``data/private`` (OAuth tokens, protected keys) and
temporary staging folders never enter a snapshot. Business records can therefore
be restored on a replacement computer, while connections are re-authorised there.

Encryption for off-site copies is in ``app.recovery_crypto``; restore into a new
folder is ``restore_snapshot``.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import zipfile

from sqlalchemy.orm import Session

from app.config import ROOT_DIR

SNAPSHOT_VERSION = 1
DB_ARCNAME = "data/fashion_orders.db"
INCLUDED_FOLDERS = ("data/images", "data/imports", "data/library", "data/intake-sources", "data/sources",
                    "data/excel-exchange", "data/reports", "data/threema", "output", "outputs")
EXCLUDED_PREFIXES = ("data/private/", "data/staged-images/", "data/tmp/")


@dataclass(frozen=True)
class SnapshotInfo:
    path: Path
    sha256: str
    size: int
    manifest: dict


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_database(session: Session, target: Path) -> None:
    connection = session.connection()
    if connection.dialect.name != "sqlite":
        raise ValueError("Recovery snapshots currently require SQLite.")
    driver = connection.connection.driver_connection
    with closing(sqlite3.connect(target)) as output:
        driver.backup(output)
        if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed; the snapshot was not created.")


def _database_facts(db_path: Path) -> dict:
    with closing(sqlite3.connect(db_path)) as connection:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {table: connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
        migrations = []
        if "schema_migrations" in tables:
            migrations = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
    return {"table_counts": counts, "data_migrations": migrations}


def _managed_files(root: Path):
    resolved_root = root.resolve()
    for folder in INCLUDED_FOLDERS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(resolved_root):
                continue
            relative = path.relative_to(root).as_posix()
            if relative.startswith(EXCLUDED_PREFIXES) or path.suffix in {".db", ".db-wal", ".db-shm", ".partial"}:
                continue
            yield relative, path


def create_snapshot(session: Session, *, reason: str, root: Path | None = None, destination: Path | None = None) -> SnapshotInfo:
    """Write a verified snapshot ZIP; the session's pending changes must be committed first."""
    root = root or ROOT_DIR
    destination = destination or root / "backups" / "recovery"
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = destination / f"snapshot-{stamp}-{secrets.token_hex(3)}.zip"
    pending = final.with_suffix(".partial")
    try:
        with tempfile.TemporaryDirectory(prefix="snapshot-", dir=destination) as scratch:
            db_copy = Path(scratch) / "fashion_orders.db"
            _backup_database(session, db_copy)
            files = {DB_ARCNAME: {"sha256": file_sha256(db_copy), "size": db_copy.stat().st_size}}
            with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(db_copy, DB_ARCNAME)
                for relative, path in _managed_files(root):
                    payload = path.read_bytes()
                    archive.writestr(relative, payload)
                    files[relative] = {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
                manifest = {
                    "format": "alina-fashion-snapshot", "version": SNAPSHOT_VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(), "reason": reason,
                    "files": files, **_database_facts(db_copy),
                    "excludes": "OAuth tokens, protected keys (data/private), staging uploads and application source code.",
                }
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        verify_snapshot(pending)
        os.replace(pending, final)
    finally:
        pending.unlink(missing_ok=True)
    return SnapshotInfo(path=final, sha256=file_sha256(final), size=final.stat().st_size, manifest=manifest)


def _safe_member(name: str) -> bool:
    parts = Path(name).parts
    return not (name.startswith(("/", "\\")) or ".." in parts or ":" in name)


def verify_snapshot(path: Path) -> dict:
    """Check every listed file's hash and that nothing unlisted is present."""
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("Snapshot archive CRC check failed.")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "alina-fashion-snapshot":
            raise ValueError("Not an Alina Fashion snapshot.")
        names = set(archive.namelist())
        if names != set(manifest["files"]) | {"manifest.json"}:
            raise ValueError("Snapshot contains unlisted or missing files.")
        for name, facts in manifest["files"].items():
            if not _safe_member(name):
                raise ValueError("Unsafe file path in snapshot.")
            if hashlib.sha256(archive.read(name)).hexdigest() != facts["sha256"]:
                raise ValueError(f"Checksum mismatch for {name}.")
        return manifest


def restore_snapshot(path: Path, target: Path) -> dict:
    """Restore a verified snapshot into a NEW, empty folder and check the database there."""
    target = Path(target)
    if target.exists() and any(target.iterdir()):
        raise ValueError("Restore only into a new or empty folder; existing data is never overwritten.")
    manifest = verify_snapshot(path)
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for name in manifest["files"]:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(name))
    db_path = target / DB_ARCNAME
    with closing(sqlite3.connect(db_path)) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Restored database failed its integrity check.")
    facts = _database_facts(db_path)
    if facts["table_counts"] != manifest["table_counts"]:
        raise ValueError("Restored row counts differ from the snapshot manifest.")
    return {"restored_to": str(target), "files": len(manifest["files"]), **facts}
