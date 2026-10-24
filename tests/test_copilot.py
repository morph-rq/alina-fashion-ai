"""Fashion AI bar: screen context, navigation, dropped files (compare → confirm → merge), exact workbook
inspection, deposit table, multi-step requests and the page shell. Synthetic fixture only; no network."""
from __future__ import annotations

import time
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import select

import factory_fixture
from test_factory_settlement import db, imported  # noqa: F401
from app import agent, customer_documents, factory_routes, revision, workbook_inspect as wi
from app.database import get_db
from app.factory_import import analyze_workbook
from app.main import app
from app.models import AgentProposal, CustomerPricingRule, ImportBatch, OrderItem

KNOWN = {"AA", "IDS", "SS", "ALINA"}


@pytest.fixture
def client(db, tmp_path, monkeypatch):  # noqa: F811
    real = customer_documents.generate
    monkeypatch.setattr(customer_documents, "generate", lambda *a, **k: real(*a, **{**k, "root": tmp_path}))
    monkeypatch.setattr(factory_routes, "_known_tokens", lambda _db: KNOWN)
    app.dependency_overrides[get_db] = lambda: (yield db)
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()


def revised(tmp_path):
    """The same order a week later: coat price changed, dress cancelled, a new jacket for SS."""
    path = factory_fixture.build(tmp_path / "Dior cruise SS27 v2.xlsx")
    book = load_workbook(path)
    sheet = book["Dior cruise"]
    sheet["F4"] = 2100                      # Coat test 2000 → 2100
    sheet["I7"] = "SS CANCELLED"            # Dress test cancelled
    sheet.insert_rows(10)
    sheet["C10"], sheet["D10"], sheet["E10"], sheet["F10"], sheet["G10"], sheet["H10"], sheet["I10"] = \
        "Jacket new", "TEST-JKT-07", 38, 900, 1, "=F10*G10", "SS"
    book.save(path)
    return path


def wait(client, token):
    for _ in range(200):
        state = client.get(f"/factory-import/{token}/status").json()["state"]
        if state not in ("queued", "analysing"):
            return state
        time.sleep(0.05)
    raise AssertionError("analysis did not finish")


# --- Screen context and navigation -------------------------------------------------------------

