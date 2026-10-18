"""Shared product mutation rules used by the form, worksheet, Excel and AI paths."""
import io
from datetime import date
from decimal import Decimal as D

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from test_workspace_features import workspace_session, workspace_client  # noqa: F401
from app import excel_exchange as ex
from app import item_service
from app.models import AllocationReview, AuditLog, Customer, OrderItem, ReadyBatch


def _ready(db, item):
    batch = ReadyBatch(season_id=item.order.season_id, name='Ready 1', ready_date=date(2026, 9, 1))
    db.add(batch)
    db.flush()
    item.ready_batch_id = batch.id
    item.status = 'Ready'
    db.commit()
    return batch


def _form(item, **overrides):
    return {'customer_id': str(item.customer_id), 'article': item.article or '', 'name': item.name or '',
            'color': item.color or '', 'size': item.size or '', 'quantity': str(item.quantity),
            'wholesale_unit_price': str(item.wholesale_unit_price), 'markup_percentage': '30',
            'status': item.status, 'ready_batch_id': str(item.ready_batch_id or ''), 'revision': str(item.revision)} | overrides


def test_leading_zero_sku_and_text_are_kept_verbatim(workspace_session):
    db = workspace_session
    order = db.scalar(select(OrderItem)).order
    item = item_service.create_item(db, order, {'article': ' 000451 ', 'name': '=SUM(A1)', 'quantity': '2',
                                                'wholesale_unit_price': D('10')}, actor='test', channel='test').item
    assert item.article == '000451' and item.name == '=SUM(A1)' and item.quantity == 2
    assert item.allocation_status == 'Review'
    assert db.scalar(select(AllocationReview).where(AllocationReview.row == item.source_row)) is not None


@pytest.mark.parametrize('value', ['1.5', '0', '-1', 'abc', 'NaN', '2000000'])
def test_invalid_quantities_are_rejected(value):
    with pytest.raises(item_service.ItemRuleError):
        item_service.parse_quantity(value)


@pytest.mark.parametrize('value', ['10.001', '-1', 'Infinity', 'x'])
def test_invalid_money_is_rejected(value):
    with pytest.raises(item_service.ItemRuleError):
        item_service.parse_money(value, 'Price')


def test_stale_form_revision_is_rejected_and_newer_value_kept(workspace_session, workspace_client):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    stale = _form(item, size='40')
    assert workspace_client.post(f'/items/{item.id}', data=_form(item, size='38')).status_code == 200
    db.refresh(item)
    response = workspace_client.post(f'/items/{item.id}', data=stale)
    assert response.status_code == 409
    db.refresh(item)
    assert item.size == '38'


def test_ready_row_needs_correction_reason_for_financial_change(workspace_session, workspace_client):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    _ready(db, item)
    assert workspace_client.post(f'/items/{item.id}', data=_form(item, quantity='2')).status_code == 422
    db.refresh(item)
    assert item.quantity == 1
    # A non-financial correction does not need a reason.
    assert workspace_client.post(f'/items/{item.id}', data=_form(item, color='navy')).status_code == 200
    db.refresh(item)
    response = workspace_client.post(f'/items/{item.id}', data=_form(item, quantity='2', correction_reason='Supplier confirmed two pieces'))
    assert response.status_code == 200
    db.refresh(item)
    assert item.quantity == 2
    audit = db.scalar(select(AuditLog).where(AuditLog.field == 'quantity'))
    assert 'Supplier confirmed two pieces' in audit.description


def test_excel_cannot_rewrite_financial_fields_of_ready_rows(workspace_session, tmp_path, monkeypatch):
    db = workspace_session
    monkeypatch.setattr(ex, 'EXCHANGE_DIR', tmp_path / 'exchange')
    item = db.scalar(select(OrderItem))
    item.size = 'M'
    _ready(db, item)
    book = load_workbook(io.BytesIO(ex.export_editable(db)))
    book['Orders']['J2'] = 5
    stream = io.BytesIO()
    book.save(stream)
    result = ex.inspect_upload(db, stream.getvalue())
    assert result['errors'] and 'product editor' in result['errors'][0]['message']
    assert db.scalar(select(OrderItem)).quantity == 1


def test_assigning_customer_resolves_note_with_recorded_reason(workspace_session, workspace_client):
    db = workspace_session
    order = db.scalar(select(OrderItem)).order
    item = item_service.create_item(db, order, {'name': 'Scarf', 'quantity': 1, 'wholesale_unit_price': D('5')},
                                    actor='test', channel='Manual entry').item
    db.commit()
    maria = db.scalar(select(Customer))
    response = workspace_client.post(f'/items/{item.id}', data=_form(item, customer_id=str(maria.id), name='Scarf', status='Ordered', ready_batch_id=''))
    assert response.status_code == 200
    review = db.scalar(select(AllocationReview).where(AllocationReview.row == item.source_row))
    assert review.status == 'Resolved' and review.resolved_customer_id == maria.id
    assert review.resolution_note and review.resolved_at
