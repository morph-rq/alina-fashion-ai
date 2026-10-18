"""Drive, Gmail, OAuth, outbox and recovery against a fake Google (no live account)."""
from __future__ import annotations

from datetime import date
from email.message import EmailMessage
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from fake_google import FakeGoogle, install_upload_put
from app import google_api, jobs, outbox, recovery_crypto, secret_store, sync
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Connection, ConnectionSource, Job, OrderItem, OutboxItem, Season, SourceBlob, SourceItem, SyncRun

FOLDER_SCOPES = "openid email https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive.readonly"
GMAIL_SCOPES = "openid email https://www.googleapis.com/auth/gmail.readonly"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture()
def world(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fake = install_upload_put(FakeGoogle())
    monkeypatch.setattr(sync, "TRANSPORT", fake.transport())
    monkeypatch.setattr(settings, "google_client_id", "test-client.apps.googleusercontent.com")
    monkeypatch.setattr(settings, "google_client_secret", "test-secret")
    monkeypatch.setattr(google_api.time, "sleep", lambda _seconds: None)
    with factory() as db:
        db.add(Season(name="FW26"))
        db.commit()

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    yield fake, factory, TestClient(app)
    app.dependency_overrides.clear()


def connect(world, scopes):
    fake, factory, _client = world
    fake.granted = scopes
    with factory() as db:
        connection = Connection(provider="google", account_email=fake.email, granted_scopes=scopes, secret_ref="google-test")
        db.add(connection)
        db.commit()
        secret_store.save("google-test", {"refresh_token": "refresh-token"})
        return connection.id


def add_source(factory, connection_id, **values):
    with factory() as db:
        source = ConnectionSource(connection_id=connection_id, **values)
        db.add(source)
        db.commit()
        return source.id


def run_sync(factory, source_id):
    with factory() as db:
        result = sync.run_source_sync(db, source_id)
        db.commit()
        return result


def inbox(factory, channel=None):
    with factory() as db:
        query = select(SourceItem).order_by(SourceItem.id)
        if channel:
            query = query.where(SourceItem.channel == channel)
        return db.scalars(query).all()


def test_oauth_flow_checks_state_scopes_and_account_ownership(world):
    fake, factory, client = world
    started = client.post("/connections/google/start", data={"drive_files": "1", "gmail": "1", "understood": "1"}, follow_redirects=False)
    assert started.status_code == 303 and started.headers["location"].startswith(google_api.AUTH_URL)
    location = started.headers["location"]
    assert "code_challenge=" in location and "access_type=offline" in location and "gmail.readonly" in location
    state = dict(item.split("=", 1) for item in location.split("?", 1)[1].split("&"))["state"]
    fake.granted = "openid email https://www.googleapis.com/auth/drive.file"  # The user unticked Gmail.
    done = client.get(f"/connections/google/callback?state={state}&code=abc", cookies={"fashion_oauth_state": state}, follow_redirects=False)
    assert "not+granted" in done.headers["location"] and "gmail.readonly" in done.headers["location"]
    with factory() as db:
        connection = db.scalar(select(Connection))
        assert connection.account_email == fake.email and "gmail.readonly" not in connection.granted_scopes
        assert "refresh-token" not in str(connection.__dict__)
        assert secret_store.load(connection.secret_ref)["refresh_token"] == "refresh-token"
        connection_id = connection.id
    # Reconnecting as a different Google account is refused.
    started = client.post("/connections/google/start", data={"connection_id": str(connection_id), "understood": "1"}, follow_redirects=False)
    state = dict(item.split("=", 1) for item in started.headers["location"].split("?", 1)[1].split("&"))["state"]
    fake.email = "someone-else@example.com"
    refused = client.get(f"/connections/google/callback?state={state}&code=abc", cookies={"fashion_oauth_state": state}, follow_redirects=False)
    assert "belongs+to" in refused.headers["location"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Connection)) == 1
    # A replayed callback (state already consumed) is refused.
    replay = client.get(f"/connections/google/callback?state={state}&code=abc", cookies={"fashion_oauth_state": state}, follow_redirects=False)
    assert "could+not+be+verified" in replay.headers["location"]


