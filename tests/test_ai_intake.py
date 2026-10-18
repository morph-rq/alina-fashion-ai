from __future__ import annotations

from decimal import Decimal
import io

from fastapi.testclient import TestClient
import httpx
from openpyxl import Workbook
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import ai_intake, image_manager
from app.database import Base, get_db
from app.main import app
from app.models import AuditLog, Brand, BrandSeasonRule, Customer, Order, OrderItem, Season


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    season = Season(name="SS26")
    brand = Brand(name="Balenciaga Main")
    customer = Customer(code="AA", name="Alina", country_code="IT")
    session.add_all([season, brand, customer])
    session.flush()
    session.add(BrandSeasonRule(
        season_id=season.id, brand_id=brand.id, markup_percentage=Decimal("0.30"),
        initial_prepayment_percentage=Decimal("0.50"), ready_balance_percentage=Decimal("0.50"),
        transport_percentage=Decimal("0.05"), transport_exempt_country_codes="CH", expected_receipt_days=14,
    ))
    order = Order(season_id=season.id, brand_id=brand.id, source_sheet=brand.name)
    session.add(order)
    session.flush()
    session.add(OrderItem(
        order_id=order.id, customer_id=customer.id, article="868759-2ACKI-1000", name="Clicca belt 25",
        color="black", size="85", quantity=1, wholesale_unit_price=Decimal("238"), markup_percentage=Decimal("0.30"),
    ))
    session.commit()
    return session, order


def test_customer_code_uses_first_and_last_name_initials() -> None:
    assert ai_intake.suggested_customer_code("Maria Ala") == "MA"
    assert ai_intake.suggested_customer_code("New AI Customer") == "NC"
    assert ai_intake.suggested_customer_code("Alina") == "AL"
    assert ai_intake.suggested_customer_code("Мария Иванова") == "МИ"


def test_ai_review_matches_catalog_and_never_infers_size(monkeypatch) -> None:
    session, _order = _session()
    facts = ai_intake.IntakeFacts(
        customer_name="Maria Ala", customer_phone="839128398", brand="Balenciaga", product_name="Clicca belt 25",
        height_cm=167, weight_kg=50, stated_price="238", destination_country="Kazakhstan", destination_country_code="KZ",
        requested_delivery_date="2026-12-12", logistics_preference="suitcase", contact_instructions="Contact when PDF is sent",
        image_search_query="Balenciaga Clicca belt 25", source_attachment_names=["clicca.png"],
        missing_or_ambiguous=["Size must be confirmed", "Confirm price meaning"],
    )
    monkeypatch.setattr(
        ai_intake,
        "analyze_submission",
        lambda _message, _attachments: ai_intake.IntakeBundle(orders=[facts], source_summary="One typed order"),
    )
    monkeypatch.setattr(ai_intake, "search_product_images", lambda _query: (_ for _ in ()).throw(AssertionError("web search must be skipped when an uploaded photo is associated")))

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        image_buffer = io.BytesIO()
        Image.new("RGB", (40, 40), "black").save(image_buffer, format="PNG")
        with TestClient(app) as client:
            response = client.post(
                "/ai-intake/analyze",
                data={"season_name": "SS26", "message": "Maria ordered a belt"},
                files={"attachments": ("clicca.png", image_buffer.getvalue(), "image/png")},
            )
        assert response.status_code == 200
        assert "Clicca belt 25" in response.text
        assert "Size must be confirmed" in response.text
        assert 'name="size" value=""' in response.text
        assert "839128398" in response.text
        assert 'name="customer_code" value="MA" readonly' in response.text
        assert "Automatic from the first and last name" in response.text
        assert "clicca.png" in response.text
        assert "Check saved source" in response.text
        assert "ai-draft-table" in response.text
        assert "<img " not in response.text
        assert 'name="confirmation_token"' in response.text
        assert 'name="source_bundle_token"' in response.text
        assert "Save all reviewed rows" in response.text
        assert "Save row" in response.text
        assert 'name="confirmation"' not in response.text
    finally:
        app.dependency_overrides.clear()


