"""An in-memory imitation of the Google endpoints the app uses (for httpx.MockTransport).

It models only documented behaviour the app depends on: OAuth token and userinfo
responses, Drive files/changes/export/upload with paging, and Gmail profile,
message listing, raw messages and history with paging and expiry (404).
"""
from __future__ import annotations

import base64
import hashlib
import json
from urllib.parse import parse_qs

import httpx


class FakeGoogle:
    def __init__(self, email="operator@example.com"):
        self.email = email
        self.granted = "openid email https://www.googleapis.com/auth/drive.file"
        self.refresh_valid = True
        self.files: dict[str, dict] = {}
        self.content: dict[str, bytes] = {}
        self.changes: list[dict] = []
        self.page_size = 2
        self.fail_download: set[str] = set()
        self.throttle_next = 0
        self.uploads: dict[str, dict] = {}
        self.upload_bytes: dict[str, bytes] = {}
        self.folders_created: list[str] = []
        self.messages: dict[str, bytes] = {}
        self.message_order: list[str] = []
        self.history: list[dict] = []
        self.history_expired = False
        self.history_id = 100
        self.requests: list[str] = []
        self.offline = False
        self.labels: dict[str, set[str]] = {}
        self.fail_put_once = False

    # --- test helpers --------------------------------------------------------------
    def add_file(self, file_id, name, mime, content=b"", parents=("folder-1",), version="1"):
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": mime, "parents": list(parents), "trashed": False,
                               "headRevisionId": f"{file_id}-r{version}", "md5Checksum": hashlib.md5(content).hexdigest(),
                               "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
        self.content[file_id] = content
        self.changes.append({"fileId": file_id, "removed": False, "file": self.files[file_id]})

    def remove_file(self, file_id):
        self.files[file_id]["trashed"] = True
        self.changes.append({"fileId": file_id, "removed": True})

    def add_message(self, message_id, raw: bytes, labels=("alina-orders",)):
        self.messages[message_id] = raw
        self.message_order.append(message_id)
        self.labels[message_id] = set(labels)
        self.history_id += 1
        self.history.append({"id": str(self.history_id), "messagesAdded": [{"message": {"id": message_id}}]})

    def add_label(self, message_id, label):
        self.labels[message_id].add(label)
        self.history_id += 1
        self.history.append({"id": str(self.history_id), "labelsAdded": [{"message": {"id": message_id}, "labelIds": [label]}]})

    def transport(self):
        return httpx.MockTransport(self.handle)

    # --- dispatcher -------------------------------------------------------------------
    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(f"{request.method} {request.url.path}")
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        host, path = request.url.host, request.url.path
        params = dict(request.url.params)
        if host == "oauth2.googleapis.com" and path == "/token":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"] and not self.refresh_valid:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "access-token", "expires_in": 3600, "refresh_token": "refresh-token",
                                             "scope": self.granted, "token_type": "Bearer"})
        if host == "oauth2.googleapis.com" and path == "/revoke":
            return httpx.Response(200)
        if host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"email": self.email, "email_verified": True, "sub": "subject-1"})
        if self.throttle_next:
            self.throttle_next -= 1
            return httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}, headers={"Retry-After": "0"})
        if host == "www.googleapis.com" and path.startswith("/drive/v3"):
            return self.drive(request, path.removeprefix("/drive/v3"), params)
        if host == "www.googleapis.com" and path.startswith("/upload/drive/v3"):
            return self.drive_upload(request, params)
        if host == "gmail.googleapis.com":
            return self.gmail(path.removeprefix("/gmail/v1/users/me"), params)
        raise AssertionError(f"Unexpected request {request.method} {request.url}")

    def drive(self, request, path, params):
        if path == "/changes/startPageToken":
            return httpx.Response(200, json={"startPageToken": str(len(self.changes))})
        if path == "/changes":
            start = int(params["pageToken"])
            page = self.changes[start:start + self.page_size]
            body = {"changes": page}
            if start + self.page_size < len(self.changes):
                body["nextPageToken"] = str(start + self.page_size)
            else:
                body["newStartPageToken"] = str(len(self.changes))
            return httpx.Response(200, json=body)
        if path == "/files" and request.method == "GET":
            query = params.get("q", "")
            if "appProperties" in query:
                sha = query.split("value='")[1].split("'")[0]
                folder = query.split("'")[1]
                found = [meta for meta in self.uploads.values() if meta["appProperties"]["sha256"] == sha and folder in meta["parents"]]
                return httpx.Response(200, json={"files": found})
            folder = query.split("'")[1]
            children = [meta for meta in self.files.values() if folder in meta["parents"] and not meta["trashed"]]
            start = int(params.get("pageToken", 0))
            body = {"files": children[start:start + self.page_size]}
            if start + self.page_size < len(children):
                body["nextPageToken"] = str(start + self.page_size)
            return httpx.Response(200, json=body)
        if path == "/files" and request.method == "POST":
            folder_id = f"created-{len(self.folders_created) + 1}"
            self.folders_created.append(json.loads(request.content)["name"])
            return httpx.Response(200, json={"id": folder_id})
        if path.startswith("/files/"):
            parts = path.split("/")
            file_id = parts[2]
            if file_id.startswith("created-"):
                return httpx.Response(200, json={"id": file_id, "trashed": False})
            meta = self.files.get(file_id)
            if meta is None:
                return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
            if len(parts) > 3 and parts[3] == "export":
                return httpx.Response(200, content=b"PK\x03\x04exported:" + file_id.encode())
            if params.get("alt") == "media":
                if file_id in self.fail_download:
                    return httpx.Response(500, json={"error": {"status": "INTERNAL"}})
                return httpx.Response(200, content=self.content[file_id])
            return httpx.Response(200, json=meta)
        raise AssertionError(f"Unexpected Drive call {path}")

    def drive_upload(self, request, params):
        if request.method == "POST":
            self._pending = json.loads(request.content)
            return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/drive/v3/files?upload_id=1"})
        raise AssertionError("upload start expected")

    def put_upload(self, request):
        content = request.content
        if self.fail_put_once:
            # The upload reaches Drive but the response is lost.
            self.fail_put_once = False
            self.uploads[f"upload-{len(self.uploads) + 1}"] = dict(self._pending, id=f"upload-{len(self.uploads) + 1}",
                                                                    md5Checksum=hashlib.md5(content).hexdigest())
            raise httpx.ReadTimeout("response lost", request=request)
        file_id = f"upload-{len(self.uploads) + 1}"
        meta = dict(self._pending, id=file_id, md5Checksum=hashlib.md5(content).hexdigest(), size=str(len(content)))
        self.uploads[file_id] = meta
        self.upload_bytes[file_id] = content
        return httpx.Response(200, json=meta)

    def gmail(self, path, params):
        if path == "/profile":
            return httpx.Response(200, json={"emailAddress": self.email, "historyId": str(self.history_id)})
        if path == "/messages":
            query = params.get("q", "")
            wanted = [part.split(":", 1)[1] for part in query.split() if part.startswith("label:")]
            ids = [message_id for message_id in reversed(self.message_order)
                   if all(label in self.labels.get(message_id, set()) for label in wanted)]
            start = int(params.get("pageToken", 0))
            body = {"messages": [{"id": message_id, "threadId": f"t-{message_id}"} for message_id in ids[start:start + self.page_size]]}
            if start + self.page_size < len(ids):
                body["nextPageToken"] = str(start + self.page_size)
            return httpx.Response(200, json=body)
        if path.startswith("/messages/"):
            message_id = path.split("/")[2]
            raw = base64.urlsafe_b64encode(self.messages[message_id]).decode().rstrip("=")
            return httpx.Response(200, json={"id": message_id, "threadId": f"t-{message_id}", "labelIds": ["Label_1"], "raw": raw})
        if path == "/history":
            if self.history_expired:
                return httpx.Response(404, json={"error": {"errors": [{"reason": "notFound"}]}})
            start = int(params["startHistoryId"])
            records = [record for record in self.history if int(record["id"]) > start]
            return httpx.Response(200, json={"history": records, "historyId": str(self.history_id)})
        raise AssertionError(f"Unexpected Gmail call {path}")


def install_upload_put(fake: FakeGoogle):
    """Route PUT requests to the upload session to the fake."""
    original = fake.handle

    def handle(request):
        if request.method == "PUT" and request.url.path.startswith("/upload/"):
            return fake.put_upload(request)
        return original(request)

    fake.handle = handle
    return fake
