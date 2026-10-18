from datetime import date
from decimal import Decimal, InvalidOperation
from collections import defaultdict
import secrets
import re

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.calculations import ZERO, money
from app.database import get_db
from app.models import (Customer, Distributor, OrderItem, PurchaseGroup, Season,
                        SellerCreditAccount, SellerCreditEntry, ImportReview,
                        ReadyBatch, Shipment, ShipmentItem)
from app.worksheet import (STATUS_LABELS, account_balance, audit, ensure_import_groups,
                          get_account, lock_ledger, transfer_credit, worksheet_data, status_of)


def amount_value(value, label='Amount', positive=False):
    try:
        amount = Decimal(str(value).replace(' ', '').replace(',', '.'))
        if not amount.is_finite() or amount < 0 or amount > Decimal('999999999999.99'):
            raise ValueError()
        if positive and amount <= 0:
            raise ValueError()
        if amount != money(amount):
            raise ValueError()
        return money(amount)
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f'{label} must be a valid {"positive" if positive else "non-negative"} amount with at most two decimals.')


def find(db, model, identity):
    obj = db.get(model, identity)
    if obj is None:
        raise HTTPException(404, 'Record not found.')
    return obj


def render_worksheet(request, db, templates, context):
    data = worksheet_data(db)
    query = request.query_params
    search = query.get('q', '').strip().casefold()
    selected = []
    for row in data['rows']:
        i = row.item
        values = [i.name, i.article, i.color, i.size, i.source_sheet,
                  i.customer.code if i.customer else 'unallocated',
                  i.customer.name if i.customer else '', i.order.brand.name,
                  i.purchase_group.name if i.purchase_group else 'ungrouped']
        if search and search not in ' '.join(str(v or '') for v in values).casefold():
            continue
        if query.get('season') and str(i.order.season_id) != query['season']:
            continue
        if query.get('customer') and str(i.customer_id or 0) != query['customer']:
            continue
        if query.get('distributor') and str(i.purchase_group.distributor_id if i.purchase_group and i.purchase_group.distributor_id else 0) != query['distributor']:
            continue
        if query.get('group') and str(i.purchase_group_id or 0) != query['group']:
            continue
        if query.get('state') in STATUS_LABELS and row.state != query['state']:
            continue
        selected.append(row)
    sections = {state: defaultdict(list) for state in STATUS_LABELS}
    grouping = query.get('by', 'group')
    for row in selected:
        item = row.item
        if grouping == 'brand':
            label = item.order.brand.name
        elif grouping == 'customer':
            label = (item.customer.name or item.customer.code) if item.customer else 'Customer needs review'
        elif grouping == 'distributor':
            label = item.purchase_group.distributor.name if item.purchase_group and item.purchase_group.distributor else 'Distributor not assigned'
        else:
            label = item.purchase_group.name if item.purchase_group else 'Not grouped yet'
        sections[row.state][label].append(row)
    return templates.TemplateResponse(request, 'worksheet.html', context(request, **data,
        sections=sections, filtered_count=len(selected),
        customers=db.scalars(select(Customer).order_by(Customer.code)).all(),
        distributors=db.scalars(select(Distributor).order_by(Distributor.name)).all(),
        seasons=db.scalars(select(Season).order_by(Season.id.desc())).all(),
        reviews=db.scalars(select(ImportReview).where(ImportReview.status == 'Open')).all()))