def test_ai_intake_page_has_voice_files_without_processing_consent_gate() -> None:
    session, _order = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/ai-intake")
        assert response.status_code == 200
        assert 'id="start-voice-note"' in response.text
        assert "window.SpeechRecognition || window.webkitSpeechRecognition" in response.text
        assert "navigator.language || document.documentElement.lang" in response.text
        assert "Dictation needs the secure https website link" in response.text
        assert "iPhone|iPad|iPod" in response.text
        assert 'name="attachments"' in response.text
        assert 'enctype="multipart/form-data"' in response.text
        assert 'name="ai_consent"' not in response.text
        assert "processed by the configured AI service" not in response.text
        assert "Which season is this for?" in response.text
        assert 'aria-label="Choose season"' in response.text
    finally:
        app.dependency_overrides.clear()


def test_manual_entry_hides_photos_but_retains_image_api(monkeypatch) -> None:
    session, order = _session()
    captured = {}

    def fake_search(query, limit=5):
        captured["query"] = query
        captured["limit"] = limit
        return [ai_intake.ImageCandidate(
            title="Balenciaga black Clicca belt",
            image_url="https://img.example/belt.jpg",
            thumbnail_url="https://img.example/thumb.jpg",
            context_url="https://shop.example/belt",
        )]

    monkeypatch.setattr(ai_intake, "search_product_images", fake_search)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get(f"/orders/{order.id}")
            assert page.status_code == 200
            assert "Optional product image" not in page.text
            assert 'class="image-cell"' not in page.text
            assert 'enctype="multipart/form-data"' in page.text
            response = client.get(
                f"/api/orders/{order.id}/image-suggestions",
                params={"product_name": "Clicca belt", "article": "868759", "color": "black"},
            )
            assert response.status_code == 200
            assert response.json()["candidates"][0]["title"] == "Balenciaga black Clicca belt"
            assert captured["query"] == "Balenciaga Main Clicca belt 868759 black"
            assert captured["limit"] == 8
    finally:
        app.dependency_overrides.clear()


def test_manual_product_can_upload_image_during_creation(tmp_path, monkeypatch) -> None:
    session, order = _session()
    permanent = tmp_path / "images"
    monkeypatch.setattr(image_manager, "PERMANENT_ROOT", permanent)
    customer = session.scalar(select(Customer).where(Customer.code == "AA"))
    image_buffer = io.BytesIO()
    Image.new("RGB", (60, 80), "navy").save(image_buffer, format="PNG")

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/orders/{order.id}/items",
                data={
                    "customer_id": str(customer.id), "name": "Manual bag", "quantity": "1",
                    "wholesale_unit_price": "100", "status": "Ordered",
                },
                files={"image": ("manual-bag.png", image_buffer.getvalue(), "image/png")},
                follow_redirects=False,
            )
            assert response.status_code == 303
    finally:
        app.dependency_overrides.clear()

    item = session.scalar(select(OrderItem).where(OrderItem.name == "Manual bag"))
    assert item.image_path and item.image_match_method == "manual_order_upload"
    assert (permanent / item.image_path).is_file()


def test_ai_intake_analyzes_without_processing_consent_checkbox(monkeypatch) -> None:
    session, _order = _session()
    called = False

    def analyze(_message, _attachments):
        nonlocal called
        called = True
        return ai_intake.IntakeBundle(orders=[ai_intake.IntakeFacts(
            customer_name="Maria", brand="Balenciaga Main", product_name="Belt",
            explicit_size="80", stated_price="100", price_type="wholesale",
        )])

    monkeypatch.setattr(ai_intake, "analyze_submission", analyze)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post("/ai-intake/analyze", data={"season_name": "SS26", "message": "Maria ordered a belt"})
        assert response.status_code == 200
        assert "Maria" in response.text
        assert called is True
    finally:
        app.dependency_overrides.clear()


def test_brand_table_suggestion_is_confident_or_requires_review() -> None:
    session, order = _session()
    orders = [order]
    exact = ai_intake.suggest_order(orders, ai_intake.IntakeFacts(brand="Balenciaga Main"), [])
    assert exact.order.id == order.id

    second_brand = Brand(name="Balenciaga Couture")
    session.add(second_brand)
    session.flush()
    second_order = Order(season_id=order.season_id, brand_id=second_brand.id, source_sheet=second_brand.name)
    session.add(second_order)
    session.commit()
    ambiguous = ai_intake.suggest_order([order, second_order], ai_intake.IntakeFacts(brand="Balenciaga"), [])
    assert ambiguous.order is None
    assert ambiguous.reason == "brand match is ambiguous"