def test_navigation_and_upload_commands(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    assert agent.handle(db, "n1aaaaaaaaaaaaaa", "Открой фабрики").client == {"navigate": "/factories?season=SS27"}
    dior = agent.handle(db, "n1aaaaaaaaaaaaaa", "Открой Dior Cruise")
    assert dior.client == {"navigate": "/board?season=SS27&brand=Dior+cruise"} and dior.panel["selection"]["rows"]
    ids = agent.handle(db, "n1aaaaaaaaaaaaaa", "Покажи все заказы IDS")
    assert ids.client == {"navigate": "/board?season=SS27&brand=Dior+cruise&customer=IDS"}
    assert {r["customer"] for r in ids.panel["selection"]["rows"]} == {"IDS"}
    assert agent.handle(db, "n1aaaaaaaaaaaaaa", "Открой загрузку файла").client == {"open_upload": True}
    assert agent.handle(db, "n1aaaaaaaaaaaaaa", "Открой документы").client["navigate"].startswith("/documents")
    change = agent.handle(db, "n1aaaaaaaaaaaaaa", "Измени AA процент на 30")
    assert change.proposal and "AA 30%" in change.proposal["summary"]


def test_what_is_open_reads_structured_screen(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    coat = db.scalar(select(OrderItem).where(OrderItem.article == "TEST-COAT-01"))
    out = agent.handle(db, "n2aaaaaaaaaaaaaa", "Что сейчас открыто?", screen={
        "area": "workspace", "season": "SS27", "brand": "Dior cruise", "customer": "IDS", "selected_item_ids": [coat.id],
        "figures": [{"label": "Фабрике всего", "value": "€ 10530.00"}]})
    assert "Заказы → SS27 → Dior cruise → IDS" in out.reply
    assert "Coat test (IDS, р. 40)" in out.reply and "Фабрике всего: € 10530.00" in out.reply


def test_page_context_resolves_here_this_and_her(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    # The owner opened Dior cruise filtered to AA (a different page than the conversation knew): "здесь 35%" is AA there.
    out = agent.handle(db, "n3aaaaaaaaaaaaaa", "Поставь здесь 35%", screen={"area": "workspace", "season": "SS27", "brand": "Dior cruise",
                                                                         "customer": "AA", "fresh": True})
    actions = __import__("json").loads(db.scalar(select(AgentProposal)).actions_json)["actions"]
    assert out.proposal and actions == [{"action": "set_customer_markup", "params": {"customer": "AA", "markup": "35", "season": "SS27", "brand": "Dior cruise"}}]
    dress = db.scalar(select(OrderItem).where(OrderItem.article == "TEST-DRS-04"))
    out = agent.handle(db, "n4aaaaaaaaaaaaaa", "Вот это платье убери", screen={"season": "SS27", "selected_item_ids": [dress.id], "fresh": True})
    plan = __import__("json").loads(db.get(AgentProposal, 2).actions_json)
    assert plan["actions"][0]["params"]["item_ids"] == [dress.id] and plan["actions"][0]["params"]["status"] == "Cancelled"
    price = agent.handle(db, "n4aaaaaaaaaaaaaa", "А это сколько стоит?", screen={"season": "SS27", "selected_item_ids": [dress.id]})
    assert "Dress test" in price.reply and "€1 000.00" in price.reply


def test_same_again_repeats_the_last_step_for_a_new_customer(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    first = agent.handle(db, "f1aaaaaaaaaaaaaa", "Сделай Excel и PDF для AA")
    assert {d["customer"] for d in first.panel["documents"]} == {"AA"}
    again = agent.handle(db, "f1aaaaaaaaaaaaaa", "А теперь то же самое для IDS", screen={"area": "inbox", "season": "SS27", "fresh": True})
    assert sorted((d["customer"], d["format"]) for d in again.panel["documents"]) == [("IDS", "pdf"), ("IDS", "xlsx")]
    agent.handle(db, "f2aaaaaaaaaaaaaa", "Покажи AA в Dior cruise")
    agent.handle(db, "f2aaaaaaaaaaaaaa", "Поставь ей 30%")
    same = agent.handle(db, "f2aaaaaaaaaaaaaa", "Так же для SS")
    assert same.proposal and same.proposal["summary"].startswith("Наценка 30%: SS")


# --- Deposits from the verified engine ------------------------------------------------------------

def test_deposit_table_matches_settlement_engine(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    out = agent.handle(db, "n5aaaaaaaaaaaaaa", "Посчитай предоплаты")
    s = agent.t_settlement(db, {"season": "SS27"})
    table = {row[0]: row for row in out.panel["table"]["rows"]}
    assert set(table) == {"AA", "IDS", "SS"}                      # the owner is not a customer row
    for code, row in table.items():
        assert row[2] == f"€{D(s['customers'][code]['deposit_due']):,.2f}".replace(",", " ")
    figures = {f["label"]: f["value"] for f in out.panel["figures"]}
    assert figures["Предоплата фабрике"] == s["factory_deposit_required"]
    assert D(figures["Ваш взнос"]) == max(D(s["owner_net_deposit_planned"]), D(0))
    assert D(s["factory_deposit_required"]) - D(s["external_customer_deposits_expected"]) == D(s["owner_net_deposit_planned"])
    combined = agent.handle(db, "n6aaaaaaaaaaaaaa", "Открой все заказы Dior Cruise и покажи, сколько каждая девушка должна заплатить")
    assert combined.client == {"navigate": "/board?season=SS27&brand=Dior+cruise"} and combined.panel["table"]["rows"]


# --- Exact inspection of big workbooks ---------------------------------------------------------------

def test_workbook_index_is_exact_paged_and_searchable(tmp_path):
    path = factory_fixture.build(tmp_path / "fixture.xlsx")
    outline = wi.outline(path)
    sheet = next(s for s in outline["sheets"] if s["name"] == "Dior cruise")
    assert sheet["photos"] == 6 and sheet["rows"] == 12
    page = wi.rows(path, "dior cruise", 4, 5)
    coat = page["rows"][0]
    assert coat["row"] == 4 and coat.get("photo") and coat["cells"]["D"]["v"] == "TEST-COAT-01"
    assert coat["cells"]["H"]["f"] == "=F4*G4" and coat["cells"]["F"]["v"] == "2000"
    big = wi.rows(path, "Dior cruise", 1, 5000)
    assert big["truncated_to_page"] and big["end"] == wi.MAX_ROWS
    hits = wi.search(path, "test-scf")
    assert hits["total"] == 1 and hits["hits"][0]["cell"] == "D8" and hits["hits"][0]["row_values"]["I"] == "IDS, AA"
    assert wi.search(path, "=F4")["hits"][0]["formula"] == "=F4*G4"                     # formulas are searchable too
    with pytest.raises(wi.InspectError):
        wi.rows(path, "No such sheet")


def test_inspect_commands_through_the_assistant(db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    outline = agent.handle(db, "n7aaaaaaaaaaaaaa", "Какие листы в файле?")
    assert [r[0] for r in outline.panel["table"]["rows"]] == ["SS27", "Dior cruise"]
    rows = agent.handle(db, "n7aaaaaaaaaaaaaa", "Покажи строки 4-6 листа Dior cruise")
    assert rows.panel["table"]["rows"][0][0] == "4 📷" and "TEST-COAT-01" in rows.panel["table"]["rows"][0]
    found = agent.handle(db, "n7aaaaaaaaaaaaaa", "Найди TEST-SHT-06 в файле")
    assert "найдено 1" in found.reply and found.panel["table"]["rows"][0][1] == "D9"


# --- Dropped file: read → compare → confirm (button only for voice) → merged ---------------------------

def test_dropped_revision_is_compared_confirmed_and_merged(client, db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    conv = "d0aaaaaaaaaaaaaa"
    path = revised(tmp_path)
    token = client.post("/api/agent/attach", data={"conversation": conv}, files=[("files", (path.name, path.read_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))]).json()["tokens"][0]
    assert wait(client, token) == "ready"
    first = client.post("/api/agent/attachment", json={"conversation": conv, "token": token}).json()
    assert "Dior cruise" in first["reply"] and first["question"]["options"][0] == "Добавь к существующему и проверь изменения"
    assert first["panel"]["attachment"]["sheets"][0]["exists"] is True
    reply = client.post("/api/agent/message", json={"conversation": conv, "text": "Добавь к существующему и проверь изменения"}).json()
    rev = reply["panel"]["revision"]
    assert [r["label"].split(" ·")[0] for r in rev["new"]] == ["Jacket new"]
    assert [(r["label"].split(" ·")[0], r["changes"][0]["after"]) for r in rev["changed"]] == [("Coat test", "2100.00")]
    assert [r["label"].split(" ·")[0] for r in rev["cancelled"]] == ["Dress test"]
    assert reply["proposal"]["kind"] == "import" and reply["proposal"]["button_only"]
    assert "сезон" not in client.post("/api/agent/message", json={"conversation": conv, "text": "Оставь без клиента"}).json()["reply"].split("\n")[0]
    reply = client.post("/api/agent/message", json={"conversation": conv, "text": "Добавь к существующему и проверь изменения"}).json()
    spoken = client.post("/api/agent/message", json={"conversation": conv, "text": "Да, сохрани", "channel": "voice"}).json()
    assert "кнопк" in spoken["reply"] and db.scalar(select(OrderItem).where(OrderItem.article == "TEST-JKT-07")) is None
    done = client.post(f"/api/agent/proposals/{reply['proposal']['id']}/commit").json()
    assert done["ok"], done
    db.expire_all()
    assert db.scalar(select(OrderItem).where(OrderItem.article == "TEST-COAT-01")).wholesale_unit_price == D("2100")
    assert db.scalar(select(OrderItem).where(OrderItem.article == "TEST-DRS-04")).status == "Cancelled"
    jacket = db.scalar(select(OrderItem).where(OrderItem.article == "TEST-JKT-07"))
    assert jacket.customer.code == "SS" and jacket.order.brand.name == "Dior cruise"
    assert len(list(db.scalars(select(OrderItem).where(OrderItem.article == "TEST-SUIT-02")))) == 2     # nothing duplicated
    assert db.scalar(select(ImportBatch).where(ImportBatch.filename == path.name)) is not None
    # Same file again: nothing new.
    analysis = analyze_workbook(path, path.name, KNOWN)
    diff = revision.compare(db, analysis, "SS27")
    assert not diff["new"] and not diff["changed"] and not diff["cancelled"]


def test_multi_step_request_continues_after_the_import(client, db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    conv = "d1aaaaaaaaaaaaaa"
    path = revised(tmp_path)
    token = client.post("/api/agent/attach", data={"conversation": conv}, files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))]).json()["tokens"][0]
    wait(client, token)
    client.post("/api/agent/attachment", json={"conversation": conv, "token": token})
    text = "Я добавила новый Dior. Раздели заказы по девушкам, поставь IDS 35%, AA 30%, рассчитай все предоплаты и сделай для каждой Excel и PDF."
    first = client.post("/api/agent/message", json={"conversation": conv, "text": text}).json()
    assert first["proposal"]["kind"] == "import" and "продолжу" in first["reply"]
    done = client.post(f"/api/agent/proposals/{first['proposal']['id']}/commit").json()
    assert done["follow_up"] == text
    second = client.post("/api/agent/message", json={"conversation": conv, "text": done["follow_up"]}).json()
    assert second["proposal"]["kind"] == "change"
    assert "разделён по клиентам" in second["reply"] and "SS" in second["reply"]
    assert sorted(d["customer"] for d in second["proposal"]["documents"]) == ["AA", "IDS", "SS"]       # each girl, never the owner
    final = client.post(f"/api/agent/proposals/{second['proposal']['id']}/commit").json()
    assert final["ok"] and len(final["documents"]) == 6
    rules = {(r.customer.code, r.markup) for r in db.scalars(select(CustomerPricingRule))}
    assert rules == {("IDS", D("0.35")), ("AA", D("0.30"))}


def test_pdf_attachment_is_compared_by_article(client, db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    from reportlab.pdfgen import canvas
    pdf = tmp_path / "proforma.pdf"
    c = canvas.Canvas(str(pdf))
    c.drawString(72, 720, "Proforma TEST-COAT-01 qty 1")
    c.drawString(72, 700, "TEST-MISSING-99 qty 2")
    c.save()
    conv = "d2aaaaaaaaaaaaaa"
    token = client.post("/api/agent/attach", data={"conversation": conv}, files=[("files", ("proforma.pdf", pdf.read_bytes(), "application/pdf"))]).json()["tokens"][0]
    assert wait(client, token) == "reference"
    out = client.post("/api/agent/attachment", json={"conversation": conv, "token": token}).json()
    rows = {r[0]: r for r in out["panel"]["table"]["rows"]}
    assert rows["TEST-COAT-01"][1] == "Dior cruise" and rows["TEST-MISSING-99"][2] == "нет в заказах"


# --- Shell, history, hints, previews, screen sharing -----------------------------------------------------

def test_every_page_has_the_bar_and_history_api(client, db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    for url in ("/", "/board?season=SS27&brand=Dior+cruise", "/factories", "/pricing", "/documents", "/assistant", "/settings/voice"):
        page = client.get(url)
        assert page.status_code == 200, url
        assert 'id="copilot"' in page.text and "Спросите Fashion AI или перетащите файл" in page.text and 'id="drop-veil"' in page.text
        assert not __import__("re").search(r"sk_[0-9a-f]{16,}", page.text)      # no key material in any page
    board = client.get("/board?season=SS27&brand=Dior+cruise").text
    assert "data-fx-ctx=" in board and '"brand": "Dior cruise"' in board and '"season": "SS27"' in board
    client.post("/api/agent/message", json={"conversation": "a0aaaaaaaaaaaaaa", "text": "Открой фабрики"})
    listed = client.get("/api/agent/conversations").json()["conversations"]
    assert listed[0]["id"] == "a0aaaaaaaaaaaaaa" and listed[0]["title"] == "Открой фабрики"
    hints = client.post("/api/agent/hints", json={"screen": {"area": "factories", "season": "SS27"}}).json()["hints"]
    assert any("предоплата" in h["text"] for h in hints)


def test_generated_documents_preview_inline(client, db, tmp_path):  # noqa: F811
    imported(db, tmp_path)
    out = agent.handle(db, "p0aaaaaaaaaaaaaa", "Сделай Excel и PDF для IDS")
    docs = {d["format"]: d["id"] for d in out.panel["documents"]}
    pdf = client.get(f"/documents/{docs['pdf']}/view")
    assert pdf.headers["content-type"] == "application/pdf" and pdf.headers["content-disposition"].startswith("inline")
    html = client.get(f"/documents/{docs['xlsx']}/view").text
    assert "TEST-COAT-01" in html and "/image?part=xl/media/" in html and "TEST-SUIT-02" not in html
    image = client.get(html.split('src="')[1].split('"')[0])
    assert image.status_code == 200 and image.headers["content-type"].startswith("image/")


def test_screen_frame_needs_vision_ai_and_is_honest(client):
    tiny = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    out = client.post("/api/agent/screen-frame", json={"conversation": "e0aaaaaaaaaaaaaa", "image": tiny, "text": "Что это?"}).json()
    assert out["vision"] is False and "Claude" in out["reply"]
    assert client.post("/api/agent/screen-frame", json={"conversation": "e0aaaaaaaaaaaaaa", "image": "nope"}).status_code == 422


def test_voice_key_terms_respect_the_scribe_limit(db):  # noqa: F811
    from app import voice
    from app.models import Brand
    db.add(Brand(name="Brunello Cucinelli women"))
    db.commit()
    terms = voice.keyterms(db)
    assert all(len(t) <= 20 for t in terms) and {"Brunello", "Cucinelli", "women"} <= set(terms)
