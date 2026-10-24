"""Integration: the real routes from upload to documents, on a real (temporary) SQLite file."""
from __future__ import annotations

import time
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import factory_fixture
from test_factory_settlement import db  # noqa: F401
from app import customer_documents, factory_routes
from app.database import get_db
from app.main import app
from app.models import Customer, CustomerPricingRule, GeneratedDocument, OrderItem


@pytest.fixture()
def client(db, tmp_path, monkeypatch):
    monkeypatch.setattr(factory_routes, "IMPORT_DIR", tmp_path / "imports")
    monkeypatch.setattr(factory_routes, "ROOT_DIR", tmp_path)
    real = customer_documents.generate
    monkeypatch.setattr(customer_documents, "generate", lambda *a, **k: real(*a, **{**k, "root": tmp_path}))
    app.dependency_overrides[get_db] = lambda: (yield db)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def wait_ready(client, token):
    for _ in range(100):
        status = client.get(f"/factory-import/{token}/status").json()
        if status["state"] not in ("queued", "analysing"):
            return status
        time.sleep(0.1)
    raise AssertionError("analysis did not finish")


def test_upload_review_commit_and_every_screen(client, db, tmp_path):
    path = factory_fixture.build(tmp_path / "Dior cruise SS27.xlsx")
    with open(path, "rb") as handle:
        response = client.post("/factory-import", files=[("files", (path.name, handle, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))],
                               headers={"Accept": "application/json"})
    token = response.json()["tokens"][0]
    status = wait_ready(client, token)
    assert status["state"] == "ready" and status["items"] == 6 and status["unclear"] == 1
    review = client.get(f"/factory-import/{token}")
    assert review.status_code == 200 and "Кому относится каждая штука" in review.text and "TEST-SHT-06" in review.text
    image = client.get(f"/factory-import/{token}/image", params={"part": "xl/media/image1.png"})
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert client.get(f"/factory-import/{token}/image", params={"part": "xl/workbook.xml"}).status_code == 404
    shirt_row = 9
    committed = client.post(f"/factory-import/{token}/commit", data={
        "season": "SS27", "owner": "ALINA", "rates_confirmed": "on", "rate:Dior cruise:0": "30",
        f"alloc:Dior cruise:{shirt_row}:0": "IDS", f"size:Dior cruise:{shirt_row}:0": "40",
        f"alloc:Dior cruise:{shirt_row}:1": "ALINA", f"size:Dior cruise:{shirt_row}:1": "38"}, follow_redirects=False)
    assert committed.status_code == 303 and committed.headers["location"] == "/board?season=SS27"
    assert db.scalar(select(OrderItem).where(OrderItem.customer_id.is_(None))) is None   # resolved on the review screen
    board = client.get("/board?season=SS27")
    assert board.status_code == 200 and "TEST-COAT-01" in board.text and "/media/" in board.text
    for page in ("/factories?season=SS27", "/pricing?season=SS27", "/documents", "/assistant", "/history?season=SS27", "/factory-import"):
        assert client.get(page).status_code == 200, page
    actions = [{"action": "set_customer_markup", "params": {"customer": "IDS", "markup": "35", "season": "SS27"}}]
    preview = client.post("/api/actions/preview", json={"actions": actions}).json()
    assert preview["ok"] and any(r["label"] == "Заказ IDS" for r in preview["diff"])
    assert db.scalar(select(CustomerPricingRule)) is None
    assert client.post("/api/actions/commit", json={"actions": actions, "reason": "test"}).json()["ok"]
    factories = client.get("/factories?season=SS27").text
    assert "10 530.00" in factories.replace(" ", " ").replace("\xa0", " ") or "10530.00" in factories
    generated = client.post("/documents/generate", data={"season": "SS27", "customer": "IDS"}, follow_redirects=False)
    assert generated.status_code == 303 and "error" not in generated.headers["location"]
    doc = db.scalar(select(GeneratedDocument).where(GeneratedDocument.file_format == "pdf"))
    download = client.get(f"/documents/{doc.id}/download")
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    bad = client.post("/api/actions/commit", json={"actions": [{"action": "set_customer_markup", "params": {"customer": "NOPE", "markup": "1"}}]})
    assert bad.status_code == 422


def test_assistant_api_proposal_then_commit(client, db, tmp_path):
    from test_factory_settlement import imported
    imported(db, tmp_path)
    reply = client.post("/api/agent/message", json={"conversation": "0123456789abcdef", "text": "IDS 35%", "screen": {"season": "SS27"}}).json()
    assert reply["proposal"]["status"] == "pending" and "propose_changes" in reply["tools"]
    assert client.post(f"/api/agent/proposals/{reply['proposal']['id']}/commit").json()["ok"]
    assert db.scalar(select(CustomerPricingRule)).markup == D("0.35")
    page = client.get("/assistant?c=0123456789abcdef")
    assert 'data-conversation="0123456789abcdef"' in page.text and 'id="copilot"' in page.text
    history = client.get("/api/agent/conversations/0123456789abcdef").json()
    assert [m["proposal"]["status"] for m in history["messages"] if m.get("proposal")] == ["committed"]
    assert client.post("/api/agent/message", json={"conversation": "bad", "text": "x"}).status_code == 422


def test_rejects_unsupported_and_oversized(client):
    response = client.post("/factory-import", files=[("files", ("notes.txt", b"hello", "text/plain"))])
    assert response.status_code == 422
