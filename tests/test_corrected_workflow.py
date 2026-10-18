from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from openpyxl import Workbook
from pypdf import PdfReader
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import (
    AuditLog,
    Brand,
    BrandSeasonRule,
    Customer,
    Expense,
    FactoryPayment,
    Order,
    OrderItem,
    Payment,
    PaymentAllocation,
    ReadyBatch,
    Season,
    Shipment,
    ShipmentItem,
)
from app.pdf_report import build_customer_pdf
from app.ready_importer import process_ready_workbook
from app.services import customer_statement, dashboard_data, ready_collection_status, shipment_summary
from app.validation import validate_system


D = Decimal


@pytest.fixture()
def workflow_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        season = Season(name="SS26")
        brand = Brand(name="Test Factory")
        aa = Customer(code="AA", country_code="IT")
        sv = Customer(code="SV", country_code="CH")
        session.add_all([season, brand, aa, sv])
        session.flush()
        rule = BrandSeasonRule(
            season_id=season.id,
            brand_id=brand.id,
            markup_percentage=D("0"),
            initial_prepayment_percentage=D("0.50"),
            ready_balance_percentage=D("0.50"),
            transport_percentage=D("0.05"),
            transport_exempt_country_codes="CH",
            expected_receipt_days=14,
        )
        order = Order(season_id=season.id, brand_id=brand.id, status="Ordered")
        session.add_all([rule, order])
        session.flush()
        batch = ReadyBatch(season_id=season.id, name="Ready 1", ready_date=date(2026, 9, 1))
        session.add(batch)
        session.flush()
        session.add_all(
            [
                OrderItem(order_id=order.id, customer_id=aa.id, ready_batch_id=batch.id, article="A", name="Ready coat", size="40", quantity=1, wholesale_unit_price=D("1000"), markup_percentage=D("0"), status="Ready"),
                OrderItem(order_id=order.id, customer_id=aa.id, article="B", name="Production bag", size="M", quantity=1, wholesale_unit_price=D("2000"), markup_percentage=D("0"), status="Production"),
                OrderItem(order_id=order.id, customer_id=sv.id, ready_batch_id=batch.id, article="C", name="Swiss shoes", size="39", quantity=1, wholesale_unit_price=D("700"), markup_percentage=D("0"), status="Ready"),
            ]
        )
        session.commit()
        yield session


def test_partial_ready_is_allocated_per_item_and_receipt_waits_for_every_customer(workflow_session: Session) -> None:
    aa = workflow_session.scalar(select(Customer).where(Customer.code == "AA"))
    sv = workflow_session.scalar(select(Customer).where(Customer.code == "SV"))
    batch = workflow_session.scalar(select(ReadyBatch).where(ReadyBatch.name == "Ready 1"))

    aa_statement = customer_statement(workflow_session, aa.id)
    assert aa_statement.ordered == D("3000.00")
    assert aa_statement.initial_prepayment == D("1500.00")
    assert aa_statement.ready_balance_due == D("500.00")

    requirements = ready_collection_status(workflow_session, batch.id).requirements
    assert requirements == {aa.id: D("500.00"), sv.id: D("350.00")}

    aa_payment = Payment(season_id=batch.season_id, customer_id=aa.id, date=date(2026, 9, 2), amount=D("850"), type="payment")
    workflow_session.add(aa_payment)
    workflow_session.flush()
    workflow_session.add(PaymentAllocation(payment_id=aa_payment.id, ready_batch_id=batch.id, stage="ready", amount=D("850")))
    workflow_session.flush()
    assert ready_collection_status(workflow_session, batch.id).completion_date is None
    assert customer_statement(workflow_session, aa.id).expected_receipts[batch.id] is None

    sv_payment = Payment(season_id=batch.season_id, customer_id=sv.id, date=date(2026, 9, 5), amount=D("350"), type="payment")
    workflow_session.add(sv_payment)
    workflow_session.flush()
    workflow_session.add(PaymentAllocation(payment_id=sv_payment.id, ready_batch_id=batch.id, stage="ready", amount=D("350")))
    workflow_session.flush()
    assert ready_collection_status(workflow_session, batch.id).completion_date == date(2026, 9, 5)
    # Ready collection alone is insufficient: initial and other current charges must also be paid.
    assert customer_statement(workflow_session, aa.id).expected_receipts[batch.id] is None
    workflow_session.add_all([
        Payment(season_id=batch.season_id, customer_id=aa.id, date=date(2026, 9, 5), amount=D('1150'), type='payment'),
        Payment(season_id=batch.season_id, customer_id=sv.id, date=date(2026, 9, 5), amount=D('350'), type='payment'),
    ])
    workflow_session.flush()
    assert customer_statement(workflow_session, aa.id).expected_receipts[batch.id] == date(2026, 9, 19)


