"""The simplified navigation: four areas, combined Workspace pages, old URLs and safe return links."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import Brand, BrandSeasonRule, Customer, Order, OrderItem, Payment, Season
from app.navigation import nav_area, safe_next, workspace_view


@pytest.fixture()
def nav_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        older, newest = Season(name="SS26"), Season(name="FW26")
        session.add(older)
        session.flush()
        session.add(newest)
        north, verre = Brand(name="Atelier Nord", default_markup_percentage=Decimal("0.30")), Brand(name="Maison Verre", default_markup_percentage=Decimal("0.30"))
        customer = Customer(code="CR", name="Clara Rossi")
        session.add_all([north, verre, customer])
        session.flush()
        for season in (older, newest):
            for brand in (north, verre):
                session.add(BrandSeasonRule(
                    season_id=season.id, brand_id=brand.id, markup_percentage=Decimal("0.30"),
                    initial_prepayment_percentage=Decimal("0.50"), ready_balance_percentage=Decimal("0.50"),
                    transport_percentage=Decimal("0.05"), special_cost=Decimal("0"), expected_receipt_days=14,
                ))
        north_order = Order(season_id=newest.id, brand_id=north.id, status="Ordered")
        verre_order = Order(season_id=newest.id, brand_id=verre.id, status="Ordered")
        session.add_all([north_order, verre_order])
        session.flush()
        session.add_all([
            OrderItem(order_id=north_order.id, customer_id=customer.id, article="AN-COAT", name="Wool wrap coat",
                      quantity=1, wholesale_unit_price=Decimal("100"), markup_percentage=Decimal("0.30")),
            OrderItem(order_id=verre_order.id, customer_id=customer.id, article="MV-SCARF", name="Silk scarf",
                      quantity=1, wholesale_unit_price=Decimal("50"), markup_percentage=Decimal("0.30")),
        ])
        session.commit()
        yield session


@pytest.fixture()
def nav_client(nav_session: Session):
    def override_db():
        yield nav_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def ids(session: Session):
    customer = session.scalar(select(Customer))
    coat = session.scalar(select(OrderItem).where(OrderItem.article == "AN-COAT"))
    scarf = session.scalar(select(OrderItem).where(OrderItem.article == "MV-SCARF"))
    north = session.scalar(select(Brand).where(Brand.name == "Atelier Nord"))
    return customer, coat, scarf, north


def location(response) -> tuple[str, dict[str, list[str]], str]:
    parts = urlsplit(response.headers["location"])
    return parts.path, parse_qs(parts.query), parts.fragment


def test_shell_offers_four_areas_and_settings_without_more_tools(nav_client: TestClient) -> None:
    page = nav_client.get("/")
    assert page.status_code == 200
    nav = page.text.split('class="shell-nav"', 1)[1].split("</nav>", 1)[0]
    for href in ('href="/"', 'href="/worksheet"', 'href="/inbox"', 'href="/customers"'):
        assert href in nav
    assert nav.count("<a ") == 4
    assert 'href="/settings"' in page.text and "data-search-open" in page.text and 'id="ai-help"' in page.text
    assert "More tools" not in page.text


@pytest.mark.parametrize("path,area", [
    ("/", "overview"), ("/validation", "overview"), ("/worksheet", "workspace"), ("/logistics", "workspace"),
    ("/money", "workspace"), ("/seasons/FW26", "workspace"), ("/inbox", "inbox"), ("/intake/sessions/4", "inbox"),
    ("/source-reviews", "inbox"), ("/customers/3", "customers"), ("/reports", "customers"), ("/settings", "settings"),
    ("/rules", "settings"), ("/recovery", "settings"), ("/connections", "settings"), ("/audit", "settings"),
])
def test_every_page_belongs_to_one_area(path: str, area: str) -> None:
    assert nav_area(path) == area


def test_workspace_views_follow_the_page() -> None:
    assert workspace_view("/worksheet/items/4") == "products"
    assert workspace_view("/orders/2") == "brands"
    assert workspace_view("/logistics") == "logistics"
    assert workspace_view("/money") == "money"


@pytest.mark.parametrize("old,new_path,expected", [
    ("/orders?season=FW26&q=coat", "/worksheet", {"season": ["FW26"], "q": ["coat"]}),
    ("/payments?season=SS26", "/money", {"season": ["SS26"], "focus": ["payments"]}),
    ("/shipments?season=FW26", "/logistics", {"season": ["FW26"], "focus": ["shipments"]}),
    ("/ready-batches?season=FW26", "/logistics", {"season": ["FW26"], "focus": ["ready"]}),
    ("/groups", "/logistics", {"focus": ["groups"]}),
    ("/credits", "/money", {"focus": ["seller-credit"]}),
    ("/intake?channel=email", "/inbox", {"channel": ["email"]}),
])
def test_old_pages_redirect_and_keep_their_query(nav_client: TestClient, old: str, new_path: str, expected: dict) -> None:
    response = nav_client.get(old, follow_redirects=False)
    assert response.status_code in (301, 302, 303, 307, 308)
    path, query, _ = location(response)
    assert path == new_path
    for key, value in expected.items():
        assert query.get(key) == value
    assert nav_client.get(old).status_code == 200


def test_old_to_do_page_opens_next_actions(nav_client: TestClient) -> None:
    response = nav_client.get("/operations", follow_redirects=False)
    path, _, fragment = location(response)
    assert (path, fragment) == ("/", "next-actions")


def test_brands_opens_the_newest_season_table(nav_client: TestClient) -> None:
    response = nav_client.get("/brands", follow_redirects=False)
    assert location(response)[0] == "/seasons/FW26"


@pytest.mark.parametrize("path", ["/", "/worksheet", "/brands", "/logistics", "/logistics?season=SS26", "/money",
                                  "/money?season=SS26", "/inbox", "/customers", "/reports", "/settings", "/excel",
                                  "/manual-entry", "/validation", "/audit", "/rules", "/library", "/recovery",
                                  "/connections", "/source-reviews", "/import", "/product-images", "/ai-intake"])
def test_every_area_page_renders(nav_client: TestClient, path: str) -> None:
    assert nav_client.get(path).status_code == 200


@pytest.mark.parametrize("value", [
    "https://elsewhere.example/x", "//elsewhere.example/x", "/\\elsewhere.example", "javascript:alert(1)",
    "/money\r\nSet-Cookie: x=1", "money", "",
])
def test_safe_next_only_returns_to_this_site(value: str) -> None:
    assert safe_next(value, "/fallback") == "/fallback"


def test_safe_next_keeps_a_local_return_target() -> None:
    assert safe_next("/money?season=FW26&focus=payments&done=payment", "/payments") == "/money?season=FW26&focus=payments&done=payment"


def test_item_save_returns_to_the_filtered_worksheet(nav_client: TestClient, nav_session: Session) -> None:
    _, coat, _, north = ids(nav_session)
    back = f"/worksheet?season=FW26&brand={north.id}&state=process"
    response = nav_client.post(f"/worksheet/items/{coat.id}", data={"state": "done", "next": back}, follow_redirects=False)
    assert response.status_code == 303
    path, query, _ = location(response)
    assert path == "/worksheet"
    assert query["season"] == ["FW26"] and query["brand"] == [str(north.id)] and query["state"] == ["process"]
    assert query["saved"] == [str(coat.id)]
    nav_session.refresh(coat)
    assert coat.worksheet_status == "done"

    elsewhere = nav_client.post(f"/worksheet/items/{coat.id}", data={"state": "process", "next": "https://elsewhere.example/"}, follow_redirects=False)
    assert location(elsewhere)[0] == "/worksheet"


def test_item_page_offers_a_way_back_to_the_list(nav_client: TestClient, nav_session: Session) -> None:
    _, coat, _, _ = ids(nav_session)
    page = nav_client.get(f"/worksheet/items/{coat.id}", params={"next": "/worksheet?season=FW26&q=coat"})
    assert page.status_code == 200
    assert 'name="next" value="/worksheet?season=FW26&amp;q=coat"' in page.text
    outside = nav_client.get(f"/worksheet/items/{coat.id}", params={"next": "/money"})
    assert 'name="next" value="/money"' not in outside.text


def test_bulk_change_returns_with_the_count(nav_client: TestClient, nav_session: Session) -> None:
    _, coat, scarf, _ = ids(nav_session)
    response = nav_client.post("/worksheet/bulk", data={"item_ids": [str(coat.id), str(scarf.id)], "state": "pending",
                                                        "next": "/worksheet?q=s"}, follow_redirects=False)
    path, query, _ = location(response)
    assert path == "/worksheet" and query["q"] == ["s"] and query["updated"] == ["2"]


def test_worksheet_filters_by_brand(nav_client: TestClient, nav_session: Session) -> None:
    _, _, _, north = ids(nav_session)
    page = nav_client.get("/worksheet", params={"brand": north.id})
    assert page.status_code == 200
    assert "Wool wrap coat" in page.text and "Silk scarf" not in page.text
    assert "Silk scarf" in nav_client.get("/worksheet").text


def test_payment_drawer_returns_to_the_money_view(nav_client: TestClient, nav_session: Session) -> None:
    customer, _, _, _ = ids(nav_session)
    form = {"customer_id": str(customer.id), "payment_date": date(2026, 10, 1).isoformat(), "amount": "50", "type": "payment",
            "stage": "unallocated", "next": "/money?season=FW26&focus=payments&done=payment"}
    response = nav_client.post("/payments", data=form, follow_redirects=False)
    assert response.headers["location"] == "/money?season=FW26&focus=payments&done=payment"
    unsafe = nav_client.post("/payments", data={**form, "next": "//elsewhere.example"}, follow_redirects=False)
    assert unsafe.headers["location"] == "/payments"
    assert nav_session.scalar(select(Payment).order_by(Payment.id.desc())).amount == Decimal("50.00")


def test_new_payments_are_only_offered_in_the_newest_season(nav_client: TestClient, nav_session: Session) -> None:
    customer, _, _, _ = ids(nav_session)
    newest = nav_client.get(f"/customers/{customer.id}", params={"season": "FW26"}).text
    older = nav_client.get(f"/customers/{customer.id}", params={"season": "SS26"}).text
    assert 'action="/payments"' in newest
    assert 'action="/payments"' not in older and "New payments are recorded in the newest season" in older
    assert 'action="/payments"' in nav_client.get("/money?season=FW26").text
    assert 'action="/payments"' not in nav_client.get("/money?season=SS26").text


def test_customer_files_keep_their_scopes_apart(nav_client: TestClient, nav_session: Session) -> None:
    customer, _, _, _ = ids(nav_session)
    page = nav_client.get(f"/customers/{customer.id}", params={"season": "FW26"}).text
    assert f"/exports/customers/{customer.id}.xlsx" in page
    assert f"/exports/customers/{customer.id}/internal.xlsx" in page
    shareable = page.split(f"/exports/customers/{customer.id}.xlsx", 1)[1].split("</a>", 1)[0]
    internal = page.split(f"/exports/customers/{customer.id}/internal.xlsx", 1)[1].split("</a>", 1)[0]
    assert "internal" not in shareable.lower()
    assert "internal" in internal.lower()


def test_errors_are_readable_pages_for_browsers_and_json_for_clients(nav_client: TestClient) -> None:
    browser = nav_client.get("/customers/99999", headers={"accept": "text/html,application/xhtml+xml"})
    assert browser.status_code == 404 and "text/html" in browser.headers["content-type"]
    client = nav_client.get("/customers/99999")
    assert client.status_code == 404 and "detail" in client.json()
    api = nav_client.get("/api/search", params={"q": ""}, headers={"accept": "text/html"})
    assert "text/html" not in api.headers.get("content-type", "")


def test_settings_groups_the_less_frequent_tools(nav_client: TestClient) -> None:
    page = nav_client.get("/settings").text
    for href in ("/rules", "/library", "/connections", "/inbox#sources", "/recovery", "/workspace/reset", "/audit", "/validation", "/import"):
        assert f'href="{href}' in page
