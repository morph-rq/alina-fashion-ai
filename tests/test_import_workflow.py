from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from pypdf import PdfReader

from app.database import Base
from app.importer import import_workbook, parse_customer_allocations
from app.models import BrandSeasonRule, Customer, Order, OrderItem, Payment, PaymentAllocation, ReadyBatch, Season, Shipment, ShipmentItem
from app.pdf_report import build_customer_pdf
from app.ready_importer import process_ready_workbook
from app.services import customer_statement, dashboard_data, ready_collection_status
from app.services import country_is_transport_exempt


SOURCE = Path("C:/Users/user/Downloads/Отчет_SS26 AA.xlsx")


@pytest.fixture(scope="module")
def imported_session(tmp_path_factory):
    if not SOURCE.exists():
        pytest.skip("Attached source workbook is not available")
    db_path = tmp_path_factory.mktemp("db") / "test.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        summary = import_workbook(session, SOURCE)
        session.commit()
        assert summary["orders"] == 17
        yield session


def test_real_workbook_imports_products_and_dynamic_shipments(imported_session: Session) -> None:
    assert imported_session.scalar(select(Order).where(Order.source_total > 0)) is not None
    assert imported_session.scalar(select(OrderItem).where(OrderItem.article == "FAQ5681")) is not None
    shipment_names = set(imported_session.scalars(select(Shipment.name)).all())
    assert {"Sped 1", "Sped 10", "Sped 16"} <= shipment_names


def test_all_previously_omitted_products_are_retained(imported_session: Session) -> None:
    for article in ['W1800-L72','W1744-WL60','W1773M-L72']:
        item=imported_session.scalar(select(OrderItem).where(OrderItem.article==article))
        assert item is not None
        assert item.worksheet_status=='pending'
        assert item.status=='Cancelled'  # Broken totals never become confirmed charges.
    skirt=imported_session.scalar(select(OrderItem).where(OrderItem.source_sheet=='DIOR CRUIZ SSv 26',OrderItem.source_row==5))
    assert skirt is not None and skirt.quantity==0 and skirt.worksheet_status=='pending'


def test_workbook_rows_have_one_status_and_reconcile_original_amount(imported_session: Session) -> None:
    from app.worksheet import worksheet_data
    data=worksheet_data(imported_session)
    ids=[r.item.id for r in data['rows']]
    assert len(ids)==len(set(ids))==143
    assert sum(data['counts'].values())==143
    assert dashboard_data(imported_session,'SS26').totals.final==Decimal('114599.25')


def test_customer_statement_is_customer_isolated(imported_session: Session) -> None:
    aa = imported_session.scalar(select(Customer).where(Customer.code == "AA"))
    statement = customer_statement(imported_session, aa.id)
    assert statement.items
    assert all(item.customer_id == aa.id for item in statement.items)


def test_ready_to_shipment_recalculates_without_stored_totals(imported_session: Session) -> None:
    aa = imported_session.scalar(select(Customer).where(Customer.code == "AA"))
    item = imported_session.scalar(
        select(OrderItem).where(OrderItem.customer_id == aa.id, OrderItem.status == "Ready")
    )
    shipment = imported_session.scalar(select(Shipment).where(Shipment.name == "Sped 16"))
    before = dashboard_data(imported_session)
    imported_session.add(ShipmentItem(shipment_id=shipment.id, order_item_id=item.id))
    item.status = "Assigned to shipment"
    imported_session.flush()
    imported_session.expire_all()
    after = dashboard_data(imported_session)
    assert after.totals.ready < before.totals.ready
    assert after.totals.shipments[shipment.id] > before.totals.shipments.get(shipment.id, 0)
    imported_session.rollback()


def test_customer_pdf_does_not_include_another_customers_product(imported_session: Session, tmp_path) -> None:
    aa = imported_session.scalar(select(Customer).where(Customer.code == "AA"))
    another_item = imported_session.scalar(
        select(OrderItem).where(OrderItem.customer_id != aa.id, OrderItem.article.is_not(None))
    )
    aa_item = imported_session.scalar(
        select(OrderItem).where(OrderItem.customer_id == aa.id, OrderItem.article.is_not(None), OrderItem.status != "Cancelled")
    )
    path = build_customer_pdf(imported_session, aa.id, tmp_path)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    assert aa_item.article in text
    assert another_item.article not in text


def test_supplier_allocation_parser_splits_customer_size_descriptions() -> None:
    assert parse_customer_allocations("AA 40", 1, "AA")[0] == ["AA"]
    assert parse_customer_allocations("AA S", 1, "AA")[0] == ["AA"]
    assert parse_customer_allocations("AA 38, SV 40", 2, "AA")[0] == ["AA", "SV"]
    allocations, reason = parse_customer_allocations("AA 38, SV 40", 1, "AA")
    assert allocations == [None]
    assert "source quantity" in reason


