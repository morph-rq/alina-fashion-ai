"""One liability calculation for statements, dashboards, System Check and To-do.

The first three tests are ported from the main copy's test_workflow_improvements;
the purchase-group cases cover this copy's group payment terms.
"""
from datetime import date
from decimal import Decimal as D

from sqlalchemy import select

from test_workspace_features import workspace_session, workspace_client  # noqa: F401
from app.models import Customer, Expense, OrderItem, Payment, PaymentAllocation, PurchaseGroup, ReadyBatch
from app.operations import operations_inbox
from app.services import client_initial_charges, customer_statement, dashboard_data, factory_initial_due, factory_ready_due
from app.validation import validate_system


def test_factory_liability_excludes_client_markup(workspace_session):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    assert dashboard_data(db, 'AW26').factory_required == D('50')
    assert dashboard_data(db, 'AW26').totals.required_prepayment == D('65')
    batch = ReadyBatch(season_id=item.order.season_id, name='Ready 1')
    db.add(batch)
    db.flush()
    item.ready_batch_id = batch.id
    item.status = 'Ready'
    db.flush()
    assert factory_ready_due([item]) == D('50')
    assert dashboard_data(db, 'AW26').factory_required == D('100')
    assert customer_statement(db, item.customer_id, 'AW26').merchandise_charge == D('130')


def test_selling_override_does_not_change_factory_liability(workspace_session):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    item.selling_unit_price = D('150')
    db.flush()
    assert factory_initial_due(item.order) == D('50')
    assert customer_statement(db, item.customer_id, 'AW26').initial_prepayment == D('75')


def test_initial_client_shares_reconcile_when_half_cents_occur(workspace_session):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    item.wholesale_unit_price = D('120.50')
    other = Customer(code='SECOND')
    db.add(other)
    db.flush()
    db.add_all([
        OrderItem(order_id=item.order_id, customer_id=item.customer_id, quantity=3,
                  wholesale_unit_price=D('120.50'), markup_percentage=D('.3')),
        OrderItem(order_id=item.order_id, customer_id=other.id, quantity=2,
                  wholesale_unit_price=D('120.50'), markup_percentage=D('.3')),
    ])
    db.commit()
    db.expire_all()
    first_due = customer_statement(db, item.customer_id, 'AW26').initial_prepayment
    second_due = customer_statement(db, other.id, 'AW26').initial_prepayment
    assert (first_due, second_due) == (D('313.30'), D('156.65'))
    assert first_due + second_due == dashboard_data(db, 'AW26').totals.required_prepayment


def test_delivery_waits_for_fees_and_refunds_revoke_completion(workspace_session):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    batch = ReadyBatch(season_id=item.order.season_id, name='Ready 1')
    db.add(batch)
    db.flush()
    item.ready_batch_id = batch.id
    item.status = 'Ready'
    payment = Payment(customer_id=item.customer_id, season_id=item.order.season_id,
                      amount=D('130'), date=date(2026, 9, 1), type='payment')
    db.add(payment)
    db.flush()
    db.add(PaymentAllocation(payment_id=payment.id, stage='ready', ready_batch_id=batch.id, amount=D('65')))
    db.flush()
    assert customer_statement(db, item.customer_id, 'AW26').expected_receipts[batch.id] == date(2026, 9, 15)
    db.add(Expense(customer_id=item.customer_id, season_id=item.order.season_id, type='exceptional', amount=D('5')))
    db.flush()
    assert customer_statement(db, item.customer_id, 'AW26').expected_receipts[batch.id] is None
    db.add(Payment(customer_id=item.customer_id, season_id=item.order.season_id, amount=D('5'), date=date(2026, 9, 4), type='payment'))
    db.flush()
    assert customer_statement(db, item.customer_id, 'AW26').expected_receipts[batch.id] == date(2026, 9, 18)
    db.add(Payment(customer_id=item.customer_id, season_id=item.order.season_id, amount=D('1'), date=date(2026, 9, 5), type='refund'))
    db.flush()
    assert customer_statement(db, item.customer_id, 'AW26').expected_receipts[batch.id] is None


def test_purchase_group_terms_reconcile_in_system_check(workspace_session):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    other = Customer(code='SECOND')
    db.add(other)
    db.flush()
    group = PurchaseGroup(name='Seller lot', season_id=item.order.season_id,
                          initial_percentage=D('0.30'), shipment_percentage=D('0.70'))
    db.add(group)
    db.flush()
    db.add(OrderItem(order_id=item.order_id, customer_id=other.id, quantity=1, size='M', purchase_group_id=group.id,
                     wholesale_unit_price=D('33.33'), markup_percentage=D('.3')))
    item.size = 'M'
    db.commit()
    db.expire_all()
    shares = client_initial_charges(item.order)
    assert sum(shares.values()) == dashboard_data(db, 'AW26').totals.required_prepayment
    assert shares[other.id] == D('13.00')  # 43.33 customer value x 30%, cent-rounded once
    codes = {issue.code for issue in validate_system(db).errors}
    assert 'CUSTOMER_INITIAL_ALLOCATION_MISMATCH' not in codes
    assert 'CUSTOMER_READY_ALLOCATION_MISMATCH' not in codes


def test_todo_uses_wholesale_factory_due_after_customers_pay(workspace_session):
    db = workspace_session
    item = db.scalar(select(OrderItem))
    batch = ReadyBatch(season_id=item.order.season_id, name='Ready 1')
    db.add(batch)
    db.flush()
    item.ready_batch_id = batch.id
    item.status = 'Ready'
    payment = Payment(customer_id=item.customer_id, season_id=item.order.season_id, amount=D('65'), date=date(2026, 9, 1), type='payment')
    db.add(payment)
    db.flush()
    db.add(PaymentAllocation(payment_id=payment.id, stage='ready', ready_batch_id=batch.id, amount=D('65')))
    db.flush()
    factory_task = next(task for task in operations_inbox(db) if task.title.startswith('Factory payment'))
    assert '50.00' in factory_task.detail
