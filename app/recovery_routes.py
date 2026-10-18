"""Recovery page: verified snapshots, the recovery code, off-site status and restore exercises."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import secrets
import shutil

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from urllib.parse import urlencode
from sqlalchemy.orm import Session

from app import outbox, recovery, recovery_crypto, secret_store
from app.connections_routes import require_admin
from app.database import get_db
from app.models import OutboxItem


def _snapshots(root: Path) -> list[Path]:
    return sorted((root / "backups" / "recovery").glob("snapshot-*.zip"), key=lambda path: path.name, reverse=True)


def restore_exercise(key: bytes | None, root: Path, archive: Path | None = None, keep: bool = False) -> dict:
    """Decrypt (when encrypted) and restore the newest archive into a new folder, then check it."""
    encrypted = sorted((root / "backups" / "recovery" / "encrypted").glob("snapshot-*.afra"), key=lambda p: p.name, reverse=True)
    source = archive or (encrypted[0] if encrypted else (_snapshots(root)[0] if _snapshots(root) else None))
    if source is None:
        raise ValueError("No recovery snapshot exists yet. Create a backup first.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / "backups" / "restore-exercises" / f"{stamp}-{secrets.token_hex(3)}"
    work = target.with_name(target.name + "-work")
    work.mkdir(parents=True)
    try:
        snapshot = source
        if source.suffix == ".afra":
            if key is None:
                raise ValueError("The archive is encrypted; set up or enter the recovery code first.")
            snapshot = work / "snapshot.zip"
            recovery_crypto.decrypt_file(source, snapshot, key)
        report = recovery.restore_snapshot(snapshot, target)
        report.update(archive=source.name, encrypted=source.suffix == ".afra", checked_at=datetime.now(timezone.utc).isoformat())
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if not keep:
            shutil.rmtree(target, ignore_errors=True)  # The exercise proves recoverability; it should not keep a second data copy.
    report["kept_folder"] = str(target) if keep else None
    return report


def _back(message: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode({key: value for key, value in (("message", message), ("error", error)) if value})
    return RedirectResponse("/recovery" + (f"?{query}" if query else ""), 303)


def install_recovery(app, templates, context):
    def page(request, db, **extra):
        status = outbox.recovery_status(db)
        root = recovery.ROOT_DIR
        legacy = sorted((root / "backups" / "verified").glob("fashion-*.zip"), reverse=True)[:10]
        uploads = db.query(OutboxItem).filter(OutboxItem.kind == "recovery").order_by(OutboxItem.id.desc()).limit(20).all()
        return templates.TemplateResponse(request, "recovery.html", context(
            request, status=status, snapshots=_snapshots(root)[:30], legacy=legacy, uploads=uploads,
            exercise=secret_store.load(outbox.STATE_REF).get("last_exercise"), **extra))

    @app.get("/recovery")
    def recovery_page(request: Request, db: Session = Depends(get_db)):
        return page(request, db, message=request.query_params.get("message"), error=request.query_params.get("error"))

    @app.post("/recovery/backup", dependencies=[Depends(require_admin)])
    def backup_now(request: Request, db: Session = Depends(get_db)):
        try:
            result = outbox.make_backup(db, "Manual backup", force=True)
            db.commit()
        except Exception as exc:  # noqa: BLE001 - shown to the operator; records are untouched
            db.rollback()
            return _back(error=f"Backup failed ({type(exc).__name__}): records are unchanged; try again.")
        note = "Verified snapshot created" + (", encrypted copy verified" if result.get("encrypted") else "") + \
               (", off-site upload queued" if result.get("queued_upload") else "") + "."
        return _back(message=note)

    @app.post("/recovery/key", dependencies=[Depends(require_admin)])
    def create_key(request: Request, db: Session = Depends(get_db)):
        if outbox.recovery_key() is not None:
            return _back(error="A recovery code already exists. Replacing it is a separate, deliberate step (see the guide).")
        key = outbox.recovery_key(create=True)
        # The code is shown once, in this response only; it is never put in a URL or log.
        return page(request, db, new_code=recovery_crypto.format_code(key))

    @app.post("/recovery/key/acknowledge", dependencies=[Depends(require_admin)])
    def acknowledge(confirmation: str = Form("")):
        stored = secret_store.load(recovery_crypto.KEY_REF)
        if not stored.get("key"):
            raise HTTPException(404, "No recovery code exists.")
        if confirmation.strip().upper() != "I HAVE STORED IT":
            return _back(error="Type I HAVE STORED IT after writing the code down somewhere other than this computer and your Google account.")
        stored["acknowledged"] = True
        secret_store.save(recovery_crypto.KEY_REF, stored)
        return _back(message="Recovery code confirmed. Encrypted off-site backups are enabled.")

    @app.post("/recovery/exercise", dependencies=[Depends(require_admin)])
    def exercise(code: str = Form(""), db: Session = Depends(get_db)):
        try:
            key = recovery_crypto.parse_code(code) if code.strip() else outbox.recovery_key()
            report = restore_exercise(key, recovery.ROOT_DIR)
        except (ValueError, recovery_crypto.RecoveryKeyError) as exc:
            return _back(error=f"Restore exercise failed: {exc}")
        state = secret_store.load(outbox.STATE_REF)
        state["last_exercise"] = {key_name: report[key_name] for key_name in ("archive", "encrypted", "checked_at", "files")} | {
            "rows": sum(report["table_counts"].values()), "used_entered_code": bool(code.strip())}
        secret_store.save(outbox.STATE_REF, state)
        return _back(message="Restore exercise passed: the archive restored into a new folder and checked out.")

    @app.get("/recovery/download/{filename}", dependencies=[Depends(require_admin)])
    def download(filename: str):
        root = recovery.ROOT_DIR / "backups" / "recovery"
        if Path(filename).name != filename or not filename.startswith("snapshot-") or not filename.endswith((".zip", ".afra")):
            raise HTTPException(404, "Backup not found.")
        path = root / filename if filename.endswith(".zip") else root / "encrypted" / filename
        if not path.is_file():
            raise HTTPException(404, "Backup not found.")
        if filename.endswith(".zip"):
            recovery.verify_snapshot(path)
        return FileResponse(path, filename=filename, media_type="application/octet-stream")
