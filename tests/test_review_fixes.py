"""Regression tests for findings of the code review (one test per finding, reproduced before the fix)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from fake_google import FakeGoogle, install_upload_put
from test_connections_sync import _mail, add_source, connect, inbox, run_sync, world  # noqa: F401
from app import google_api, jobs, outbox, secret_store, source_store, sync
from app.database import Base, get_db
from app.main import app
from app.models import Connection, ConnectionSource, Customer, Job, OrderItem, OutboxItem, Season, SourceItem

GMAIL = "openid email https://www.googleapis.com/auth/gmail.readonly"


def test_gmail_message_labelled_after_arrival_is_captured(world):
    fake, factory, _client = world
    connection_id = connect(world, GMAIL)
    source_id = add_source(factory, connection_id, kind="gmail_query", query="label:alina-orders", name="Gmail")
    run_sync(factory, source_id)  # empty mailbox: checkpoint set
    fake.add_message("m1", _mail("Order without label yet"), labels=())
    assert run_sync(factory, source_id)["captured"] == 0  # Does not match yet; checkpoint moves on.
    fake.add_label("m1", "alina-orders")  # The operator labels it later.
    assert run_sync(factory, source_id)["captured"] == 1


def test_oversized_message_is_recorded_and_does_not_block_the_source(world, monkeypatch):
    fake, factory, _client = world
    connection_id = connect(world, GMAIL)
    monkeypatch.setattr(source_store, "MAX_SOURCE_BYTES", 2000)
    fake.add_message("big", _mail("Huge", [("scan.pdf", "application/pdf", b"%PDF" + b"x" * 5000)]))
    fake.add_message("small", _mail("Small order"))
    source_id = add_source(factory, connection_id, kind="gmail_query", query="label:alina-orders", name="Gmail")
    result = run_sync(factory, source_id)
    assert result["failed"] == 1 and result["captured"] == 1
    items = {item.remote_id: item for item in inbox(factory, "gmail")}
    assert items["big"].state == "failed" and "larger than" in items["big"].state_reason
    assert items["small"].state == "captured"
    with factory() as db:
        assert db.get(ConnectionSource, source_id).checkpoint_kind == "gmail_history"


def test_lost_upload_response_reuses_folder_and_never_uploads_twice(world):
    fake, factory, client = world
    connection_id = connect(world, "openid email https://www.googleapis.com/auth/drive.file")
    add_source(factory, connection_id, kind="drive_outputs", name="Exports")
    fake.fail_put_once = True
    client.post(f"/connections/{connection_id}/publish-exports")
    jobs.run_pending(factory, kinds={"outbox.upload"})
    with factory() as db:
        item = db.scalar(select(OutboxItem))
        assert item.state == "queued" and "network interrupted" in item.last_error
        assert db.get(ConnectionSource, item.destination_id).remote_id == "created-1"  # kept despite the failure
        for job in db.scalars(select(Job)).all():
            job.run_after = jobs.now()
        db.commit()
    jobs.run_pending(factory, kinds={"outbox.upload"})
    with factory() as db:
        assert db.scalar(select(OutboxItem)).state == "verified"
    assert fake.folders_created == ["Exports"] and len(fake.uploads) == 1


def test_revoked_consent_on_upload_marks_reconnect(world):
    fake, factory, client = world
    connection_id = connect(world, "openid email https://www.googleapis.com/auth/drive.file")
    add_source(factory, connection_id, kind="drive_outputs", name="Exports")
    client.post(f"/connections/{connection_id}/publish-exports")
    fake.refresh_valid = False
    jobs.run_pending(factory, kinds={"outbox.upload"})
    with factory() as db:
        assert db.get(Connection, connection_id).status == "needs_reconnect"
        assert db.scalar(select(OutboxItem)).last_error


def test_sync_does_not_hold_the_write_lock_while_downloading(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{(tmp_path / 'lock.db').as_posix()}", connect_args={"check_same_thread": False, "timeout": 0.2})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    fake = install_upload_put(FakeGoogle())
    fake.granted = "openid email https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/drive.readonly"
    monkeypatch.setattr(sync, "TRANSPORT", fake.transport())
    monkeypatch.setattr(google_api.time, "sleep", lambda _s: None)
    monkeypatch.setattr("app.config.settings.google_client_id", "c.apps.googleusercontent.com")
    monkeypatch.setattr("app.config.settings.google_client_secret", "s")
    with factory() as db:
        connection = Connection(account_email=fake.email, granted_scopes=fake.granted, secret_ref="google-lock")
        db.add(connection)
        db.flush()
        source = ConnectionSource(connection_id=connection.id, kind="drive_folder", remote_id="folder-1", name="Orders")
        db.add(source)
        db.commit()
        source_id = source.id
    secret_store.save("google-lock", {"refresh_token": "r"})
    for index in range(3):
        fake.add_file(f"f{index}", f"order{index}.pdf", "application/pdf", b"%PDF " + bytes([index]))
    writes = []
    original = fake.drive

    def drive(request, path, params):
        if params.get("alt") == "media":
            other = sqlite3.connect(tmp_path / "lock.db", timeout=0.2)
            other.execute("BEGIN IMMEDIATE")  # An operator save at the same moment.
            other.execute("COMMIT")
            other.close()
            writes.append(path)
        return original(request, path, params)

    fake.drive = drive
    with factory() as db:
        assert sync.run_source_sync(db, source_id)["captured"] == 3
    assert len(writes) == 3
    engine.dispose()


def test_two_rows_for_one_new_customer_create_one_customer():
    from test_intake_workflow import bundle, facts  # noqa: F401
    from app import drafts
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        from app.models import Brand, BrandSeasonRule, Order
        season, brand = Season(name="FW26"), Brand(name="The Row")
        db.add_all([season, brand])
        db.flush()
        db.add(BrandSeasonRule(season_id=season.id, brand_id=brand.id, markup_percentage=D("0.30")))
        db.add(Order(season_id=season.id, brand_id=brand.id))
        db.commit()
        session = drafts.new_session(db, season)
        for name, size in (("Coat", "38"), ("Scarf", "One size")):
            row = drafts.add_row(db, session, "new_item", {})
            for field, value in (("customer", "Nina New"), ("order", "The Row"), ("name", name), ("size", size), ("quantity", "1"),
                                 ("price", "100"), ("price_meaning", "wholesale")):
                drafts.edit_field(db, session, row, field, value)
        db.commit()
        drafts.commit_session(db, session, operation_key="new-customer-key-0001", expected_revision=session.revision)
        assert db.scalar(select(func.count()).select_from(Customer)) == 1
        assert {item.customer.code for item in db.scalars(select(OrderItem)).all()} == {"NN"}


def test_requests_for_other_host_names_are_refused():
    client = TestClient(app)
    assert client.get("/health", headers={"Host": "evil.example:8001"}).status_code == 400
    assert client.get("/health", headers={"Host": "127.0.0.1:8001"}).status_code == 200


def test_wrong_account_reconnect_does_not_revoke_that_accounts_own_connection(world):
    fake, factory, client = world
    with factory() as db:
        db.add_all([Connection(account_email="a@example.com", granted_scopes="openid email", secret_ref="google-a"),
                    Connection(account_email="b@example.com", granted_scopes="openid email", secret_ref="google-b")])
        db.commit()
        a_id = db.scalar(select(Connection.id).where(Connection.account_email == "a@example.com"))
    started = client.post("/connections/google/start", data={"connection_id": str(a_id), "understood": "1"}, follow_redirects=False)
    state = dict(item.split("=", 1) for item in started.headers["location"].split("?", 1)[1].split("&"))["state"]
    fake.email = "b@example.com"
    response = client.get(f"/connections/google/callback?state={state}&code=x", cookies={"fashion_oauth_state": state}, follow_redirects=False)
    assert "belongs+to" in response.headers["location"]
    assert not any(request.endswith("/revoke") for request in fake.requests)


def test_evidence_with_non_latin_name_downloads(world):
    _fake, factory, client = world
    with factory() as db:
        item = source_store.capture(db, channel="upload", content=b"%PDF order", original_name="Заказ №5.pdf", media_type="application/pdf").item
        db.commit()
        item_id = item.id
    response = client.get(f"/sources/{item_id}/content")
    assert response.status_code == 200 and "filename*=UTF-8''" in response.headers["content-disposition"]


def test_gmail_retry_succeeds_when_an_attachment_is_empty(world, monkeypatch):
    fake, factory, _client = world
    connection_id = connect(world, GMAIL)
    add_source(factory, connection_id, kind="gmail_query", query="label:alina-orders", name="Gmail")
    fake.add_message("m1", _mail("Order", [("empty.pdf", "application/pdf", b"")]))
    with factory() as db:
        jobs.enqueue(db, "gmail.fetch", {"connection_id": connection_id, "message_id": "m1", "query": "label:alina-orders"})
        db.commit()
    jobs.run_pending(factory, kinds={"gmail.fetch"})
    with factory() as db:
        assert db.scalar(select(Job.state).where(Job.kind == "gmail.fetch")) == "done"
    items = {item.remote_id: item for item in inbox(factory, "gmail")}
    assert items["m1"].state == "captured" and items["m1:0"].state == "failed"


def test_reset_keeps_connections_and_a_vanished_job_is_harmless(tmp_path):
    from app.workspace_reset import reset_workspace
    engine = create_engine(f"sqlite:///{(tmp_path / 'r.db').as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        connection = Connection(account_email="a@example.com", secret_ref="google-r")
        db.add_all([connection, Season(name="FW26")])
        db.flush()
        db.add(ConnectionSource(connection_id=connection.id, kind="gmail_query", checkpoint="99", checkpoint_kind="gmail_history"))
        job = jobs.enqueue(db, "test.none", {})
        db.commit()
        job_id = job.id
        reset_workspace(db, "RESET WORKSPACE", root=tmp_path)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Connection)) == 1
        assert db.scalar(select(ConnectionSource.checkpoint)) is None
        assert db.scalar(select(func.count()).select_from(Season)) == 0
    assert jobs.run_job(factory, job_id) == "missing"


def test_a_rejected_access_token_is_refreshed_once_before_reconnect(tmp_path, monkeypatch):
    import httpx
    import time
    monkeypatch.setattr("app.config.settings.google_client_id", "c.apps.googleusercontent.com")
    monkeypatch.setattr("app.config.settings.google_client_secret", "s")
    secret_store.save("google-x", {"refresh_token": "r", "access_token": "stale", "expires_at": time.time() + 3000})
    calls = []

    def handler(request):
        if request.url.host == "oauth2.googleapis.com":
            calls.append("refresh")
            return httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600})
        if request.headers["Authorization"] == "Bearer stale":
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        return httpx.Response(200, json={"emailAddress": "a@example.com", "historyId": "1"})

    session = google_api.GoogleSession("google-x", transport=httpx.MockTransport(handler), sleep=lambda _s: None)
    assert google_api.GmailClient(session).profile()["historyId"] == "1" and calls == ["refresh"]


@pytest.mark.parametrize("text", ["1.200.50", "12.00.000", "1,20,000"])
def test_malformed_thousands_separators_are_not_guessed(text):
    from app.pricing import parse_amount_text
    assert parse_amount_text(text).amount is None


def test_zero_final_price_is_blocked_before_save():
    from app import drafts
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        season = Season(name="FW26")
        db.add(season)
        db.commit()
        session = drafts.new_session(db, season)
        row = drafts.add_row(db, session, "new_item", {})
        drafts.edit_field(db, session, row, "price", "0")
        drafts.edit_field(db, session, row, "price_meaning", "final")
        assert any("above zero" in issue.text for issue in drafts.row_issues(db, session, row))


def test_inbox_review_requires_a_season_and_keeps_failed_states(world):
    _fake, factory, client = world
    with factory() as db:
        message = source_store.capture(db, channel="gmail", content=b"From: a\r\n\r\nbody", original_name="Order", account="a",
                                       remote_id="m1", media_type="message/rfc822").item
        source_store.capture(db, channel="gmail", content=None, account="a", remote_id="m1:0", original_name="broken.pdf",
                             parent_id=message.id, state="failed", state_reason="undecodable")
        db.commit()
        season_id, message_id = db.scalar(select(Season.id)), message.id
    assert client.post(f"/inbox/{message_id}/intake", data={}).status_code == 422
    assert client.post(f"/inbox/{message_id}/intake", data={"season_id": season_id}, follow_redirects=False).status_code == 303
    with factory() as db:
        child = db.scalar(select(SourceItem).where(SourceItem.remote_id == "m1:0"))
        assert child.state == "failed" and child.session_id is not None


def test_backup_fingerprint_notices_in_place_edits(world):
    _fake, factory, _client = world
    with factory() as db:
        before = outbox.data_fingerprint(db)
        season = db.scalar(select(Season))
        season.name = "FW27"
        db.flush()
        assert outbox.data_fingerprint(db) != before