def test_drive_folder_listing_changes_and_checkpoints(world):
    fake, factory, _client = world
    connection_id = connect(world, FOLDER_SCOPES)
    fake.add_file("f1", "order.pdf", "application/pdf", b"%PDF-1.4 order")
    fake.add_file("f2", "list", "application/vnd.google-apps.spreadsheet")
    fake.add_file("f3", "video.mov", "video/quicktime", b"movie")
    fake.add_file("other", "elsewhere.pdf", "application/pdf", b"%PDF other", parents=("folder-2",))
    source_id = add_source(factory, connection_id, kind="drive_folder", remote_id="folder-1", name="Orders")
    first = run_sync(factory, source_id)
    assert first["captured"] == 2 and first["seen"] == 3
    items = {item.remote_id: item for item in inbox(factory, "drive")}
    assert set(items) == {"f1", "f2", "f3"}
    assert items["f3"].state == "ignored" and "Unsupported" in items["f3"].state_reason
    import json
    assert json.loads(items["f2"].metadata_json)["export_format"] == XLSX and items["f2"].original_name.endswith(".xlsx")
    # Changes after the listing: a modified file becomes a new version; a removed one is only noted.
    fake.add_file("f1", "order.pdf", "application/pdf", b"%PDF-1.4 order v2", version="2")
    fake.add_file("f4", "new.png", "image/png", b"\x89PNG\r\n\x1a\nimage")
    fake.remove_file("f2")
    second = run_sync(factory, source_id)
    assert second["captured"] == 2
    versions = [item for item in inbox(factory, "drive") if item.remote_id == "f1"]
    assert len(versions) == 2 and versions[0].blob_id != versions[1].blob_id
    assert "remote_removed_at" in json.loads([item for item in inbox(factory, "drive") if item.remote_id == "f2"][0].metadata_json)
    # Replaying the same changes captures nothing twice.
    with factory() as db:
        db.get(ConnectionSource, source_id).checkpoint = "4"
        db.commit()
    replay = run_sync(factory, source_id)
    assert replay["captured"] == 0 and replay["duplicates"] >= 2
    with factory() as db:
        assert db.get(ConnectionSource, source_id).checkpoint == str(len(fake.changes))


def test_failed_download_is_recorded_retried_and_never_skipped(world):
    fake, factory, _client = world
    connection_id = connect(world, FOLDER_SCOPES)
    source_id = add_source(factory, connection_id, kind="drive_folder", remote_id="folder-1", name="Orders")
    run_sync(factory, source_id)  # empty listing, sets the change checkpoint
    fake.add_file("f9", "invoice.pdf", "application/pdf", b"%PDF invoice")
    fake.fail_download.add("f9")
    result = run_sync(factory, source_id)
    assert result["failed"] == 1
    item = next(item for item in inbox(factory, "drive") if item.remote_id == "f9")
    assert item.state == "failed" and item.blob_id is None
    with factory() as db:
        retry = db.scalar(select(Job).where(Job.kind == "drive.fetch"))
        assert retry is not None and retry.state == "queued"
        retry.run_after = jobs.now()
        db.commit()
    fake.fail_download.clear()
    assert jobs.run_pending(factory, kinds={"drive.fetch"}) == 1
    item = next(item for item in inbox(factory, "drive") if item.remote_id == "f9")
    assert item.state == "captured" and item.blob_id is not None


def test_throttling_retries_and_revoked_consent_needs_reconnect(world):
    fake, factory, _client = world
    connection_id = connect(world, FOLDER_SCOPES)
    fake.add_file("f1", "order.pdf", "application/pdf", b"%PDF order")
    source_id = add_source(factory, connection_id, kind="drive_file", remote_id="f1", name="order.pdf")
    fake.throttle_next = 2
    assert run_sync(factory, source_id)["captured"] == 1
    secret_store.save("google-test", {"refresh_token": "refresh-token"})  # Force a token refresh.
    fake.refresh_valid = False
    with factory() as db:
        db.add(Job(kind="sync.source", payload_json=f'{{"source_id": {source_id}}}'))
        db.commit()
    jobs.run_pending(factory, kinds={"sync.source"})
    with factory() as db:
        connection = db.get(Connection, connection_id)
        assert connection.status == "needs_reconnect"
        assert db.scalar(select(Job.state).where(Job.kind == "sync.source")) == "dead"


