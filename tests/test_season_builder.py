from __future__ import annotations

from decimal import Decimal
import io
import re

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app import image_manager
from app.brand_catalog import BRAND_SUGGESTIONS
from app.main import app
from app.models import AuditLog, Brand, BrandSeasonRule, Customer, Order, OrderItem, Season
from app.services import dashboard_data


D = Decimal


def _session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    ss26 = Season(name="SS26")
    loro = Brand(name="Loro Piana Woman")
    aa = Customer(code="AA", name="Alina", country_code="IT")
    session.add_all([ss26, loro, aa])
    session.flush()
    session.add(BrandSeasonRule(
        season_id=ss26.id,
        brand_id=loro.id,
        markup_percentage=D("0.35"),
        initial_prepayment_percentage=D("0.50"),
        ready_balance_percentage=D("0.50"),
        transport_percentage=D("0.05"),
        transport_exempt_country_codes="CH",
        expected_receipt_days=14,
    ))
    session.commit()
    return session


def _empty_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _valid_form() -> dict[str, str]:
    return {
        "season_name": "FW26",
        "primary_customer_code": "AA",
        "primary_customer_name": "Alina",
        "primary_country_code": "IT",
        "additional_customers": "SV | Sofia | CH",
        "brand_rows": "Loro Piana Woman | 35\nBalenciaga Main | 30",
        "product_rows": (
            "Brand\tCustomer\tArticle\tProduct\tColor\tSize\tQuantity\tWholesale Price\tMarkup %\n"
            "Loro Piana Woman\tAA\tFAQ9434\tCoat\tNavy\t40\t1\t1028\t35\n"
            "Balenciaga Main\tAA\tBAG1\tBag\tBlack\tOne Size\t1\t400\t30"
        ),
        "copy_rules_from": "SS26",
        "default_markup": "35",
        "initial_percentage": "50",
        "ready_percentage": "50",
        "transport_percentage": "5",
        "exempt_countries": "CH",
        "receipt_days": "14",
    }


def test_preview_is_read_only_then_creation_is_atomic_and_calculated() -> None:
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            preview = client.post("/seasons/preview", data=_valid_form())
            assert preview.status_code == 200
            assert "Preview passed" in preview.text
            assert "1 907.80" in preview.text
            assert "953.90" in preview.text
            assert session.scalar(select(Season).where(Season.name == "FW26")) is None

            created = client.post("/seasons", data=_valid_form(), follow_redirects=False)
            assert created.status_code == 303
            assert created.headers["location"] == "/seasons/FW26?created=1"

            workspace = client.get("/seasons/FW26")
            assert workspace.status_code == 200
            assert "FW26 seasonal table" in workspace.text
    finally:
        app.dependency_overrides.clear()

    season = session.scalar(select(Season).where(Season.name == "FW26"))
    assert season is not None
    orders = session.scalars(select(Order).where(Order.season_id == season.id)).all()
    items = session.scalars(select(OrderItem).join(Order).where(Order.season_id == season.id)).all()
    rules = session.scalars(select(BrandSeasonRule).where(BrandSeasonRule.season_id == season.id)).all()
    assert len(orders) == 2 and len(items) == 2 and len(rules) == 2
    assert dashboard_data(session, "FW26").totals.final == D("1907.80")
    assert dashboard_data(session, "FW26").totals.required_prepayment == D("953.90")
    assert session.scalar(select(AuditLog).where(AuditLog.entity_type == "Season", AuditLog.entity_id == str(season.id))) is not None


def test_invalid_or_duplicate_rows_never_create_a_partial_season() -> None:
    session = _session()
    form = _valid_form()
    form["product_rows"] = (
        "Loro Piana Woman\tZZ\tFAQ9434\tCoat\tNavy\t40\t1\t1028\t35\n"
        "Loro Piana Woman\tAA\tFAQ9434\tCoat\tNavy\t40\t1\t1028\t35\n"
        "Loro Piana Woman\tAA\tFAQ9434\tCoat\tNavy\t40\t1\t1028\t35"
    )

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            preview = client.post("/seasons/preview", data=form)
            assert preview.status_code == 422
            assert "customer ZZ is not defined" in preview.text
            assert "duplicate allocation" in preview.text
            create = client.post("/seasons", data=form)
            assert create.status_code == 422
    finally:
        app.dependency_overrides.clear()

    assert session.scalar(select(Season).where(Season.name == "FW26")) is None