def test_multimodal_submission_sends_inline_photo_and_returns_separate_drafts(monkeypatch) -> None:
    captured = {}
    answer = ai_intake.IntakeBundle(
        orders=[
            ai_intake.IntakeFacts(customer_name="Maria", brand="Balenciaga", product_name="Belt", explicit_size="80"),
            ai_intake.IntakeFacts(customer_name="Alina", brand="Loro Piana", product_name="Coat", explicit_size="40"),
        ],
        follow_up_questions=["Confirm both price meanings"],
        source_summary="Two customer allocations detected.",
    ).model_dump_json()

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"candidates": [{"content": {"parts": [{"text": answer}]}}]})

    image_buffer = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(image_buffer, format="PNG")
    monkeypatch.setattr(ai_intake.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_intake.httpx, "post", fake_post)

    bundle = ai_intake.analyze_submission("Orders are shown in the photo", [("orders.png", "image/png", image_buffer.getvalue())])

    assert [order.customer_name for order in bundle.orders] == ["Maria", "Alina"]
    parts = captured["payload"]["contents"][0]["parts"]
    assert any(part.get("inlineData", {}).get("mimeType") == "image/png" for part in parts)
    assert "orders.png" in bundle.source_summary


def test_spreadsheet_attachment_is_extracted_locally_before_gemini() -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Balenciaga"
    sheet.append(["Customer", "Product", "Price"])
    sheet.append(["AA", "Clicca belt 25", 238])
    buffer = io.BytesIO()
    workbook.save(buffer)

    parts, names = ai_intake._bundle_parts("", [("orders.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", buffer.getvalue())])

    combined_text = "\n".join(part.get("text", "") for part in parts)
    assert names == ["orders.xlsx"]
    assert "Sheet: Balenciaga" in combined_text
    assert "AA\tClicca belt 25\t238" in combined_text


def test_gemini_transient_503_retries_then_returns_structured_facts(monkeypatch) -> None:
    calls = []
    payload = ai_intake.IntakeFacts(customer_name="Maria", product_name="Clicca belt 25", stated_price="238").model_dump_json()

    def fake_post(url, **_kwargs):
        calls.append(url)
        request = httpx.Request("POST", url)
        if len(calls) == 1:
            return httpx.Response(503, request=request, json={"error": {"message": "busy"}})
        return httpx.Response(200, request=request, json={"candidates": [{"content": {"parts": [{"text": payload}]}}]})

    monkeypatch.setattr(ai_intake.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_intake.settings, "gemini_model", "gemini-3.7-flash")
    monkeypatch.setattr(ai_intake.settings, "gemini_fallback_models", "gemini-3.6-flash,gemini-2.5-flash")
    monkeypatch.setattr(ai_intake.httpx, "post", fake_post)
    monkeypatch.setattr(ai_intake.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ai_intake.random, "uniform", lambda _start, _end: 0)

    facts = ai_intake.analyze_message("Maria ordered a belt")
    assert facts.customer_name == "Maria"
    assert len(calls) == 2
    assert "gemini-3.7-flash" in calls[0]
    assert "gemini-3.6-flash" in calls[1]
    assert any("Size must be confirmed" in warning for warning in facts.missing_or_ambiguous)
    assert any("price" in warning.casefold() for warning in facts.missing_or_ambiguous)