def test_system_check_displays_zero_difference_when_source_facts_reconcile(workflow_session: Session) -> None:
    report = validate_system(workflow_session)
    messages = {issue.message for issue in report.issues if issue.severity == "pass"}
    assert report.passed
    assert "Difference: €0.00" in messages
    assert "All calculations reconcile" in messages


def test_factory_liability_transport_cost_types_and_customer_pdf(workflow_session: Session, tmp_path) -> None:
    order = workflow_session.scalar(select(Order))
    batch = workflow_session.scalar(select(ReadyBatch))
    aa = workflow_session.scalar(select(Customer).where(Customer.code == "AA"))
    sv = workflow_session.scalar(select(Customer).where(Customer.code == "SV"))
    aa_item = workflow_session.scalar(select(OrderItem).where(OrderItem.article == "A"))
    sv_item = workflow_session.scalar(select(OrderItem).where(OrderItem.article == "C"))

    sped_aa = Shipment(season_id=order.season_id, name="Sped 18", date=date(2026, 9, 7), status="In transit", use_rule_transport=True, fixed_cost=D("10"), suitcase_cost=D("20"), bag_cost=D("30"))
    sped_ch = Shipment(season_id=order.season_id, name="Sped 19", date=date(2026, 9, 7), status="In transit", use_rule_transport=True, destination_country_code="CH")
    workflow_session.add_all([sped_aa, sped_ch])
    workflow_session.flush()
    workflow_session.add_all([
        ShipmentItem(shipment_id=sped_aa.id, order_item_id=aa_item.id),
        ShipmentItem(shipment_id=sped_ch.id, order_item_id=sv_item.id),
        Expense(season_id=order.season_id, customer_id=aa.id, type="exceptional", amount=D("15"), note="Manual courier exception"),
        Payment(season_id=order.season_id, customer_id=aa.id, date=date(2026, 9, 2), amount=D("100"), type="payment", note="PRIVATE BANK REFERENCE 123"),
        FactoryPayment(season_id=order.season_id, order_id=order.id, date=date(2026, 8, 1), amount=D("1850"), stage="initial"),
        FactoryPayment(season_id=order.season_id, order_id=order.id, ready_batch_id=batch.id, date=date(2026, 9, 6), amount=D("500"), stage="ready"),
    ])
    aa_item.status = "In transit"
    aa_item.ai_source_text = "PRIVATE CUSTOMER MESSAGE"
    aa_item.customer_requirements = "PRIVATE BODY DETAILS"
    aa.name = "Alice Adams"
    aa.phone = "+39 555 PRIVATE"
    aa.email = "private@example.com"
    aa.address = "PRIVATE HOME ADDRESS"
    sv_item.status = "In transit"
    workflow_session.commit()
    workflow_session.expire_all()

    assert shipment_summary(workflow_session.scalar(select(Shipment).where(Shipment.name == "Sped 18"))).transport_fee == D("50.00")
    assert shipment_summary(workflow_session.scalar(select(Shipment).where(Shipment.name == "Sped 18"))).fixed_costs == D("60.00")
    assert shipment_summary(workflow_session.scalar(select(Shipment).where(Shipment.name == "Sped 19"))).transport_fee == D("0.00")

    dashboard = dashboard_data(workflow_session)
    assert dashboard.factory_required == D("2700.00")
    assert dashboard.factory_paid == D("2350.00")
    assert dashboard.factory_account.outstanding == D("350.00")

    statement = customer_statement(workflow_session, aa.id)
    assert statement.transport_costs == D("50.00")
    assert statement.fixed_costs == D("60.00")
    assert statement.allocated_costs == D("125.00")
    assert statement.shipment_costs[sped_aa.id] == D("110.00")
    path = build_customer_pdf(workflow_session, aa.id, tmp_path)
    assert path.name == "AA_SS26.pdf"
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    assert "Sped 18" in text and "110.00" in text and "In transit" in text
    assert "Swiss shoes" not in text
    assert "Alice Adams" in text and "Customer reference: AA" in text
    assert "PRIVATE" not in text
    assert "private@example.com" not in text
    assert "source-of-truth" not in text
    assert "Liability" not in text
    assert "Ready 1" not in text
    assert "wholesale" not in text.casefold()
    assert "markup" not in text.casefold()