def install_worksheet(app, templates, context):
    @app.exception_handler(HTTPException)
    async def worksheet_error(request: Request, exc: HTTPException):
        if request.url.path.startswith(('/worksheet','/groups','/credits','/distributors','/storage','/source-reviews')):
            return templates.TemplateResponse(request, 'workflow_error.html', context(request,message=exc.detail),status_code=exc.status_code)
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request,exc)

    @app.get('/worksheet')
    def worksheet_page(request: Request, db=Depends(get_db)):
        return render_worksheet(request, db, templates, context)

    @app.post('/worksheet/group-unassigned')
    def group_unassigned(db=Depends(get_db)):
        count = ensure_import_groups(db)
        audit(db, 'PurchaseGroup', 'unassigned', f'Grouped {count} products; no payments inferred.')
        db.commit()
        return RedirectResponse('/worksheet', 303)

    @app.get('/worksheet/items/{item_id}')
    def item_page(item_id: int, request: Request, db=Depends(get_db)):
        item = find(db, OrderItem, item_id)
        return templates.TemplateResponse(request, 'worksheet_item.html', context(request, item=item,
            status_labels=STATUS_LABELS, current_state=status_of(item), customers=db.scalars(select(Customer).order_by(Customer.code)).all(),
            groups=db.scalars(select(PurchaseGroup).where(PurchaseGroup.season_id == item.order.season_id)).all()))

    @app.post('/worksheet/items/{item_id}')
    def item_update(item_id: int, state: str = Form(...), customer_id: str = Form(''),
                    note: str = Form(''), name: str | None = Form(None), article: str | None = Form(None),
                    color: str | None = Form(None), size: str | None = Form(None), quantity: int | None = Form(None),
                    wholesale: str | None = Form(None), selling: str | None = Form(None), markup: str | None = Form(None),
                    return_review: str = Form(''), db=Depends(get_db)):
        item = find(db, OrderItem, item_id)
        new_price = amount_value(wholesale,'Wholesale price') if wholesale and wholesale.strip() else None
        new_selling = amount_value(selling,'Selling override') if selling and selling.strip() else None
        new_markup = amount_value(markup,'Markup') / 100 if markup is not None else None
        if new_markup is not None and new_markup > 1:
            raise HTTPException(422,'Markup cannot exceed 100%.')
        if quantity is not None and (quantity < 0 or quantity > 1000000):
            raise HTTPException(422,'Quantity must be between 0 and 1,000,000.')
        if state not in STATUS_LABELS:
            raise HTTPException(422, 'Choose a valid worksheet status.')
        customer = find(db, Customer, int(customer_id)) if customer_id.isdigit() else None
        if customer_id and not customer:
            raise HTTPException(422, 'Choose a valid customer.')
        if item.customer_id != (customer.id if customer else None):
            if db.scalar(select(SellerCreditAccount.id).where(SellerCreditAccount.item_id == item.id)):
                raise HTTPException(422, 'This product has a seller-credit history. Its customer cannot be changed.')
            item.customer_id = customer.id if customer else None
            item.allocation_status = 'Allocated' if customer else 'Review'
        previous = item.worksheet_status
        for field,value in [('name',name),('article',article),('color',color),('size',size)]:
            if value is not None: setattr(item,field,value.strip() or None)
        if quantity is not None: item.quantity = quantity
        if wholesale is not None: item.wholesale_unit_price = new_price
        if selling is not None: item.selling_unit_price = new_selling
        if new_markup is not None: item.markup_percentage = new_markup
        item.worksheet_status, item.worksheet_note = state, note.strip() or None
        audit(db, 'OrderItem', item.id, f'Worksheet status {previous} → {state}; customer {item.customer_id}. {note}')
        db.commit()
        if re.fullmatch(r'(import|allocation)-\d+',return_review):
            kind,identity = return_review.split('-')
            return RedirectResponse(f'/source-reviews/{kind}/{identity}',303)
        return RedirectResponse('/worksheet', 303)

    @app.post('/worksheet/bulk')
    def bulk_items(item_ids: list[int] = Form(...), group_id: str = Form(''), state: str = Form(''), db=Depends(get_db)):
        if not group_id and not state:
            raise HTTPException(422, 'Choose a group or status before applying.')
        if state and state not in STATUS_LABELS:
            raise HTTPException(422, 'Invalid status.')
        group = find(db, PurchaseGroup, int(group_id)) if group_id.isdigit() else None
        if group_id and not group:
            raise HTTPException(422, 'Invalid group.')
        items = [find(db, OrderItem, identity) for identity in set(item_ids)]
        for item in items:
            if group and item.purchase_group_id != group.id:
                if group.season_id != item.order.season_id:
                    raise HTTPException(422, 'A shipment group cannot mix seasons.')
                if db.scalar(select(SellerCreditAccount.id).where(
                    (SellerCreditAccount.item_id == item.id) | (SellerCreditAccount.group_id == item.purchase_group_id))):
                    raise HTTPException(422, 'This group has seller-credit history. Reuse its credit explicitly; do not move its products out of the audit trail.')
                if group.shipment_id and item.shipment_id != group.shipment_id:
                    raise HTTPException(422, 'Cannot add products to an already dispatched/imported shipment group. Create a new group.')
                audit(db, 'OrderItem', item.id, f'Group {item.purchase_group_id} → {group.id}; group cost shares recalculate.')
                item.purchase_group_id = group.id
            if state:
                audit(db, 'OrderItem', item.id, f'Worksheet status {item.worksheet_status} → {state}. Credit preserved.')
                item.worksheet_status = state
        db.commit()
        return RedirectResponse('/worksheet', 303)

    @app.get('/groups')
    def groups_page(request: Request, db=Depends(get_db)):
        return templates.TemplateResponse(request, 'purchase_groups.html', context(request, **worksheet_data(db),
            distributors=db.scalars(select(Distributor).order_by(Distributor.name)).all(),
            seasons=db.scalars(select(Season).order_by(Season.id.desc())).all()))

    @app.post('/distributors')
    def create_distributor(name: str = Form(...), contact: str = Form(''), db=Depends(get_db)):
        name = name.strip()
        if not name or len(name) > 200:
            raise HTTPException(422, 'Enter a distributor name (up to 200 characters).')
        if any(d.name.casefold() == name.casefold() for d in db.scalars(select(Distributor)).all()):
            raise HTTPException(422, 'This distributor already exists.')
        db.add(Distributor(name=name, contact=contact.strip()[:500] or None))
        db.commit()
        return RedirectResponse('/groups', 303)

    @app.post('/groups')
    def create_group(name: str = Form(...), season_id: int = Form(...), seller: str = Form(''),
                     distributor_id: str = Form(''), db=Depends(get_db)):
        find(db, Season, season_id)
        if not name.strip() or len(name) > 200:
            raise HTTPException(422, 'Enter a group name (up to 200 characters).')
        distributor = find(db, Distributor, int(distributor_id)) if distributor_id.isdigit() else None
        group = PurchaseGroup(name=name.strip(), season_id=season_id, seller=seller.strip()[:200] or None,
                              distributor_id=distributor.id if distributor else None)
        db.add(group)
        db.flush()
        audit(db, 'PurchaseGroup', group.id, f'Created group {group.name}.')
        db.commit()
        return RedirectResponse(f'/groups#group-{group.id}', 303)

    @app.post('/groups/{group_id}')
    def edit_group(group_id: int, name: str = Form(...), seller: str = Form(''), distributor_id: str = Form(''),
                   special_cost: str = Form('0'), cost_method: str = Form('value'),
                   initial_percentage: str = Form('50'), shipment_percentage: str = Form('50'),
                   transport_percentage: str = Form('5'), notes: str = Form(''), db=Depends(get_db)):
        group = find(db, PurchaseGroup, group_id)
        distributor = find(db, Distributor, int(distributor_id)) if distributor_id.isdigit() else None
        distributor_key = distributor.id if distributor else None
        seller = seller.strip()[:200] or None
        if (group.distributor_id, group.seller) != (distributor_key, seller) and db.scalar(
            select(SellerCreditAccount.id).where(SellerCreditAccount.group_id == group.id)):
            raise HTTPException(422, 'Seller/distributor identity is locked after a credit history exists.')
        if not name.strip() or len(name) > 200 or cost_method not in {'value','quantity','equal'}:
            raise HTTPException(422, 'Check group name and cost allocation method.')
        initial = amount_value(initial_percentage, 'Initial percentage')
        shipment = amount_value(shipment_percentage, 'Shipment percentage')
        transport = amount_value(transport_percentage, 'Transport percentage')
        if initial + shipment != 100 or transport > 100:
            raise HTTPException(422, 'Merchandise stages must add to 100%; transport must be 0–100%.')
        group.name, group.seller, group.distributor_id = name.strip(), seller, distributor_key
        group.special_cost = amount_value(special_cost, 'Special cost')
        group.cost_method = cost_method
        group.initial_percentage, group.shipment_percentage, group.transport_percentage = initial/100, shipment/100, transport/100
        group.notes = notes.strip() or None
        audit(db, 'PurchaseGroup', group.id, f'Saved group: {initial}% + ({shipment}% + {transport}% transport); special {group.special_cost}, split by {cost_method}.')
        db.commit()
        return RedirectResponse(f'/groups#group-{group.id}', 303)

    @app.post('/groups/{group_id}/dispatch')
    def dispatch_group(group_id: int, shipment_date: date = Form(...), db=Depends(get_db)):
        group = find(db, PurchaseGroup, group_id)
        if not group.items or group.shipment_id:
            raise HTTPException(422, 'Choose a non-empty group that has not already been dispatched.')
        if not group.distributor_id or not group.seller:
            raise HTTPException(422, 'Assign a distributor and seller first.')
        if any(i.shipment_id or i.status == 'Cancelled' or i.worksheet_status != 'process' for i in group.items):
            raise HTTPException(422, 'Every product must be in process, active, and not already in a shipment.')
        batch = ReadyBatch(season_id=group.season_id, name=f'Group {group.id} ready', ready_date=shipment_date)
        shipment = Shipment(name=f'Group {group.id} · {group.name}', season_id=group.season_id,
                            date=shipment_date, status='In transit', use_rule_transport=False,
                            transport_percentage=group.transport_percentage)
        db.add_all([batch, shipment])
        db.flush()
        group.shipment_id = shipment.id
        for item in group.items:
            item.ready_batch_id = batch.id
            item.status = 'In transit'
            db.add(ShipmentItem(shipment_id=shipment.id, order_item_id=item.id))
        audit(db, 'PurchaseGroup', group.id, 'Dispatched whole group. Prepayment remains held until explicitly settled; items remain yellow until marked done.')
        db.commit()
        return RedirectResponse(f'/groups#group-{group.id}', 303)

    @app.get('/credits')
    def credits_page(request: Request, db=Depends(get_db)):
        return templates.TemplateResponse(request, 'seller_credits.html', context(request, **worksheet_data(db),
            entries=db.scalars(select(SellerCreditEntry).order_by(SellerCreditEntry.id.desc())).all(),
            customers=db.scalars(select(Customer).order_by(Customer.code)).all(), today=date.today(), token=secrets.token_hex(16)))

    @app.post('/credits/prepay')
    def record_prepay(group_id: int = Form(...), item_id: str = Form(''), customer_id: str = Form(''),
                      amount: str = Form(...), stage: str = Form(...), payment_date: date = Form(...),
                      reference: str = Form(...), token: str = Form(...), db=Depends(get_db)):
        lock_ledger(db)
        key = 'prepay:'+token[:64]
        if db.scalar(select(SellerCreditEntry.id).where(SellerCreditEntry.operation_key == key)):
            return RedirectResponse('/credits', 303)
        group = find(db, PurchaseGroup, group_id)
        if not group.distributor_id or not group.seller:
            raise HTTPException(422, 'Set the distributor and seller on the group before recording a payment.')
        if stage not in {'initial','shipment','other'} or not reference.strip() or not token.strip():
            raise HTTPException(422, 'Select the payment stage and enter its bank/reference note.')
        item = find(db, OrderItem, int(item_id)) if item_id.isdigit() else None
        if item and not item.customer_id:
            raise HTTPException(422, 'Resolve the product customer before attaching money to it.')
        owner = find(db, Customer, int(customer_id)) if customer_id.isdigit() else None
        if owner and item and owner.id != item.customer_id:
            raise HTTPException(422, 'Selected customer does not own this product.')
        if owner and not any(i.customer_id == owner.id for i in group.items):
            raise HTTPException(422, 'The customer has no products in this group.')
        value = amount_value(amount, positive=True)
        try:
            account = get_account(db, group, item, owner.id if owner else None)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        db.add(SellerCreditEntry(account_id=account.id, amount=value, kind='prepayment', stage=stage,
                                date=payment_date, reference=reference.strip()[:500], operation_key=key))
        audit(db, 'SellerCreditAccount', account.id, f'Recorded {value} {group.currency} seller prepayment ({stage}). {reference}')
        db.commit()
        return RedirectResponse('/credits', 303)

    @app.post('/credits/transfer')
    def transfer(source_id: int = Form(...), target_group_id: int = Form(...), amount: str = Form(...),
                 reference: str = Form(...), token: str = Form(...), db=Depends(get_db)):
        lock_ledger(db)
        key = 'transfer:'+token[:64]
        if db.scalar(select(SellerCreditEntry.id).where(SellerCreditEntry.operation_key == key+':out')):
            return RedirectResponse('/credits', 303)
        if not reference.strip() or not token.strip():
            raise HTTPException(422, 'A reference is required.')
        source = find(db, SellerCreditAccount, source_id)
        target = find(db, PurchaseGroup, target_group_id)
        try:
            transfer_credit(db, source, target, amount_value(amount, positive=True), reference.strip()[:500], key)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        db.commit()
        return RedirectResponse('/credits', 303)

    @app.post('/credits/settle')
    def settle(account_id: int = Form(...), amount: str = Form(...), kind: str = Form(...),
               reference: str = Form(...), token: str = Form(...), db=Depends(get_db)):
        lock_ledger(db)
        key = 'settle:'+token[:64]
        if db.scalar(select(SellerCreditEntry.id).where(SellerCreditEntry.operation_key == key)):
            return RedirectResponse('/credits', 303)
        account = find(db, SellerCreditAccount, account_id)
        value = amount_value(amount, positive=True)
        if kind not in {'settlement','refund'} or not reference.strip() or not token.strip():
            raise HTTPException(422, 'Choose settlement/refund and enter a reference.')
        if value > account_balance(account):
            raise HTTPException(422, 'Amount exceeds the money held by the seller.')
        db.add(SellerCreditEntry(account_id=account.id, amount=-value, kind=kind, date=date.today(),
                                reference=reference.strip()[:500], operation_key=key))
        audit(db, 'SellerCreditAccount', account.id, f'{kind}: {value}; {reference}')
        db.commit()
        return RedirectResponse('/credits', 303)