def test_ai_intake_schema_collects_facts_but_delegates_money_to_decimal_engine() -> None:
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.get("/api/season-intake/schema")
        assert response.status_code == 200
        payload = response.json()
        assert payload["required"] == ["season_name", "primary_customer_code", "brands"]
        assert "deterministic Decimal" in payload["calculation_policy"]
        assert "final_total" in payload["derived_fields"]
    finally:
        app.dependency_overrides.clear()


def test_quick_create_starts_new_season_and_redirects_to_first_product_form() -> None:
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/seasons/quick-create",
                data={
                    "season_name": "Resort 27",
                    "brand_name": "New Brand",
                    "customer_name": "Maria Buyer",
                    "country_code": "IT",
                    "default_markup": "30",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert response.headers["location"].endswith("?season_created=1#manual-entry")

            season = session.scalar(select(Season).where(Season.name == "RESORT 27"))
            assert season is not None
            order = session.scalar(select(Order).where(Order.season_id == season.id))
            assert order is not None
            assert session.scalar(select(Customer).where(Customer.name == "Maria Buyer")).code == "MB"
            detail = client.get(response.headers["location"])
            assert detail.status_code == 200
            assert "RESORT 27 and New Brand are ready" in detail.text
            assert dashboard_data(session).season.name == "RESORT 27"
    finally:
        app.dependency_overrides.clear()


def test_brand_catalog_is_large_searchable_and_does_not_restrict_custom_brands() -> None:
    assert len(BRAND_SUGGESTIONS) >= 150
    assert {"Balenciaga", "Chanel", "Loro Piana", "Prada", "The Row"}.issubset(BRAND_SUGGESTIONS)
    session = _empty_session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            page = client.get("/manual-entry")
            assert page.status_code == 200
            assert 'list="brand-catalog"' in page.text
            assert page.text.count('<option value="') >= 150
            created = client.post(
                "/seasons/quick-create",
                data={"season_name": "SS28", "brand_name": "My Custom Atelier", "customer_name": "Test Customer"},
                follow_redirects=False,
            )
            assert created.status_code == 303
            assert session.scalar(select(Brand).where(Brand.name == "My Custom Atelier")) is not None
    finally:
        app.dependency_overrides.clear()


def test_quick_create_shows_clear_error_for_existing_season() -> None:
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                "/seasons/quick-create",
                data={"season_name": "SS26", "brand_name": "Brand", "customer_name": "Another Customer"},
            )
            assert response.status_code == 422
            assert "Season SS26 already exists" in response.text
            assert "Please check this" in response.text
    finally:
        app.dependency_overrides.clear()


def test_ai_intake_empty_state_can_create_first_season_and_return_to_ai() -> None:
    session = _empty_session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            empty_page = client.get("/ai-intake")
            assert empty_page.status_code == 200
            assert "Start with the season" in empty_page.text
            assert 'name="next_path" value="/ai-intake"' in empty_page.text
            assert 'name="brand_name"' in empty_page.text
            assert 'name="customer_name"' in empty_page.text
            assert "Create drafts" not in empty_page.text

            created = client.post(
                "/seasons/quick-create",
                data={
                    "season_name": "FW27",
                    "next_path": "/ai-intake",
                },
                follow_redirects=False,
            )
            assert created.status_code == 303
            assert created.headers["location"] == "/ai-intake?season_created=FW27"
            ai_page = client.get(created.headers["location"])
            assert ai_page.status_code == 200
            assert "FW27 is ready" in ai_page.text
            assert "Which season is this for?" in ai_page.text
            assert "Create drafts" in ai_page.text
            assert session.scalar(select(Brand)) is None
            assert session.scalar(select(Customer)) is None
            assert session.scalar(select(Order)) is None
    finally:
        app.dependency_overrides.clear()


def _png_bytes(color: str = "navy") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (80, 100), color=color).save(output, format="PNG")
    return output.getvalue()


def test_bulk_image_upload_matches_article_previews_and_persists_after_creation(tmp_path, monkeypatch) -> None:
    staging = tmp_path / "staged"
    permanent = tmp_path / "images"
    monkeypatch.setattr(image_manager, "STAGING_ROOT", staging)
    monkeypatch.setattr(image_manager, "PERMANENT_ROOT", permanent)
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            preview = client.post(
                "/seasons/preview",
                data=_valid_form(),
                files=[("images", ("FAQ9434.png", _png_bytes(), "image/png"))],
            )
            assert preview.status_code == 200
            assert "FAQ9434.png" in preview.text
            assert "no renaming required" in preview.text
            token_match = re.search(r'name="staged_image_token" value="([A-Za-z0-9_-]+)"', preview.text)
            assert token_match is not None
            token = token_match.group(1)
            assert (staging / token / "manifest.json").is_file()
            assert session.scalar(select(Season).where(Season.name == "FW26")) is None

            create_data = _valid_form() | {"staged_image_token": token}
            created = client.post("/seasons", data=create_data, follow_redirects=False)
            assert created.status_code == 303
            item = session.scalar(select(OrderItem).where(OrderItem.article == "FAQ9434"))
            assert item.image_path and item.image_source_filename == "FAQ9434.png"
            assert item.image_match_method == "exact_article_filename"
            assert (permanent / item.image_path).is_file()
            assert not (staging / token).exists()

            order_page = client.get(f"/orders/{item.order_id}")
            assert order_page.status_code == 200
            assert "FAQ9434.png" in order_page.text
    finally:
        app.dependency_overrides.clear()


