"""Connections page: Google account, selected sources/destinations, sync and outbox status."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import secrets
import time

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import google_api, jobs, secret_store
from app.config import ROOT_DIR, settings
from app.database import get_db
from app.models import Connection, ConnectionSource, OutboxItem, SourceItem, SyncRun

PENDING_REF = "oauth-pending"
PICKER_ALLOWED = set(google_api.IDENTITY_SCOPES) | {google_api.SCOPE_DRIVE_FILE}
CAPABILITIES = {
    "drive_files": [google_api.SCOPE_DRIVE_FILE],
    "drive_folder": [google_api.SCOPE_DRIVE_FILE, google_api.SCOPE_DRIVE_READONLY],
    "gmail": [google_api.SCOPE_GMAIL_READONLY],
}
DESTINATION_NAMES = {"drive_outputs": "Alina Fashion exports", "drive_recovery": "Alina Fashion recovery archives"}


def require_admin(request: Request) -> None:
    """Connection management needs the app login, or a loopback-only installation without one."""
    if settings.app_username.strip() and settings.app_password:
        return  # The HTTP Basic middleware has already authenticated this request.
    host = request.client.host if request.client else ""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host in {"localhost", "testclient"}
    if not loopback:
        raise HTTPException(403, "Connection settings are available only from this computer or after signing in.")


def _now():
    return datetime.now(timezone.utc)


def install_connections(app, templates, context):
    def page(request, db, **extra):
        connections = db.scalars(select(Connection).order_by(Connection.id)).all()
        runs = {source.id: db.scalars(select(SyncRun).where(SyncRun.source_id == source.id).order_by(SyncRun.id.desc()).limit(5)).all()
                for connection in connections for source in connection.sources}
        outbox = db.scalars(select(OutboxItem).order_by(OutboxItem.id.desc()).limit(30)).all()
        pending_outbox = db.scalar(select(func.count()).select_from(OutboxItem).where(OutboxItem.state.in_(["queued", "uploading", "failed"]))) or 0
        client = google_api.client_config()
        return templates.TemplateResponse(request, "connections.html", context(
            request, connections=connections, runs=runs, outbox=outbox, pending_outbox=pending_outbox,
            client_configured=bool(client.get("client_id")), redirect_uri=google_api.redirect_uri(),
            explanations=google_api.SCOPE_EXPLANATIONS, scopes=google_api, picker_ready=bool(settings.google_api_key and settings.google_app_id),
            api_key=settings.google_api_key, app_id=settings.google_app_id, queue=jobs.queue_summary(db),
            inbox_counts=dict(db.execute(select(SourceItem.channel, func.count()).group_by(SourceItem.channel)).all()), **extra))

    @app.get("/connections")
    def connections_page(request: Request, db: Session = Depends(get_db)):
        return page(request, db, message=request.query_params.get("message"), error=request.query_params.get("error"))

    def back(message=None, error=None):
        from urllib.parse import urlencode
        query = urlencode({key: value for key, value in (("message", message), ("error", error)) if value})
        return RedirectResponse("/connections" + (f"?{query}" if query else ""), 303)

    @app.post("/connections/google/client", dependencies=[Depends(require_admin)])
    async def upload_client(file: UploadFile = File(...)):
        payload = await file.read(65537)
        if len(payload) > 65536:
            return back(error="That file is too large to be an OAuth client file.")
        try:
            google_api.save_client_json(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            return back(error=str(exc))
        return back(message="Google OAuth client saved (protected on this computer).")

    @app.post("/connections/google/start", dependencies=[Depends(require_admin)])
    async def start(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        chosen = [name for name in CAPABILITIES if form.get(name)]
        reconnect_id = int(form["connection_id"]) if str(form.get("connection_id", "")).isdigit() else None
        existing = db.get(Connection, reconnect_id) if reconnect_id else None
        if not chosen and existing is None:
            return back(error="Choose at least one capability to connect.")
        scopes = sorted({scope for name in chosen for scope in CAPABILITIES[name]} |
                        set((existing.granted_scopes or "").split() if existing else []) - set(google_api.IDENTITY_SCOPES))
        if not form.get("understood"):
            return back(error="Confirm that you have read what each permission allows before continuing.")
        try:
            pending = google_api.authorization_request(scopes, login_hint=existing.account_email if existing else None)
        except google_api.GoogleError as exc:
            return back(error=str(exc))
        secret_store.save(PENDING_REF, {"state_hash": hashlib.sha256(pending.state.encode()).hexdigest(), "verifier": pending.verifier,
                                        "scopes": scopes, "expires": time.time() + 600, "connection_id": reconnect_id})
        response = RedirectResponse(pending.url, 303)
        response.set_cookie("fashion_oauth_state", pending.state, httponly=True, samesite="lax", max_age=600, path="/connections/google")
        return response

    @app.get("/connections/google/callback", dependencies=[Depends(require_admin)])
    def callback(request: Request, state: str = "", code: str = "", error: str = "", db: Session = Depends(get_db)):
        pending = secret_store.load(PENDING_REF)
        if pending.get("purpose") == "picker":
            return picker_callback(request, state, code, error, pending, db)
        cookie = request.cookies.get("fashion_oauth_state", "")
        valid = (state and cookie and secrets.compare_digest(cookie, state) and pending.get("expires", 0) > time.time()
                 and secrets.compare_digest(pending.get("state_hash", ""), hashlib.sha256(state.encode()).hexdigest()))
        secret_store.delete(PENDING_REF)
        if not valid:
            return back(error="Google sign-in could not be verified or expired. Start again.")
        if error or not code:
            return back(error="Google sign-in was cancelled; nothing changed.")
        try:
            result = google_api.exchange_code(code, pending["verifier"], transport=_transport())
        except google_api.GoogleError as exc:
            return back(error=str(exc))
        tokens = result["tokens"]
        granted = set(tokens.get("scope", "").split())
        missing = [scope for scope in pending["scopes"] if scope not in granted]
        existing = db.get(Connection, pending["connection_id"]) if pending.get("connection_id") else None
        if existing is not None and existing.account_email != result["email"]:
            other = db.scalar(select(Connection).where(Connection.provider == "google", Connection.account_email == result["email"],
                                                      Connection.status != "disconnected"))
            if other is None:  # Revoking would also cancel an existing connection's grant for that account.
                google_api.revoke(tokens.get("refresh_token") or tokens["access_token"], transport=_transport())
            return back(error=f"You signed in as {result['email']}, but this connection belongs to {existing.account_email}. Nothing was changed.")
        connection = existing or db.scalar(select(Connection).where(Connection.provider == "google", Connection.account_email == result["email"]))
        if connection is None:
            connection = Connection(provider="google", account_email=result["email"], account_subject=result.get("subject"),
                                    secret_ref="google-" + secrets.token_hex(8))
            db.add(connection)
        stored = secret_store.load(connection.secret_ref)
        refresh = tokens.get("refresh_token") or stored.get("refresh_token")
        if not refresh:
            return back(error="Google did not grant offline access. Remove this app's access in your Google account, then connect again.")
        secret_store.save(connection.secret_ref, {"refresh_token": refresh, "access_token": tokens["access_token"],
                                                  "expires_at": time.time() + int(tokens.get("expires_in", 3600))})
        connection.granted_scopes = " ".join(sorted(granted))
        connection.status, connection.status_reason = "connected", None
        db.commit()
        if missing:
            labels = ", ".join(scope.rsplit("/", 1)[-1] for scope in missing)
            return back(message=f"Connected {result['email']}, but these permissions were not granted: {labels}. Features that need them stay off.")
        return back(message=f"Connected {result['email']}.")

    def connection_or_404(db, connection_id):
        connection = db.get(Connection, connection_id)
        if connection is None:
            raise HTTPException(404, "Connection not found")
        return connection

    @app.post("/connections/{connection_id}/pause", dependencies=[Depends(require_admin)])
    def pause(connection_id: int, db: Session = Depends(get_db)):
        connection = connection_or_404(db, connection_id)
        connection.status = "paused" if connection.status == "connected" else ("connected" if connection.status == "paused" else connection.status)
        db.commit()
        return back(message=f"{connection.account_email}: {connection.status}.")

    @app.post("/connections/{connection_id}/disconnect", dependencies=[Depends(require_admin)])
    def disconnect(connection_id: int, confirm: str = Form(""), db: Session = Depends(get_db)):
        connection = connection_or_404(db, connection_id)
        if confirm != "DISCONNECT":
            return back(error="Type DISCONNECT to confirm. Captured evidence stays; future syncing stops.")
        stored = secret_store.load(connection.secret_ref)
        revoked = google_api.revoke(stored["refresh_token"], transport=_transport()) if stored.get("refresh_token") else False
        secret_store.delete(connection.secret_ref)
        connection.status = "disconnected"
        connection.status_reason = "Disconnected by the operator" + ("" if revoked else "; also remove the app at myaccount.google.com/permissions if it is still listed")
        for source in connection.sources:
            source.active = False
        db.commit()
        return back(message=f"Disconnected {connection.account_email}. Stored sources and records were kept.")

    @app.post("/connections/{connection_id}/sync", dependencies=[Depends(require_admin)])
    def sync_now(connection_id: int, db: Session = Depends(get_db)):
        connection = connection_or_404(db, connection_id)
        for source in connection.sources:
            if source.active and source.kind in {"drive_file", "drive_folder", "gmail_query"}:
                jobs.enqueue(db, "sync.source", {"source_id": source.id}, dedupe_key=f"sync:{source.id}")
        db.commit()
        return back(message="Sync queued; results appear below and in the source inbox.")

    @app.post("/connections/{connection_id}/picker/start", dependencies=[Depends(require_admin)])
    def picker_start(connection_id: int, db: Session = Depends(get_db)):
        """Ask Google for a short-lived drive.file-only token for the Picker (no other scopes, no refresh token)."""
        connection = connection_or_404(db, connection_id)
        pending = google_api.authorization_request([google_api.SCOPE_DRIVE_FILE], login_hint=connection.account_email,
                                                   offline=False, include_granted=False)
        secret_store.save(PENDING_REF, {"state_hash": hashlib.sha256(pending.state.encode()).hexdigest(), "verifier": pending.verifier,
                                        "expires": time.time() + 600, "connection_id": connection.id, "purpose": "picker"})
        response = RedirectResponse(pending.url, 303)
        response.set_cookie("fashion_oauth_state", pending.state, httponly=True, samesite="lax", max_age=600, path="/connections/google")
        return response

    def picker_callback(request, state, code, error, pending, db):
        cookie = request.cookies.get("fashion_oauth_state", "")
        valid = (state and cookie and secrets.compare_digest(cookie, state) and pending.get("expires", 0) > time.time()
                 and secrets.compare_digest(pending.get("state_hash", ""), hashlib.sha256(state.encode()).hexdigest()))
        secret_store.delete(PENDING_REF)
        if not valid or error or not code:
            return back(error="Picking files was cancelled or could not be verified.")
        connection = connection_or_404(db, pending["connection_id"])
        try:
            result = google_api.exchange_code(code, pending["verifier"], transport=_transport())
        except google_api.GoogleError as exc:
            return back(error=str(exc))
        granted = set(result["tokens"].get("scope", "").split())
        if result["email"] != connection.account_email:
            return back(error=f"Pick files with {connection.account_email}; you signed in as {result['email']}.")
        if not granted <= PICKER_ALLOWED:
            return back(error="Google returned a token with more than per-file Drive access, so it was not given to the browser.")
        return page(request, db, picker_connection=connection.id, picker_token=result["tokens"]["access_token"])

    @app.post("/connections/{connection_id}/sources", dependencies=[Depends(require_admin)])
    async def add_source(connection_id: int, request: Request, db: Session = Depends(get_db)):
        connection = connection_or_404(db, connection_id)
        form = await request.form()
        kind = str(form.get("kind", ""))
        granted = connection.granted_scopes.split()
        interval = int(form["interval_minutes"]) if str(form.get("interval_minutes", "")).isdigit() else 15
        interval = min(max(interval, 5), 24 * 60)
        if kind == "gmail_query":
            if google_api.SCOPE_GMAIL_READONLY not in granted:
                return back(error="Connect Gmail (read-only) first.")
            query = str(form.get("query", "")).strip()
            if not query or len(query) > 500:
                return back(error="Enter a Gmail search such as label:alina-orders (the import boundary).")
            try:
                start = date.fromisoformat(str(form.get("start_date"))) if form.get("start_date") else date.today() - timedelta(days=30)
            except ValueError:
                return back(error="Start date must be YYYY-MM-DD.")
            db.add(ConnectionSource(connection_id=connection.id, kind=kind, name=f"Gmail: {query}", query=query, start_date=start, interval_minutes=interval))
        elif kind == "drive_folder":
            if google_api.SCOPE_DRIVE_READONLY not in granted:
                return back(error="Watching a folder needs the read-only Drive permission. Reconnect and choose 'Watch a Drive folder'.")
            folder = _drive_id(str(form.get("folder", "")))
            if not folder:
                return back(error="Paste the folder link or id.")
            db.add(ConnectionSource(connection_id=connection.id, kind=kind, remote_id=folder, name=str(form.get("name") or folder)[:300], interval_minutes=interval))
        elif kind == "drive_file":
            ids = [value for value in json.loads(str(form.get("files", "[]"))) if isinstance(value, dict) and _drive_id(value.get("id", ""))]
            if not ids:
                return back(error="Pick files with the Google Picker.")
            for picked in ids[:50]:
                db.add(ConnectionSource(connection_id=connection.id, kind=kind, remote_id=picked["id"], name=str(picked.get("name", picked["id"]))[:300], interval_minutes=interval))
        elif kind in DESTINATION_NAMES:
            if google_api.SCOPE_DRIVE_FILE not in granted:
                return back(error="Connect Drive (per-file) first; destinations are folders this app creates.")
            if db.scalar(select(ConnectionSource).where(ConnectionSource.connection_id == connection.id, ConnectionSource.kind == kind, ConnectionSource.active.is_(True))):
                return back(error="That destination already exists.")
            db.add(ConnectionSource(connection_id=connection.id, kind=kind, name=DESTINATION_NAMES[kind]))
        else:
            return back(error="Unknown source type.")
        db.commit()
        return back(message="Saved. The first sync is queued." if not kind.startswith("drive_o") and not kind.startswith("drive_r") else "Destination saved; its folder is created on first upload.")

    @app.post("/connections/sources/{source_id}/toggle", dependencies=[Depends(require_admin)])
    def toggle(source_id: int, db: Session = Depends(get_db)):
        source = db.get(ConnectionSource, source_id)
        if source is None:
            raise HTTPException(404, "Source not found")
        source.active = not source.active
        db.commit()
        return back(message=f"{source.name}: {'active' if source.active else 'paused'}.")

    @app.post("/connections/sources/{source_id}/backfill", dependencies=[Depends(require_admin)])
    def backfill(source_id: int, start_date: str = Form(...), db: Session = Depends(get_db)):
        source = db.get(ConnectionSource, source_id)
        if source is None or source.kind != "gmail_query":
            raise HTTPException(404, "Gmail source not found")
        try:
            source.start_date = date.fromisoformat(start_date)
        except ValueError:
            return back(error="Start date must be YYYY-MM-DD.")
        source.checkpoint, source.checkpoint_kind = None, None  # Next sync is a full, deduplicated listing from this date.
        jobs.enqueue(db, "sync.source", {"source_id": source.id}, dedupe_key=f"sync:{source.id}")
        db.commit()
        return back(message=f"Backfill from {start_date} queued; messages already captured are skipped.")

    @app.post("/connections/{connection_id}/publish-exports", dependencies=[Depends(require_admin)])
    def publish_exports(connection_id: int, db: Session = Depends(get_db)):
        from app.outbox import queue_exports
        connection = connection_or_404(db, connection_id)
        try:
            count = queue_exports(db, connection)
        except ValueError as exc:
            return back(error=str(exc))
        db.commit()
        return back(message=f"{count} export file(s) queued for upload. They upload when Google is reachable.")


def _drive_id(value: str) -> str | None:
    value = (value or "").strip()
    match = re.search(r"/folders/([A-Za-z0-9_-]{10,})|/d/([A-Za-z0-9_-]{10,})|[?&]id=([A-Za-z0-9_-]{10,})", value)
    if match:
        return next(group for group in match.groups() if group)
    return value if re.fullmatch(r"[A-Za-z0-9_-]{10,}", value) else None


def _transport():
    from app import sync
    return sync.TRANSPORT