def test_customer_alias_is_applied_before_split() -> None:
    allocations, reason = parse_customer_allocations("AL 38, SV 40", 2, "AA", {"AL": "AA"})
    assert allocations == ["AA", "SV"]
    assert reason is None
    assert parse_customer_allocations("A.A. 40", 1, "AA")[0] == ["AA"]
    assert parse_customer_allocations("Alina A. S", 1, "AA")[0] == ["AA"]


def test_brand_season_rules_are_historically_separate(imported_session: Session) -> None:
    order = imported_session.scalar(select(Order).limit(1))
    future = Season(name="FW26")
    imported_session.add(future)
    imported_session.flush()
    imported_session.add(BrandSeasonRule(season_id=future.id, brand_id=order.brand_id, markup_percentage=Decimal("0.40"), initial_prepayment_percentage=Decimal("0.40"), ready_balance_percentage=Decimal("0.60"), transport_percentage=Decimal("0.06"), transport_exempt_country_codes="CH", expected_receipt_days=21))
    imported_session.flush()
    ss26_rule = imported_session.scalar(select(BrandSeasonRule).where(BrandSeasonRule.season_id == order.season_id, BrandSeasonRule.brand_id == order.brand_id))
    assert ss26_rule.markup_percentage != Decimal("0.40")
    imported_session.rollback()


def test_switzerland_transport_exception_is_configurable(imported_session: Session) -> None:
    rule = imported_session.scalar(select(BrandSeasonRule).limit(1))
    assert country_is_transport_exempt("CH", rule)
    assert not country_is_transport_exempt("IT", rule)


def test_expected_receipt_is_14_days_after_ready_payment_completion(imported_session: Session) -> None:
    aa = imported_session.scalar(select(Customer).where(Customer.code == "AA"))
    item = imported_session.scalar(select(OrderItem).where(
        OrderItem.customer_id == aa.id,
        OrderItem.ready_batch_id.is_(None),
        OrderItem.status == "Ordered",
    ))
    batch = ReadyBatch(season_id=item.order.season_id, name="Ready Receipt Test", ready_date=date(2026, 8, 20))
    imported_session.add(batch)
    imported_session.flush()
    item.ready_batch_id = batch.id
    item.status = "Ready"
    imported_session.flush()
    batch_id = batch.id
    requirements = ready_collection_status(imported_session, batch_id).requirements
    for customer_id, due in requirements.items():
        assert customer_id is not None
        payment = Payment(season_id=batch.season_id, customer_id=customer_id, date=date(2026, 8, 23), amount=due, type="payment", note="Ready batch test")
        imported_session.add(payment)
        imported_session.flush()
        imported_session.add(PaymentAllocation(payment_id=payment.id, ready_batch_id=batch_id, stage="ready", amount=due))
    imported_session.flush()
    updated = customer_statement(imported_session, aa.id)
    assert updated.expected_receipts[batch_id] == date(2026, 8, 23) + timedelta(days=14)
    imported_session.rollback()


def test_same_supplier_content_with_new_file_hash_is_incremental_without_duplicates(imported_session: Session, tmp_path) -> None:
    before = imported_session.scalar(select(func.count()).select_from(OrderItem))
    changed_container = tmp_path / "supplier-update.xlsx"
    changed_container.write_bytes(SOURCE.read_bytes() + b"\n")
    summary = import_workbook(imported_session, changed_container)
    imported_session.flush()
    after = imported_session.scalar(select(func.count()).select_from(OrderItem))
    assert summary["mode"] == "incremental"
    assert summary["changes"]["unchanged"] > 0
    assert after == before
    imported_session.rollback()


def test_ready_list_creates_batch_and_assigns_matching_master_item(imported_session: Session, tmp_path) -> None:
    candidates = imported_session.scalars(select(OrderItem).where(
        OrderItem.ready_batch_id.is_(None),
        OrderItem.customer_id.is_not(None),
        OrderItem.article.is_not(None),
        OrderItem.status == "Ordered",
    )).all()
    item = next(value for value in candidates if sum(1 for other in candidates if other.article == value.article) == 1)
    ready_file = tmp_path / "ready-2.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["Brand", "Article", "Customer", "Size", "Quantity", "Ready"])
    sheet.append([item.order.brand.name, item.article, item.customer.code, item.size, item.quantity, "Ready"])
    workbook.save(ready_file)
    summary = process_ready_workbook(imported_session, ready_file, "Ready Test", date(2026, 9, 1))
    imported_session.flush()
    assert summary["assigned_units"] == item.quantity
    assert item.ready_batch.name == "Ready Test"
    assert item.status == "Ready"
    imported_session.rollback()