def test_arbitrary_filename_is_suggested_user_assigns_it_and_library_is_searchable(tmp_path, monkeypatch) -> None:
    staging = tmp_path / "staged"
    permanent = tmp_path / "images"
    monkeypatch.setattr(image_manager, "STAGING_ROOT", staging)
    monkeypatch.setattr(image_manager, "PERMANENT_ROOT", permanent)
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            blocked = client.post(
                "/seasons/preview",
                data=_valid_form(),
                files=[("images", ("navy-coat-front.png", _png_bytes("red"), "image/png"))],
            )
            assert blocked.status_code == 422
            assert "Choose a product for image navy-coat-front.png" in blocked.text
            assert "Suggested · Loro Piana Woman · FAQ9434 · Coat · Navy · 40" in blocked.text
            token_match = re.search(r'name="staged_image_token" value="([A-Za-z0-9_-]+)"', blocked.text)
            stored_match = re.search(r'class="image-product-select" data-image="([^"]+)"', blocked.text)
            assert token_match is not None and stored_match is not None
            token = token_match.group(1)
            stored_filename = stored_match.group(1)
            assert session.scalar(select(Season).where(Season.name == "FW26")) is None

            assigned_data = _valid_form() | {
                "staged_image_token": token,
                "image_assignments_json": f'{{"{stored_filename}": 2}}',
            }
            preview = client.post("/seasons/preview", data=assigned_data)
            assert preview.status_code == 200
            assert "Preview passed" in preview.text
            assert 'option value="2"' in preview.text

            created = client.post("/seasons", data=assigned_data, follow_redirects=False)
            assert created.status_code == 303
            assigned_item = session.scalar(select(OrderItem).where(OrderItem.article == "FAQ9434"))
            assert assigned_item.image_source_filename == "navy-coat-front.png"
            assert assigned_item.image_match_method == "user_selected_product"

            library = client.get("/product-images?q=navy+coat")
            assert library.status_code == 200
            assert "FAQ9434" in library.text
            assert "navy-coat-front.png" in library.text
    finally:
        app.dependency_overrides.clear()


def test_manual_upload_is_audited_and_searchable(tmp_path, monkeypatch) -> None:
    staging = tmp_path / "staged"
    permanent = tmp_path / "images"
    monkeypatch.setattr(image_manager, "STAGING_ROOT", staging)
    monkeypatch.setattr(image_manager, "PERMANENT_ROOT", permanent)
    session = _session()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:

            order = session.scalar(select(Order).where(Order.season_id == session.scalar(select(Season.id).where(Season.name == "SS26"))))
            if order is None:
                brand = session.scalar(select(Brand).where(Brand.name == "Loro Piana Woman"))
                season = session.scalar(select(Season).where(Season.name == "SS26"))
                order = Order(season_id=season.id, brand_id=brand.id)
                session.add(order)
                session.flush()
            customer = session.scalar(select(Customer).where(Customer.code == "AA"))
            item = OrderItem(order_id=order.id, customer_id=customer.id, article="MANUAL1", name="Manual image item", quantity=1, wholesale_unit_price=D("100"), markup_percentage=D("0.35"))
            session.add(item)
            session.commit()
            uploaded = client.post(
                f"/items/{item.id}/image",
                files={"image": ("supplier-look.png", _png_bytes("green"), "image/png")},
                follow_redirects=False,
            )
            assert uploaded.status_code == 303
            session.refresh(item)
            assert item.image_source_filename == "supplier-look.png"
            assert item.image_match_method == "manual_product_upload"
            assert (permanent / item.image_path).is_file()
            assert session.scalar(select(AuditLog).where(AuditLog.entity_type == "OrderItem", AuditLog.entity_id == str(item.id), AuditLog.field == "image_path")) is not None
            library = client.get("/product-images?q=manual1")
            assert library.status_code == 200
            assert "Manual image item" in library.text
    finally:
        app.dependency_overrides.clear()
