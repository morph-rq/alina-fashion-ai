"""Reproductions for confirmed accounting and preservation defects (stage 1).

Each test documents a defect observed in the testing copy before its fix:
refund presentation, repeated partial Ready rows, split provenance, final-price
reverse rounding, and startup backfills overwriting reviewed allocations.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from openpyxl import Workbook
from pypdf import PdfReader
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.calculations import final_total, item_amounts
from app.database import Base
from app.models import (
    AllocationReview, Brand, BrandSeasonRule, Collection, CollectionItem, Customer, ImportReview,
    Order, OrderItem, Payment, ReadyBatch, Season,
)
from app.pdf_report import build_customer_pdf
from app.ready_importer import process_ready_workbook
from app.services import customer_statement

D = Decimal


@pytest.fixture()
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        season = Season(name='FW26')
        brand = Brand(name='The Row')
        maria = Customer(code='MI', name='Maria Ivanova', country_code='IT')
        session.add_all([season, brand, maria])
        session.flush()
        session.add(BrandSeasonRule(season_id=season.id, brand_id=brand.id, markup_percentage=D('0.30'),
                                    initial_prepayment_percentage=D('0.50'), ready_balance_percentage=D('0.50'),
                                    transport_percentage=D('0.05'), transport_exempt_country_codes='CH',
                                    expected_receipt_days=14))
        order = Order(season_id=season.id, brand_id=brand.id)
        session.add(order)
        session.commit()
        yield session


def _item(db, **values):
    order = db.scalar(select(Order))
    maria = db.scalar(select(Customer))
    defaults = dict(order_id=order.id, customer_id=maria.id, article='0451', name='Black coat', size='38', quantity=1,
                    wholesale_unit_price=D('1000'), markup_percentage=D('0.30'), status='Ordered')
    item = OrderItem(**(defaults | values))
    db.add(item)
    db.commit()
    return item


def _ready_book(tmp_path, rows, name='ready.xlsx'):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['Article', 'Customer', 'Size', 'Quantity'])
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


def _pdf_text(path):
    return '\n'.join(page.extract_text() for page in PdfReader(path).pages)


def test_refund_is_labelled_and_signed_in_customer_pdf(db, tmp_path, monkeypatch):
    item = _item(db)
    season = db.scalar(select(Season))
    db.add_all([
        Payment(season_id=season.id, customer_id=item.customer_id, date=date(2026, 9, 1), amount=D('650.00'), type='payment'),
        Payment(season_id=season.id, customer_id=item.customer_id, date=date(2026, 9, 3), amount=D('100.00'), type='refund'),
    ])
    db.commit()
    monkeypatch.setattr('app.pdf_report.product_image', lambda _path: '')
    statement = customer_statement(db, item.customer_id, 'FW26')
    assert statement.account.total_paid == D('550.00')
    text = _pdf_text(build_customer_pdf(db, item.customer_id, tmp_path, 'FW26'))
    assert 'Refund' in text
    assert '-100.00' in text
    # Every listed payment row reconciles to the PAYMENTS RECEIVED summary figure.
    assert text.count('Payment received') == 1
    assert '550.00' in text


def test_repeated_partial_ready_rows_for_one_sku_use_the_split_remainder(db, tmp_path):
    item = _item(db, quantity=3, size='M')
    path = _ready_book(tmp_path, [['0451', 'MI', 'M', 1], ['0451', 'MI', 'M', 1]])
    summary = process_ready_workbook(db, path, 'Ready 1', date(2026, 10, 1), 'FW26')
    db.commit()
    rows = db.scalars(select(OrderItem).where(OrderItem.article == '0451')).all()
    assert summary['reviews'] == 0, db.scalars(select(ImportReview.reason)).all()
    assert summary['assigned_units'] == 2
    assert sum(row.quantity for row in rows) == 3
    assert sum(row.quantity for row in rows if row.ready_batch_id) == 2
    assert sum(row.quantity for row in rows if not row.ready_batch_id) == 1
    assert item.ready_batch_id is not None


def test_ready_split_keeps_complete_provenance_and_collection_membership(db, tmp_path):
    item = _item(db, quantity=2, size='M', source_file='AI intake', source_sheet='The Row', source_row=7,
                 source_note='AI source archive: abc', ai_source_text='Maria, black coat', product_url='https://example.test/p',
                 image_source_filename='coat.jpg', image_match_method='ai_attachment_user_confirmed',
                 requested_delivery_date=date(2026, 12, 1), logistics_preference='courier',
                 customer_requirements='gift wrap', worksheet_status='process', worksheet_note='checked',
                 allocation_group_id='grp-1', selling_unit_price=D('1400'))
    collection = Collection(name='Window')
    db.add(collection)
    db.flush()
    db.add(CollectionItem(collection_id=collection.id, order_item_id=item.id))
    db.commit()
    process_ready_workbook(db, _ready_book(tmp_path, [['0451', 'MI', 'M', 1]]), 'Ready 1', None, 'FW26')
    db.commit()
    remainder = db.scalar(select(OrderItem).where(OrderItem.id != item.id))
    for field in ('source_file', 'source_sheet', 'source_row', 'source_note', 'ai_source_text', 'product_url',
                  'image_source_filename', 'image_match_method', 'requested_delivery_date', 'logistics_preference',
                  'customer_requirements', 'worksheet_status', 'worksheet_note', 'selling_unit_price'):
        assert getattr(remainder, field) == getattr(item, field), field
    assert remainder.allocation_group_id == item.allocation_group_id
    assert db.scalar(select(func.count()).select_from(CollectionItem).where(CollectionItem.order_item_id == remainder.id)) == 1
    # Value is conserved across the split.
    assert item_amounts(item).final + item_amounts(remainder).final == D('2800.00')


@pytest.mark.parametrize('final, markup', [('100.00', '0.35'), ('1200.00', '0.30'), ('99.99', '0.25'), ('1.00', '0.35')])
def test_final_price_reverse_rounding_preserves_the_stated_final(final, markup):
    from app.pricing import wholesale_from_final
    result = wholesale_from_final(D(final), D(markup))
    if result.exact:
        assert final_total(result.wholesale, 1, D(markup)) == D(final)
        assert result.selling_unit_price is None
    else:
        # No cent wholesale produces this final: the final is kept as an explicit selling price.
        assert result.selling_unit_price == D(final)
        assert result.reason


def test_startup_backfill_never_overrides_a_reviewed_allocation(monkeypatch):
    """A restart must not reparse source descriptions over an operator's allocation."""
    from app import database, migrations
    from app.importer import SPECS
    spec = next(spec for spec in SPECS if spec.customer_col)
    row = spec.rows[0]
    Base.metadata.drop_all(database.engine)
    migrations.migrate_database()
    with database.SessionLocal.begin() as session:
        season = Season(name='SS26')
        brand = Brand(name='Imported brand')
        aa = Customer(code='AA')
        ki = Customer(code='KI')
        session.add_all([season, brand, aa, ki])
        session.flush()
        order = Order(season_id=season.id, brand_id=brand.id)
        session.add(order)
        session.flush()
        raw = {f'{spec.customer_col}{row}': 'AA 38 KI 40'}  # implies 2 units; source quantity is 3 -> ambiguous
        session.add(OrderItem(order_id=order.id, customer_id=ki.id, allocation_status='Allocated', source_sheet=spec.sheet,
                              source_row=row, source_file='Отчет_SS26 AA.xlsx', quantity=3, size='40',
                              wholesale_unit_price=D('10'), source_note=json.dumps(raw)))
        session.add(AllocationReview(source_file='Отчет_SS26 AA.xlsx', sheet=spec.sheet, row=row, raw_values='{}',
                                     reason='ambiguous', status='Resolved', resolved_customer_id=ki.id))
    for _restart in range(2):
        migrations.migrate_database()
    with database.SessionLocal() as session:
        item = session.scalar(select(OrderItem))
        assert item.customer.code == 'KI'
        assert item.allocation_status == 'Allocated'
        assert session.scalar(select(func.count()).select_from(AllocationReview).where(AllocationReview.status == 'Open')) == 0
    Base.metadata.drop_all(database.engine)