def test_owner_can_create_a_product_row_and_every_view_recalculates(workflow_session: Session) -> None:
    order = workflow_session.scalar(select(Order))
    aa = workflow_session.scalar(select(Customer).where(Customer.code == "AA"))

    def override_db():
        yield workflow_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/orders/{order.id}/items",
                data={
                    "customer_id": str(aa.id),
                    "article": "FAQ9434",
                    "name": "Owner entered coat",
                    "color": "Navy",
                    "size": "40",
                    "quantity": "1",
                    "wholesale_unit_price": "1028",
                    "markup_percentage": "35",
                    "status": "Ordered",
                },
                follow_redirects=False,
            )
        assert response.status_code == 303
    finally:
        app.dependency_overrides.clear()

    item = workflow_session.scalar(select(OrderItem).where(OrderItem.article == "FAQ9434"))
    assert item is not None and item.source_file == "Manual entry"
    assert customer_statement(workflow_session, aa.id).ordered == D("4387.80")
    assert workflow_session.scalar(select(AuditLog).where(AuditLog.entity_type == "OrderItem", AuditLog.entity_id == str(item.id))) is not None


def test_customer_money_and_expenses_do_not_leak_between_seasons(workflow_session: Session) -> None:
    aa = workflow_session.scalar(select(Customer).where(Customer.code == "AA"))
    future = Season(name="FW26")
    workflow_session.add(future)
    workflow_session.flush()
    workflow_session.add_all([
        Payment(season_id=future.id, customer_id=aa.id, date=date(2026, 10, 1), amount=D("999"), type="payment"),
        Expense(season_id=future.id, customer_id=aa.id, type="exceptional", amount=D("111")),
    ])
    workflow_session.flush()
    statement = customer_statement(workflow_session, aa.id, "SS26")
    assert statement.account.total_paid == D("0.00")
    assert statement.allocated_costs == D("0.00")


def test_ready_import_and_customer_views_use_the_explicit_season(workflow_session: Session, tmp_path) -> None:
    aa = workflow_session.scalar(select(Customer).where(Customer.code == "AA"))
    brand = workflow_session.scalar(select(Brand).where(Brand.name == "Test Factory"))
    fw = Season(name="FW26")
    workflow_session.add(fw)
    workflow_session.flush()
    workflow_session.add(BrandSeasonRule(
        season_id=fw.id,
        brand_id=brand.id,
        markup_percentage=D("0.20"),
        initial_prepayment_percentage=D("0.50"),
        ready_balance_percentage=D("0.50"),
        transport_percentage=D("0.05"),
        transport_exempt_country_codes="CH",
        expected_receipt_days=14,
    ))
    order = Order(season_id=fw.id, brand_id=brand.id, status="Ordered")
    workflow_session.add(order)
    workflow_session.flush()
    item = OrderItem(
        order_id=order.id,
        customer_id=aa.id,
        article="FW-ONLY",
        name="Winter coat",
        size="40",
        quantity=1,
        wholesale_unit_price=D("100"),
        markup_percentage=D("0.20"),
        status="Ordered",
    )
    workflow_session.add(item)
    workflow_session.commit()

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Article", "Customer", "Size", "Quantity"])
    sheet.append(["FW-ONLY", "AA 40", "40", 1])
    ready_path = tmp_path / "fw-ready.xlsx"
    workbook.save(ready_path)

    summary = process_ready_workbook(workflow_session, ready_path, "FW Ready 1", date(2026, 11, 1), "FW26")
    workflow_session.commit()
    assert summary["season"] == "FW26"
    assert item.ready_batch.season_id == fw.id
    assert customer_statement(workflow_session, aa.id, "FW26").ordered == D("120.00")
    assert customer_statement(workflow_session, aa.id, "SS26").ordered == D("3000.00")

    def override_db():
        yield workflow_session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            fw_page = client.get("/customers?season=FW26")
            ss_page = client.get("/customers?season=SS26")
        assert fw_page.status_code == 200 and "FW26 master table" in fw_page.text
        assert ss_page.status_code == 200 and "SS26 master table" in ss_page.text
    finally:
        app.dependency_overrides.clear()