def _mail(subject, attachments=(), html=None):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = "supplier@example.com", "operator@example.com", subject
    message["Date"] = "Mon, 05 Oct 2026 10:00:00 +0000"
    message.set_content("Order for Maria Ivanova: black coat 38, 1200 EUR")
    if html:
        message.add_alternative(html, subtype="html")
    for name, mime, content in attachments:
        maintype, subtype = mime.split("/")
        message.add_attachment(content, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes()


def test_gmail_full_sync_attachments_history_and_expired_history(world):
    fake, factory, _client = world
    connection_id = connect(world, GMAIL_SCOPES)
    fake.add_message("m1", _mail("Order 1", [("coat.jpg", "image/jpeg", b"\xff\xd8\xff photo"), ("tool.exe", "application/octet-stream", b"MZ")],
                                 html='<p>Hi <img src="https://tracker.example/pixel.gif"></p>'))
    fake.add_message("m2", _mail("Order 2"))
    fake.add_message("m3", _mail("Order 3"))
    source_id = add_source(factory, connection_id, kind="gmail_query", query="label:alina-orders", start_date=date(2026, 10, 1), name="Gmail")
    first = run_sync(factory, source_id)
    assert first["captured"] == 3
    assert not any("tracker.example" in request for request in fake.requests)
    messages = {item.remote_id: item for item in inbox(factory, "gmail")}
    assert messages["m1:0"].parent_id == messages["m1"].id and messages["m1:0"].state == "captured"
    assert messages["m1:1"].state == "ignored"
    import json
    meta = json.loads(messages["m1"].metadata_json)
    assert meta["subject"] == "Order 1" and "Maria Ivanova" in meta["body_text"] and meta["sender"] == "supplier@example.com"
    with factory() as db:
        source = db.get(ConnectionSource, source_id)
        assert source.checkpoint_kind == "gmail_history"
    fake.add_message("m4", _mail("Order 4"))
    assert run_sync(factory, source_id)["captured"] == 1
    fake.history_expired = True
    fake.add_message("m5", _mail("Order 5"))
    resync = run_sync(factory, source_id)
    assert resync["captured"] == 1 and resync["duplicates"] == 4
    with factory() as db:
        assert "full resync" in db.scalar(select(SyncRun.note).order_by(SyncRun.id.desc()).limit(1))
    # No message is ever modified: only reads were made.
    assert all(request.split()[0] == "GET" for request in fake.requests if "gmail" in request or "/messages" in request)


def test_same_attachment_from_mail_and_drive_shares_bytes_but_not_identity(world, monkeypatch):
    fake, factory, _client = world
    connection_id = connect(world, FOLDER_SCOPES + " https://www.googleapis.com/auth/gmail.readonly")
    pdf = b"%PDF-1.4 the same order sheet"
    fake.add_message("m1", _mail("Order", [("order.pdf", "application/pdf", pdf)]))
    fake.add_file("f1", "order.pdf", "application/pdf", pdf)
    run_sync(factory, add_source(factory, connection_id, kind="gmail_query", query="has:attachment", name="Gmail"))
    run_sync(factory, add_source(factory, connection_id, kind="drive_file", remote_id="f1", name="order.pdf"))
    with factory() as db:
        pdf_items = [item for item in db.scalars(select(SourceItem)).all() if item.media_type == "application/pdf"]
        assert len(pdf_items) == 2 and {item.channel for item in pdf_items} == {"gmail", "drive"}
        assert len({item.blob_id for item in pdf_items}) == 1
        assert db.scalar(select(func.count()).select_from(SourceBlob).where(SourceBlob.media_type == "application/pdf")) == 1
        assert db.scalar(select(func.count()).select_from(OrderItem)) == 0  # Evidence alone never creates goods.


def test_outbox_queues_offline_and_uploads_once_verified(world):
    fake, factory, client = world
    connection_id = connect(world, "openid email https://www.googleapis.com/auth/drive.file")
    destination_id = add_source(factory, connection_id, kind="drive_outputs", name="Alina Fashion exports")
    fake.offline = True
    response = client.post(f"/connections/{connection_id}/publish-exports", follow_redirects=False)
    assert "queued" in response.headers["location"]
    with factory() as db:
        for job in db.scalars(select(Job).where(Job.kind == "outbox.upload")).all():
            job.run_after = jobs.now()
        db.commit()
    jobs.run_pending(factory, kinds={"outbox.upload"})
    with factory() as db:
        item = db.scalar(select(OutboxItem))
        assert item.state == "queued" and "network" in (db.scalar(select(Job.last_error)) or "")
        for job in db.scalars(select(Job)).all():
            job.run_after = jobs.now()
        db.commit()
    fake.offline = False
    jobs.run_pending(factory, kinds={"outbox.upload"})
    with factory() as db:
        item = db.scalar(select(OutboxItem))
        assert item.state == "verified" and item.remote_id
        assert db.get(ConnectionSource, destination_id).remote_id == "created-1"
    assert len(fake.uploads) == 1
    with factory() as db:  # A retried upload of the same file finds it by hash instead of uploading again.
        item = db.scalar(select(OutboxItem))
        item.state = "queued"
        db.commit()
        outbox.upload_job(db, {"outbox_id": item.id})
        db.commit()
    assert len(fake.uploads) == 1


def test_encrypted_recovery_upload_and_restore_with_written_code(world, tmp_path):
    fake, factory, client = world
    connection_id = connect(world, "openid email https://www.googleapis.com/auth/drive.file")
    add_source(factory, connection_id, kind="drive_recovery", name="Alina Fashion recovery archives")
    page = client.post("/recovery/key")
    code = page.text.split('letter-spacing:.05em;overflow-wrap:anywhere">')[1].split("<")[0].strip()
    assert len(code.replace("-", "")) == 52
    with factory() as db:
        db.add(SourceItem(channel="text", remote_id="x", original_name="note.txt"))
        db.commit()
    location = client.post("/recovery/backup", follow_redirects=False).headers["location"]
    message = __import__("urllib.parse").parse.unquote_plus(location)
    assert "Verified snapshot created" in message and "upload queued" in message
    with factory() as db:
        for job in db.scalars(select(Job)).all():
            job.run_after = jobs.now()
        db.commit()
    jobs.run_pending(factory, kinds={"outbox.upload"})
    with factory() as db:
        item = db.scalar(select(OutboxItem).where(OutboxItem.kind == "recovery"))
        assert item.state == "verified"
    uploaded = next(iter(fake.upload_bytes.values()))
    assert uploaded.startswith(recovery_crypto.MAGIC) and b"fashion_orders.db" not in uploaded  # Encrypted before it leaves.
    # Recovery on a "new computer": only the uploaded archive and the written code are used.
    archive = tmp_path / "downloaded.afra"
    archive.write_bytes(uploaded)
    from app.recovery_routes import restore_exercise
    report = restore_exercise(recovery_crypto.parse_code(code), tmp_path / "new-computer", archive=archive, keep=True)
    assert report["table_counts"]["source_items"] == 1 and report["table_counts"]["seasons"] == 1
    assert (tmp_path / "new-computer").exists()
    with pytest.raises(recovery_crypto.RecoveryKeyError):
        restore_exercise(recovery_crypto.new_recovery_key(), tmp_path / "wrong", archive=archive)
    tampered = tmp_path / "tampered.afra"
    tampered.write_bytes(uploaded[:-10] + b"0123456789")
    with pytest.raises(ValueError):
        restore_exercise(recovery_crypto.parse_code(code), tmp_path / "tamper", archive=tampered)