def test_searxng_results_are_filtered_and_gemini_recommends_exact_image(monkeypatch) -> None:
    image_buffer = io.BytesIO()
    Image.new("RGB", (40, 40), "black").save(image_buffer, format="PNG")

    def fake_get(url, **_kwargs):
        request = httpx.Request("GET", url)
        if url.endswith("/search"):
            return httpx.Response(200, request=request, json={"results": [
                {"title": "Clicca Belt 25 - Balenciaga - Leather - Black", "url": "https://shop.example/product", "img_src": "https://img.example/belt.jpg", "thumbnail_src": "/image_proxy?url=https%3A%2F%2Fimg.example%2Fthumb.png&h=test", "engine": "google cse images"},
                {"title": "briefcase conveyor belt icon", "url": "https://icons.example", "img_src": "https://icons.example/icon.svg", "engine": "lucide"},
            ]})
        return httpx.Response(200, request=request, content=image_buffer.getvalue(), headers={"content-type": "image/png"})

    approval = ai_intake.ImageApproval(recommended_index=0, confidence=0.96, reason="Brand, model name, belt form and black color agree")

    def fake_post(url, **_kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"candidates": [{"content": {"parts": [{"text": approval.model_dump_json()}]}}]})

    monkeypatch.setattr(ai_intake.settings, "searxng_url", "http://127.0.0.1:8888")
    monkeypatch.setattr(ai_intake.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(ai_intake.httpx, "get", fake_get)
    monkeypatch.setattr(ai_intake.httpx, "post", fake_post)
    monkeypatch.setattr(ai_intake, "searxng_thumbnail_source_is_allowed", lambda _url: True)

    candidates = ai_intake.search_product_images("Balenciaga Clicca belt 25")
    assert candidates[0].title.startswith("Clicca Belt 25")
    assert candidates[0].thumbnail_url.startswith("/api/image-thumbnails/")
    assert "127.0.0.1" not in candidates[0].thumbnail_url
    candidates, warning = ai_intake.approve_image_candidates(
        ai_intake.IntakeFacts(brand="Balenciaga", product_name="Clicca belt 25", color="black"), candidates,
    )
    assert warning is None
    assert candidates[0].ai_recommended is True
    assert candidates[0].ai_confidence == 0.96

    with TestClient(app) as client:
        thumbnail = client.get(candidates[0].thumbnail_url)
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"].startswith("image/png")
    assert thumbnail.content == image_buffer.getvalue()


def test_confirmed_ai_draft_persists_source_facts_and_audit() -> None:
    session, order = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post("/ai-intake/confirm", data={
                "order_id": str(order.id), "customer_code": "MA", "customer_name": "Maria Ala", "customer_phone": "839128398",
                "country_code": "KZ", "article": "868759-2ACKI-1000", "product_name": "Clicca belt 25", "color": "black",
                "size": "80", "quantity": "1", "stated_price": "238", "price_type": "wholesale",
                "requested_delivery_date": "2026-12-12", "logistics_preference": "suitcase",
                "customer_requirements": "Height 167 cm; weight 50 kg; very lean", "source_text": "Original customer message",
                "contact_on_pdf_send": "true",
            }, follow_redirects=False)
        assert response.status_code == 303
    finally:
        app.dependency_overrides.clear()

    customer = session.scalar(select(Customer).where(Customer.code == "MA"))
    item = session.scalar(select(OrderItem).where(OrderItem.customer_id == customer.id))
    assert customer.phone == "839128398" and customer.country_code == "KZ"
    assert item.wholesale_unit_price == Decimal("238.00")
    assert item.requested_delivery_date.isoformat() == "2026-12-12"
    assert item.logistics_preference == "suitcase" and item.contact_on_pdf_send is True
    assert item.ai_source_text == "Original customer message"
    assert session.scalar(select(AuditLog).where(AuditLog.entity_type == "OrderItem", AuditLog.entity_id == str(item.id))) is not None


def test_confirmed_draft_can_save_in_place_as_json() -> None:
    session, order = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post("/ai-intake/confirm", data={
                "order_id": str(order.id), "customer_code": "FAST", "customer_name": "Fast Entry",
                "product_name": "Test bag", "size": "One Size", "quantity": "1", "stated_price": "100",
                "price_type": "wholesale", "source_text": "Synthetic test",
                "save_mode": "batch",
            }, headers={"Accept": "application/json"})
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert response.json()["order_id"] == order.id
        assert session.scalar(select(OrderItem).where(OrderItem.name == "Test bag")) is not None
    finally:
        app.dependency_overrides.clear()


def test_confirmed_ai_draft_creates_missing_brand_table_from_extracted_brand() -> None:
    session, _existing_order = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post("/ai-intake/confirm", data={
                "order_id": "", "season_name": "SS26", "brand_name": "New AI Brand", "new_brand_markup": "40",
                "customer_code": "NAD", "customer_name": "New AI Customer",
                "product_name": "AI coat", "size": "40", "quantity": "1", "stated_price": "100",
                "price_type": "wholesale", "source_text": "New AI Customer ordered a New AI Brand coat",
            }, follow_redirects=False)
        assert response.status_code == 303
    finally:
        app.dependency_overrides.clear()

    brand = session.scalar(select(Brand).where(Brand.name == "New AI Brand"))
    order = session.scalar(select(Order).where(Order.brand_id == brand.id))
    rule = session.scalar(select(BrandSeasonRule).where(BrandSeasonRule.brand_id == brand.id))
    item = session.scalar(select(OrderItem).where(OrderItem.name == "AI coat"))
    customer = session.scalar(select(Customer).where(Customer.name == "New AI Customer"))
    assert order is not None
    assert customer.code == "NC"
    assert rule.markup_percentage == Decimal("0.4000")
    assert item.order_id == order.id
    assert item.markup_percentage == Decimal("0.4000")
