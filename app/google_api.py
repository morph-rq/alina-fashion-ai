"""Google OAuth (web-server flow with PKCE) and small Drive/Gmail REST clients.

Only official endpoints are used. Clients take an optional ``httpx`` transport
so tests can simulate Google precisely without credentials or network access.
Errors are converted to short operator-readable messages that never include
tokens, response bodies or signed URLs.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from typing import Any, Iterator
from urllib.parse import urlencode

import httpx

from app import secret_store
from app.config import settings

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"

IDENTITY_SCOPES = ["openid", "email"]
SCOPE_DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
SCOPE_DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
SCOPE_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_EXPLANATIONS = {
    SCOPE_DRIVE_FILE: "Drive, per file (non-sensitive): only files you pick in the Google Picker and files this app creates "
                      "(export and recovery folders). The app cannot see the rest of your Drive.",
    SCOPE_DRIVE_READONLY: "Drive, read-only for all files (restricted): needed only to watch a whole folder unattended, because "
                          "Google does not document that picking a folder grants access to the files inside it. "
                          "The app still reads only the folder you select, but Google's consent covers your whole Drive.",
    SCOPE_GMAIL_READONLY: "Gmail, read-only (restricted): Google offers no label-only permission, so consent covers all mail. "
                          "The app itself reads only messages matching your chosen query and start date, and never "
                          "deletes, labels, archives, marks read or sends mail.",
}
# Native Google files are exported to a stored format; the chosen format is recorded with the evidence.
EXPORT_FORMATS = {
    "application/vnd.google-apps.spreadsheet": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
}
SUPPORTED_DOWNLOADS = {"application/pdf", "image/jpeg", "image/png", "image/webp", "text/plain", "text/csv",
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "application/vnd.ms-excel.sheet.macroEnabled.12",
                       "audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/webm"}


class GoogleError(Exception):
    """A Google API failure with a safe message. ``retryable`` suggests backoff."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False, reconnect: bool = False):
        super().__init__(message)
        self.status, self.retryable, self.reconnect = status, retryable, reconnect


def redirect_uri() -> str:
    return settings.public_base_url.rstrip("/") + "/connections/google/callback"


def client_config() -> dict:
    """Client id/secret from settings, or from the protected uploaded client JSON."""
    if settings.google_client_id and settings.google_client_secret:
        return {"client_id": settings.google_client_id, "client_secret": settings.google_client_secret}
    return secret_store.load("google-client")


def save_client_json(payload: bytes) -> None:
    config = json.loads(payload)
    config = config.get("web") or config.get("installed") or {}
    if not str(config.get("client_id", "")).endswith(".apps.googleusercontent.com") or not config.get("client_secret"):
        raise ValueError("Upload the OAuth client JSON downloaded from Google Cloud (Web application type).")
    if redirect_uri() not in config.get("redirect_uris", []):
        raise ValueError(f"The client must list the redirect URI {redirect_uri()} exactly.")
    secret_store.save("google-client", {"client_id": config["client_id"], "client_secret": config["client_secret"]})


@dataclass
class PendingAuth:
    url: str
    state: str
    verifier: str


