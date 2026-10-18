"""Worksheet grouping and an explicit, auditable seller-credit ledger.

Customer receipts are deliberately not guessed to be seller prepayments.
All money is Decimal; a transfer is a pair of entries in one transaction.
"""
from collections import Counter
from datetime import date
from decimal import Decimal, ROUND_FLOOR
from types import SimpleNamespace
from xml.etree import ElementTree
import colorsys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.calculations import ZERO, money, item_amounts
from app.models import (Order, OrderItem, PurchaseGroup, SellerCreditAccount,
                        SellerCreditEntry, AuditLog)

STATUS_LABELS = {"process": "In process", "done": "Done", "pending": "Not completed"}


def status_of(item):
    if item.worksheet_status in STATUS_LABELS:
        return item.worksheet_status
    if item.status == "Cancelled":
        return "pending"
    if item.status in {"Delivered", "Arrived"}:
        return "done"
    return "process" if item.status in {"Confirmed", "Production", "Ready", "Assigned to shipment", "In transit"} else "pending"


def source_status(book, sheet, row, columns, shipment=False):
    """Read actual product-cell fills, including the workbook's green theme shades."""
    theme = []
    if book.loaded_theme:
        ns = {"a": "http://schemas.openxmlformats.org/drawingml/2006/main"}
        scheme = ElementTree.fromstring(book.loaded_theme).find('.//a:clrScheme', ns)
        if scheme is not None:
            theme = [next(iter(c)).get('val') or next(iter(c)).get('lastClr') for c in scheme]
            theme = [next(iter(c)).get('lastClr', v) for c, v in zip(scheme, theme)]
            # Spreadsheet theme indices put light1 before dark1, then light2/dark2.
            theme[:4] = [theme[1], theme[0], theme[3], theme[2]]
    votes, fills = [], []
    for column in set(columns):
        if not column:
            continue
        cell = sheet[f'{column}{row}']
        if cell.value is None:
            continue
        color = cell.fill.fgColor
        rgb = None
        if cell.fill.patternType == 'solid':
            if color.type == 'rgb':
                rgb = color.rgb[-6:]
            elif color.type == 'theme' and color.theme < len(theme):
                rgb = theme[color.theme]
        if not rgb or len(rgb) != 6:
            votes.append('pending')
            continue
        fills.append(rgb)
        r,g,b = (int(rgb[i:i+2],16)/255 for i in (0,2,4))
        hue, saturation, _ = colorsys.rgb_to_hsv(r,g,b)
        hue *= 360
        if saturation > .2 and 65 <= hue <= 170:
            votes.append('done')
        elif saturation > .3 and 45 <= hue < 65:
            votes.append('process')
        elif saturation < .12:
            votes.append('pending')
        else:
            votes.append('other')
    counts = Counter(votes)
    # A colored price cell is meaningful even when description cells are unfilled.
    recognized = [v for v in votes if v in {'process', 'done'}]
    chosen = Counter(recognized).most_common(1)[0][0] if recognized else ('done' if shipment and counts['other'] else 'pending')
    note = None
    if counts['other']:
        note = 'Additional source color; status uses shipment evidence where present. Check original row.'
    if chosen == 'pending' and shipment:
        note = 'White source row also has a shipment allocation. Kept white; review the source conflict.'
    if chosen == 'process' and shipment:
        note = 'Yellow/amber source row also has a shipment allocation. Kept in process; confirm completion separately.'
    return chosen, ','.join(sorted(set(fills)))[:40], note


def split_cost(total, items, method='value'):
    """Largest-remainder allocation: shares always add to the exact group cost."""
    if not items:
        return {}
    weights = [max(item_amounts(i).final, ZERO) if method == 'value' else Decimal(i.quantity if method == 'quantity' else 1) for i in items]
    denominator = sum(weights, ZERO)
    if not denominator:
        weights, denominator = [Decimal(1)] * len(items), Decimal(len(items))
    cents = int(money(total) * 100)
    exact = [Decimal(cents) * weight / denominator for weight in weights]
    floors = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact]
    for index in sorted(range(len(items)), key=lambda i: (exact[i] - floors[i], -items[i].id), reverse=True)[:cents - sum(floors)]:
        floors[index] += 1
    return {item.id: Decimal(value)/100 for item, value in zip(items, floors)}


def group_amounts(group):
    items = sorted(group.items, key=lambda i:i.id)
    value = money(sum((item_amounts(i).final for i in items), ZERO))
    special = money(group.special_cost or ZERO)
    initial = money(value * group.initial_percentage)
    balance = money(value * group.shipment_percentage)
    # Transport is an advance at the shipment stage, not another later charge.
    transport = money(sum((item_amounts(i).final * group.transport_percentage for i in items
                           if not i.customer or (i.customer.country_code or '').upper() != 'CH'), ZERO))
    return SimpleNamespace(value=value, initial=initial, shipment=balance+transport,
                           transport=transport, total=money(value+transport+special),
                           special=special, shares=split_cost(special, items, group.cost_method))


def account_balance(account):
    return money(sum((entry.amount for entry in account.entries), ZERO))


def reusable(account):
    if account.item:
        return status_of(account.item) == 'pending'
    return bool(account.group.items) and all(status_of(item) == 'pending' for item in account.group.items)