def authorization_request(scopes: list[str], login_hint: str | None = None) -> PendingAuth:
    config = client_config()
    if not config.get("client_id"):
        raise GoogleError("Configure the Google OAuth client first (see the setup guide).")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    params = {"client_id": config["client_id"], "redirect_uri": redirect_uri(), "response_type": "code",
              "scope": " ".join(dict.fromkeys(IDENTITY_SCOPES + scopes)), "access_type": "offline", "prompt": "consent",
              "include_granted_scopes": "true", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    if login_hint:
        params["login_hint"] = login_hint
    return PendingAuth(url=AUTH_URL + "?" + urlencode(params), state=state, verifier=verifier)


def _raise_for(response: httpx.Response, action: str) -> None:
    if response.status_code < 400:
        return
    status = response.status_code
    reason = ""
    try:
        body = response.json()
        error = body.get("error")
        reason = (error.get("status") or error.get("message", "")) if isinstance(error, dict) else str(error or "")
        if isinstance(error, dict):
            reasons = [item.get("reason") for item in error.get("errors", []) if isinstance(item, dict)]
            reason = ",".join(filter(None, reasons)) or reason
    except ValueError:
        pass
    reason = reason[:80]
    if status in {401} or reason in {"invalid_grant", "unauthorized_client"}:
        raise GoogleError(f"{action}: Google access was revoked or expired; reconnect the account.", status=status, reconnect=True)
    if status == 403 and any(word in reason for word in ("rateLimitExceeded", "userRateLimitExceeded", "RESOURCE_EXHAUSTED")):
        raise GoogleError(f"{action}: Google rate limit; retrying later.", status=status, retryable=True)
    if status == 403:
        raise GoogleError(f"{action}: permission denied ({reason or 'forbidden'}).", status=status)
    if status == 404:
        raise GoogleError(f"{action}: not found or no longer shared with this app.", status=status)
    if status == 429 or status >= 500:
        raise GoogleError(f"{action}: Google is temporarily unavailable (HTTP {status}).", status=status, retryable=True)
    raise GoogleError(f"{action}: HTTP {status} {reason}".strip(), status=status)


def exchange_code(code: str, verifier: str, transport: httpx.BaseTransport | None = None) -> dict:
    config = client_config()
    with httpx.Client(timeout=30, transport=transport) as client:
        response = client.post(TOKEN_URL, data={"code": code, "client_id": config["client_id"], "client_secret": config["client_secret"],
                                                "redirect_uri": redirect_uri(), "grant_type": "authorization_code", "code_verifier": verifier})
        _raise_for(response, "Google sign-in")
        tokens = response.json()
        info = client.get(USERINFO_URL, headers={"Authorization": f"Bearer {tokens['access_token']}"})
        _raise_for(info, "Google account lookup")
    profile = info.json()
    if not profile.get("email") or not profile.get("email_verified", True):
        raise GoogleError("Google did not return a verified e-mail address for this account.")
    return {"tokens": tokens, "email": profile["email"].lower(), "subject": profile.get("sub")}


def revoke(token: str, transport: httpx.BaseTransport | None = None) -> bool:
    try:
        with httpx.Client(timeout=15, transport=transport) as client:
            return client.post(REVOKE_URL, data={"token": token}).status_code == 200
    except httpx.HTTPError:
        return False


class GoogleSession:
    """Authorised HTTP access for one connection; refreshes and persists tokens."""

    def __init__(self, secret_ref: str, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.secret_ref = secret_ref
        self.transport = transport
        self.sleep = sleep
        self.client = httpx.Client(timeout=60, transport=transport, follow_redirects=False)

    def close(self):
        self.client.close()

    def _token(self) -> str:
        stored = secret_store.load(self.secret_ref)
        if not stored.get("refresh_token"):
            raise GoogleError("This connection has no stored Google authorisation; reconnect it.", reconnect=True)
        if stored.get("access_token") and stored.get("expires_at", 0) > time.time() + 60:
            return stored["access_token"]
        config = client_config()
        try:
            response = self.client.post(TOKEN_URL, data={"client_id": config.get("client_id"), "client_secret": config.get("client_secret"),
                                                         "refresh_token": stored["refresh_token"], "grant_type": "refresh_token"})
        except (httpx.TimeoutException, httpx.NetworkError):
            raise GoogleError("Refreshing Google access: network unavailable.", retryable=True) from None
        _raise_for(response, "Refreshing Google access")
        body = response.json()
        stored["access_token"] = body["access_token"]
        stored["expires_at"] = time.time() + int(body.get("expires_in", 3600))
        if body.get("refresh_token"):  # Google may rotate it; keep the newest.
            stored["refresh_token"] = body["refresh_token"]
        secret_store.save(self.secret_ref, stored)
        return stored["access_token"]

    def request(self, method: str, url: str, action: str, *, attempts: int = 4, **kwargs) -> httpx.Response:
        for attempt in range(attempts):
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {self._token()}"
            try:
                response = self.client.request(method, url, headers=headers, **kwargs)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == attempts - 1:
                    raise GoogleError(f"{action}: network unavailable.", retryable=True) from None
                self.sleep(min(2 ** attempt, 20))
                kwargs["headers"] = headers
                continue
            try:
                _raise_for(response, action)
                return response
            except GoogleError as exc:
                if exc.retryable and attempt < attempts - 1:
                    retry_after = response.headers.get("Retry-After", "")
                    self.sleep(min(float(retry_after) if retry_after.isdigit() else 2 ** attempt, 30))
                    kwargs["headers"] = headers
                    continue
                raise
        raise GoogleError(f"{action}: gave up after retries.", retryable=True)


FILE_FIELDS = "id,name,mimeType,md5Checksum,headRevisionId,version,modifiedTime,trashed,parents,webViewLink,size"


class DriveClient:
    def __init__(self, session: GoogleSession):
        self.s = session

    def get(self, file_id: str) -> dict:
        return self.s.request("GET", f"{DRIVE}/files/{file_id}", "Reading Drive file details",
                              params={"fields": FILE_FIELDS, "supportsAllDrives": "true"}).json()

    def list_folder(self, folder_id: str) -> Iterator[dict]:
        token = None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed = false", "fields": f"nextPageToken,files({FILE_FIELDS})",
                      "pageSize": 200, "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            body = self.s.request("GET", f"{DRIVE}/files", "Listing Drive folder", params=params).json()
            yield from body.get("files", [])
            token = body.get("nextPageToken")
            if not token:
                return

    def start_page_token(self) -> str:
        return self.s.request("GET", f"{DRIVE}/changes/startPageToken", "Starting Drive change tracking",
                              params={"supportsAllDrives": "true"}).json()["startPageToken"]

    def changes_page(self, page_token: str) -> dict:
        return self.s.request("GET", f"{DRIVE}/changes", "Reading Drive changes", params={
            "pageToken": page_token, "pageSize": 200, "includeRemoved": "true", "spaces": "drive",
            "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
            "fields": f"nextPageToken,newStartPageToken,changes(fileId,removed,changeType,time,file({FILE_FIELDS}))"}).json()

    def download(self, meta: dict) -> tuple[bytes, str, dict]:
        """Return (bytes, media type, extra provenance) for a supported file or native Google document."""
        mime = meta.get("mimeType", "")
        if mime in EXPORT_FORMATS:
            target, _suffix = EXPORT_FORMATS[mime]
            response = self.s.request("GET", f"{DRIVE}/files/{meta['id']}/export", "Exporting Google document",
                                      params={"mimeType": target})
            return response.content, target, {"exported_from": mime, "export_format": target}
        if mime not in SUPPORTED_DOWNLOADS:
            raise GoogleError(f"Unsupported Drive file type {mime}.")
        response = self.s.request("GET", f"{DRIVE}/files/{meta['id']}", "Downloading Drive file",
                                  params={"alt": "media", "supportsAllDrives": "true"})
        content = response.content
        if meta.get("md5Checksum") and hashlib.md5(content).hexdigest() != meta["md5Checksum"]:
            raise GoogleError("Downloaded Drive file failed its checksum; it will be retried.", retryable=True)
        return content, mime, {}

    def ensure_folder(self, name: str, folder_id: str | None = None) -> str:
        if folder_id:
            try:
                meta = self.get(folder_id)
                if not meta.get("trashed"):
                    return folder_id
            except GoogleError as exc:
                if exc.status != 404:
                    raise
        body = self.s.request("POST", f"{DRIVE}/files", "Creating Drive folder", params={"fields": "id"},
                              json={"name": name, "mimeType": "application/vnd.google-apps.folder"}).json()
        return body["id"]

    def find_by_hash(self, folder_id: str, sha256: str) -> dict | None:
        body = self.s.request("GET", f"{DRIVE}/files", "Checking Drive destination", params={
            "q": f"'{folder_id}' in parents and trashed = false and appProperties has {{ key='sha256' and value='{sha256}' }}",
            "fields": "files(id,name,md5Checksum,size)", "pageSize": 10}).json()
        files = body.get("files", [])
        return files[0] if files else None

    def upload(self, folder_id: str, name: str, content_path, media_type: str, sha256: str) -> dict:
        """Resumable upload; the result is verified by MD5 before it counts as uploaded."""
        from pathlib import Path
        from urllib.parse import urlparse
        path = Path(content_path)
        size = path.stat().st_size
        start = self.s.request("POST", f"{DRIVE_UPLOAD}/files", "Starting Drive upload",
                               params={"uploadType": "resumable", "fields": "id,md5Checksum,name,size"},
                               headers={"X-Upload-Content-Type": media_type, "X-Upload-Content-Length": str(size)},
                               json={"name": name, "parents": [folder_id], "appProperties": {"sha256": sha256}})
        location = start.headers.get("Location", "")
        parsed = urlparse(location)
        if parsed.scheme != "https" or parsed.hostname != "www.googleapis.com":
            raise GoogleError("Google returned an unexpected upload address; upload stopped.")
        result = None
        offset = 0
        if size == 0:
            raise GoogleError("Refusing to upload an empty file.")
        with path.open("rb") as stream:
            while offset < size:
                chunk = stream.read(8 * 1024 * 1024)
                end = offset + len(chunk) - 1
                response = self.s.client.put(location, headers={"Content-Type": media_type, "Content-Range": f"bytes {offset}-{end}/{size}"},
                                             content=chunk)
                if response.status_code == 308:
                    offset += len(chunk)
                    continue
                _raise_for(response, "Uploading to Drive")
                result = response.json()
                break
        expected_md5 = hashlib.md5(path.read_bytes()).hexdigest()
        if not result or result.get("md5Checksum") != expected_md5:
            raise GoogleError("Drive checksum did not match the local file; the upload is not counted.", retryable=True)
        return result


class GmailClient:
    def __init__(self, session: GoogleSession):
        self.s = session

    def profile(self) -> dict:
        return self.s.request("GET", f"{GMAIL}/profile", "Reading Gmail profile").json()

    def list_ids(self, query: str) -> Iterator[tuple[str, str | None]]:
        """Yield (message id, page token used) for a query, oldest page first as returned."""
        token = None
        while True:
            params = {"q": query, "maxResults": 100}
            if token:
                params["pageToken"] = token
            body = self.s.request("GET", f"{GMAIL}/messages", "Listing Gmail messages", params=params).json()
            for message in body.get("messages", []):
                yield message["id"], token
            token = body.get("nextPageToken")
            if not token:
                return

    def history_page(self, start_history_id: str, page_token: str | None = None) -> dict:
        params: dict[str, Any] = {"startHistoryId": start_history_id, "historyTypes": "messageAdded", "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        return self.s.request("GET", f"{GMAIL}/history", "Reading Gmail history", params=params).json()

    def raw_message(self, message_id: str) -> dict:
        return self.s.request("GET", f"{GMAIL}/messages/{message_id}", "Reading Gmail message", params={"format": "raw"}).json()