def audit(db, entity, identity, description):
    db.add(AuditLog(entity_type=entity, entity_id=str(identity), description=description, source_user='Worksheet'))


def lock_ledger(db):
    # SQLite has no SELECT FOR UPDATE; start a reserved write transaction before
    # reading balances so concurrent transfers cannot both spend the same funds.
    if db.get_bind().dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')


def get_account(db, group, item=None, customer_id=None):
    if item and item.purchase_group_id != group.id:
        raise ValueError('Product does not belong to the selected group.')
    owner = item.customer_id if item else customer_id
    account = db.scalar(select(SellerCreditAccount).where(
        SellerCreditAccount.group_id == group.id,
        SellerCreditAccount.item_id == (item.id if item else None),
        SellerCreditAccount.customer_id == owner))
    if account is None:
        account = SellerCreditAccount(group_id=group.id, item_id=item.id if item else None, customer_id=owner)
        db.add(account)
        db.flush()
    return account


def transfer_credit(db, source, target_group, amount, reference, key, target_item=None):
    amount = money(amount)
    if amount <= ZERO or amount > account_balance(source):
        raise ValueError('Transfer exceeds the available seller credit.')
    if not reusable(source):
        raise ValueError('Only credit for white, not-completed products can be reused. For mixed groups, record credit against the specific product.')
    original = source.group
    if not original.distributor_id or not original.seller:
        raise ValueError('Set the seller and distributor before reusing credit.')
    if (original.distributor_id, original.seller.casefold(), original.currency) != (target_group.distributor_id, (target_group.seller or '').casefold(), target_group.currency):
        raise ValueError('Credit must stay with the same seller, distributor and currency.')
    if target_item and target_item.customer_id != source.customer_id:
        raise ValueError('Credit cannot move between customers.')
    if source.customer_id and not any(i.customer_id == source.customer_id for i in target_group.items):
        raise ValueError('The target group has no products for this customer.')
    target = get_account(db, target_group, target_item, source.customer_id)
    if target.id == source.id:
        raise ValueError('Choose a different target account or group.')
    for account, signed, kind, suffix in [(source, -amount, 'transfer out', 'out'), (target, amount, 'transfer in', 'in')]:
        db.add(SellerCreditEntry(account_id=account.id, amount=signed, kind=kind, date=date.today(),
                                reference=reference, operation_key=key+':'+suffix))
    audit(db, 'SellerCreditAccount', source.id, f'Transferred {amount} {original.currency} to account {target.id}. {reference}')


def ensure_import_groups(db):
    """Non-destructive grouping; do not invent distributor names or payments."""
    items = db.scalars(select(OrderItem).where(OrderItem.purchase_group_id.is_(None))).all()
    groups = {}
    for item in items:
        shipment_id = item.shipment_id
        name = item.shipment.name if shipment_id else f'{item.order.brand.name} · {STATUS_LABELS[status_of(item)]}'
        key = (item.order.season_id, name, shipment_id)
        if key not in groups:
            group = db.scalar(select(PurchaseGroup).where(PurchaseGroup.season_id == key[0], PurchaseGroup.name == name,
                                                       PurchaseGroup.shipment_id == shipment_id))
            if group is None:
                group = PurchaseGroup(season_id=key[0], name=name, shipment_id=shipment_id,
                                      notes='Imported grouping. Seller and distributor require confirmation; no seller payment inferred.')
                db.add(group)
                db.flush()
            groups[key] = group
        item.purchase_group_id = groups[key].id
    return len(items)


def worksheet_data(db):
    items = db.scalars(select(OrderItem).options(
        selectinload(OrderItem.customer), selectinload(OrderItem.order).selectinload(Order.brand),
        selectinload(OrderItem.order).selectinload(Order.season),
        selectinload(OrderItem.shipment_link),
        selectinload(OrderItem.purchase_group).selectinload(PurchaseGroup.distributor),
    ).order_by(OrderItem.id)).all()
    groups = db.scalars(select(PurchaseGroup).options(selectinload(PurchaseGroup.items),
                       selectinload(PurchaseGroup.distributor)).order_by(PurchaseGroup.id)).all()
    accounts = db.scalars(select(SellerCreditAccount).options(selectinload(SellerCreditAccount.entries))).all()
    shares = {}
    amounts = {}
    for group in groups:
        amounts[group.id] = group_amounts(group)
        shares.update(amounts[group.id].shares)
    balances = {a.id: account_balance(a) for a in accounts}
    item_credits = {}
    for account in accounts:
        if account.item_id:
            item_credits[account.item_id] = item_credits.get(account.item_id, ZERO) + balances[account.id]
    rows = [SimpleNamespace(item=i, state=status_of(i), value=item_amounts(i).final,
            special=shares.get(i.id, ZERO), credit=item_credits.get(i.id, ZERO),
            credit_recorded=i.id in item_credits) for i in items]
    return dict(rows=rows, groups=groups, group_amounts=amounts, accounts=accounts, balances=balances,
                held=money(sum(balances.values(), ZERO)),
                available=money(sum((balances[a.id] for a in accounts if reusable(a)), ZERO)),
                status_labels=STATUS_LABELS, reusable=reusable,
                counts=Counter(row.state for row in rows), total_items=len(items),
                units=sum(i.quantity for i in items))
