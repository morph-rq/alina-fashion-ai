"""Validated business actions shared by the screens and the AI assistant.

Every consequential change goes through ``preview_actions`` (applied inside a transaction
that is always rolled back, so the before/after figures come from the same code that will
commit) and then ``commit_actions`` (one transaction, audited, with a settlement snapshot).
Nothing here trusts free text: names resolve to existing records or the action is refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_change
from app.calculations import ZERO, money
from app.factory_ledger import compare_snapshots, record_snapshot, season_ledger, settlement_summary
from app.models import (Brand, CreditApplication, Customer, CustomerAlias, CustomerPricingRule, FactoryAgreement,
                        FactoryPayment, FactoryRatePortion, Order, OrderItem, Payment, Season)
from app.settlement import rate as parse_rate

ITEM_STATUSES = ("Ordered", "Confirmed", "Production", "Ready", "Assigned to shipment", "In transit", "Arrived",
                 "Delivered", "Cancelled")
STATUS_RU = {"Ordered": "Заказано", "Confirmed": "Подтверждено", "Production": "В производстве", "Ready": "Готово",
             "Assigned to shipment": "В отгрузке", "In transit": "В пути", "Arrived": "Прибыло", "Delivered": "Доставлено",
             "Cancelled": "Отменено"}
LOCKED_STATUSES = {"Assigned to shipment", "In transit", "Arrived", "Delivered"}


class ActionError(ValueError):
    pass


def _money(value: Any, label: str) -> Decimal:
    try:
        text = str(value).replace("€", "").replace(" ", "").replace(" ", "")
        if "," in text and "." not in text:
            text = text.replace(",", ".")
        amount = Decimal(text)
    except (InvalidOperation, TypeError):
        raise ActionError(f"{label}: не удалось прочитать сумму «{value}».") from None
    if not amount.is_finite() or amount != money(amount):
        raise ActionError(f"{label}: сумма должна быть в евро с точностью до цента.")
    return money(amount)


def _pct(value: Any) -> Decimal:
    try:
        return parse_rate(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ActionError(f"Не удалось прочитать процент «{value}».") from None


def find_customer(db: Session, text: str | None) -> Customer:
    if not text:
        raise ActionError("Не указан клиент.")
    key = str(text).strip()
    norm = key.casefold()
    customer = db.scalar(select(Customer).where(func.lower(Customer.code) == norm))
    if customer is None:
        alias = db.scalar(select(CustomerAlias).where(func.lower(CustomerAlias.alias) == norm))
        customer = alias.customer if alias else None
    if customer is None:
        # Names in Russian sentences change their ending ("Алине", "Алины"): compare transliterated stems.
        from app.factory_import import HOMOGLYPHS, TRANSLIT
        def latin(value: str) -> str:
            upper = value.upper().translate(HOMOGLYPHS) if re.fullmatch(r"[А-ЯЁA-Z]{2,4}", value.upper()) else value.upper()
            return "".join(TRANSLIT.get(ch, ch) for ch in upper)
        stem = re.sub(r"(YA|YU|OY|OI|EY|[AEIOUY])$", "", latin(key))
        matches = []
        if len(stem) >= 3:
            for c in db.scalars(select(Customer)):
                names = [c.code, c.name or ""] + [a.alias for a in c.aliases]
                if any(n and (latin(n) == latin(key) or (len(stem) >= 4 and latin(n).startswith(stem))) for n in names):
                    matches.append(c)
        if len(matches) == 1:
            customer = matches[0]
        elif len(matches) > 1:
            raise ActionError(f"«{key}» подходит нескольким клиентам: {', '.join(c.code for c in matches)}.")
    if customer is None:
        raise ActionError(f"Клиент «{key}» не найден.")
    return customer


def find_season(db: Session, name: str | None) -> Season:
    if name:
        season = db.scalar(select(Season).where(func.lower(Season.name) == str(name).strip().casefold()))
        if season is None:
            raise ActionError(f"Сезон «{name}» не найден.")
        return season
    seasons = list(db.scalars(select(Season).order_by(Season.id.desc())))
    with_orders = [s for s in seasons if s.orders]
    if not with_orders:
        raise ActionError("В базе ещё нет сезонов с заказами.")
    return with_orders[0]


def find_order(db: Session, season: Season, brand: str | None, order_id: int | None = None) -> Order:
    if order_id:
        order = db.get(Order, int(order_id))
        if order is None:
            raise ActionError("Заказ не найден.")
        return order
    if not brand:
        raise ActionError("Не указан бренд/заказ.")
    norm = re.sub(r"\s+", " ", str(brand)).strip().casefold()
    orders = [o for o in season.orders if o.brand.name.casefold().strip() == norm]
    if not orders:
        orders = [o for o in season.orders if norm in o.brand.name.casefold()]
    if len(orders) != 1:
        names = ", ".join(o.brand.name for o in season.orders) or "нет заказов"
        raise ActionError(f"Бренд «{brand}» не определён однозначно в {season.name}. Доступно: {names}.")
    return orders[0]


def _items(db: Session, ids) -> list[OrderItem]:
    items = []
    for item_id in ids or []:
        item = db.get(OrderItem, int(item_id))
        if item is None:
            raise ActionError(f"Позиция {item_id} не найдена.")
        items.append(item)
    if not items:
        raise ActionError("Не выбраны позиции.")
    return items


def _audit(db, entity, entity_id, field_name, before, after, actor, description):
    record_change(db, entity_type=entity, entity_id=entity_id, field=field_name, previous_value=before,
                  new_value=after, source_user=actor, description=description)


def _touch(item: OrderItem) -> None:
    """The row's revision is SQLAlchemy's version counter: it advances on every update by itself."""


# --- Actions ------------------------------------------------------------------------

def rule_scope(db: Session, params: dict) -> tuple[Customer, Season | None, Brand | None, Order | None]:
    """Customer + optional season / brand (within the season) / single order."""
    customer = find_customer(db, params.get("customer"))
    season = brand = order = None
    if params.get("order_id"):
        order = find_order(db, None, None, params["order_id"])
        return customer, order.season, order.brand, order
    if params.get("season") or params.get("brand"):
        season = find_season(db, params.get("season"))
    if params.get("brand"):
        brand = find_order(db, season, params["brand"]).brand
    return customer, season, brand, order


def act_set_customer_markup(db: Session, p: dict, actor: str) -> str:
    customer, season, brand, order = rule_scope(db, p)
    markup = _pct(p.get("markup"))
    if customer.is_owner:
        raise ActionError(f"{customer.code} — это вы: ваши позиции всегда стоят по цене фабрики. Измените условия фабрики.")
    def same(column, record):
        return column == record.id if record is not None else column.is_(None)
    rule = db.scalar(select(CustomerPricingRule).where(
        CustomerPricingRule.customer_id == customer.id, same(CustomerPricingRule.season_id, None if order else season),
        same(CustomerPricingRule.brand_id, None if order else brand), same(CustomerPricingRule.order_id, order)))
    before = rule.markup if rule else None
    if rule is None:
        rule = CustomerPricingRule(customer_id=customer.id, season_id=None if order or season is None else season.id,
                                   brand_id=None if order or brand is None else brand.id,
                                   order_id=order.id if order else None, markup=markup)
        db.add(rule)
    rule.markup = markup
    db.flush()
    scope = (f"{order.brand.name} (заказ)" if order else f"{brand.name} {season.name}" if brand
             else season.name if season else "все заказы")
    _audit(db, "CustomerPricingRule", rule.id, "markup", before, markup, actor, f"Наценка {customer.code} ({scope})")
    changed = skipped = 0
    for item in db.scalars(select(OrderItem).where(OrderItem.customer_id == customer.id)):
        if item.status == "Cancelled" or not _rule_applies(item, season, brand, order):
            continue
        if effective_rule(db, item) is not rule:
            continue
        if item.markup_source == "manual" or item.selling_unit_price is not None:
            skipped += 1
            continue
        if item.status in LOCKED_STATUSES and not p.get("include_shipped"):
            skipped += 1
            continue
        if Decimal(item.markup_percentage or 0) != markup:
            _audit(db, "OrderItem", item.id, "markup_percentage", item.markup_percentage, markup, actor,
                   f"Наценка клиента {customer.code} по правилу")
            item.markup_percentage = markup
            _touch(item)
            changed += 1
        item.markup_source = "rule"
    return f"{customer.code}: {markup * 100:.2f}% для «{scope}» — изменено позиций: {changed}" + (f", пропущено (ручная цена/отгружено): {skipped}" if skipped else "")


def _rule_applies(item: OrderItem, season, brand, order) -> bool:
    if order is not None:
        return item.order_id == order.id
    if season is not None and item.order.season_id != season.id:
        return False
    if brand is not None and item.order.brand_id != brand.id:
        return False
    return True


def effective_rule(db: Session, item: OrderItem) -> CustomerPricingRule | None:
    """Most specific rule: order > brand+season > season > brand > customer default."""
    if item.customer_id is None:
        return None
    rules = list(db.scalars(select(CustomerPricingRule).where(CustomerPricingRule.customer_id == item.customer_id)))
    order = item.order

    def score(rule):
        if rule.order_id is not None:
            return 5 if rule.order_id == order.id else -1
        if rule.season_id is not None and rule.season_id != order.season_id:
            return -1
        if rule.brand_id is not None and rule.brand_id != order.brand_id:
            return -1
        return {(True, True): 4, (True, False): 3, (False, True): 2, (False, False): 1}[(rule.season_id is not None, rule.brand_id is not None)]
    ranked = sorted(((score(r), r.id, r) for r in rules), key=lambda t: (t[0], t[1]), reverse=True)
    return ranked[0][2] if ranked and ranked[0][0] > 0 else None


def act_set_factory_rate(db: Session, p: dict, actor: str) -> str:
    season = find_season(db, p.get("season"))
    order = find_order(db, season, p.get("brand"), p.get("order_id"))
    portions = p.get("portions") or [{"rate": p.get("rate"), "share": 1}]
    parsed = []
    for index, portion in enumerate(portions):
        rate = _pct(portion.get("rate"))
        share = portion.get("share")
        share = _pct(share) if share not in (None, "") else None
        section = (portion.get("section") or None)
        if share is None and not section:
            share = Decimal("1")
        parsed.append(FactoryRatePortion(position=index, label=str(portion.get("label") or (section or f"часть {index + 1}"))[:100],
                                         rate=rate, share=share, section=section, rounding=portion.get("rounding") or "cent"))
    shares = [x.share for x in parsed if x.share is not None]
    if shares and sum(shares) <= 0:
        raise ActionError("Доли частей должны быть больше нуля.")
    agreement = db.scalar(select(FactoryAgreement).where(FactoryAgreement.order_id == order.id))
    before = describe_agreement(agreement)
    if agreement is None:
        agreement = FactoryAgreement(order_id=order.id)
        db.add(agreement)
    agreement.portions.clear()
    db.flush()
    agreement.portions.extend(parsed)
    if p.get("deposit_rate") not in (None, ""):
        agreement.deposit_rate = _pct(p.get("deposit_rate"))
    agreement.meaning = "markup"
    agreement.confirmed = True
    db.flush()
    after = describe_agreement(agreement)
    _audit(db, "FactoryAgreement", order.id, "portions", before, after, actor, f"Условия фабрики {order.brand.name}")
    return f"Фабрика {order.brand.name}: {after}"


def describe_agreement(agreement: FactoryAgreement | None) -> str:
    if agreement is None or not agreement.portions:
        return "не заданы"
    parts = []
    for p in agreement.portions:
        rate = f"{Decimal(p.rate) * 100:.2f}".rstrip("0").rstrip(".") + "%"
        if p.section:
            parts.append(f"{p.section}: {rate}")
        elif p.share is not None and Decimal(p.share) != 1:
            parts.append(f"{Decimal(p.share) * 100:.0f}% суммы: {rate}")
        else:
            parts.append(rate)
    return ", ".join(parts) + f"; предоплата {Decimal(agreement.deposit_rate) * 100:.0f}%"


def act_set_manual_price(db: Session, p: dict, actor: str) -> str:
    item = _items(db, [p.get("item_id")])[0]
    if p.get("unit_price") in (None, ""):
        before = item.selling_unit_price
        item.selling_unit_price = None
        item.markup_source = "rule"
        _audit(db, "OrderItem", item.id, "selling_unit_price", before, None, actor, "Ручная цена снята")
        _touch(item)
        return f"Ручная цена для {item.name or item.article} снята."
    price = _money(p.get("unit_price"), "Цена")
    if price <= 0:
        raise ActionError("Цена должна быть больше нуля.")
    before = item.selling_unit_price
    item.selling_unit_price = price
    item.markup_source = "manual"
    _touch(item)
    _audit(db, "OrderItem", item.id, "selling_unit_price", before, price, actor, "Согласованная цена клиента")
    return f"{item.name or item.article}: цена клиента €{price} за шт."


def act_set_item_markup(db: Session, p: dict, actor: str) -> str:
    """An agreed markup for one item; customer rules never overwrite it afterwards."""
    item = _items(db, [p.get("item_id")])[0]
    if item.customer is not None and item.customer.is_owner:
        raise ActionError("Ваши позиции идут по цене фабрики — наценка к ним не применяется.")
    markup = _pct(p.get("markup"))
    before = item.markup_percentage
    item.markup_percentage, item.markup_source = markup, "manual"
    _audit(db, "OrderItem", item.id, "markup_percentage", before, markup, actor, "Наценка позиции вручную")
    return f"{item.name or item.article}: наценка {markup * 100:.2f}% (вручную)."


def act_update_item(db: Session, p: dict, actor: str) -> str:
    item = _items(db, [p.get("item_id")])[0]
    changes = []
    for field_name, label in (("size", "размер"), ("color", "цвет"), ("worksheet_note", "заметка")):
        if field_name in p and p[field_name] is not None:
            value = str(p[field_name]).strip()[:300] or None
            if value != getattr(item, field_name):
                _audit(db, "OrderItem", item.id, field_name, getattr(item, field_name), value, actor, f"Изменён {label}")
                setattr(item, field_name, value)
                changes.append(label)
    if p.get("quantity") not in (None, ""):
        quantity = int(p["quantity"])
        if not 1 <= quantity <= 1000:
            raise ActionError("Количество должно быть от 1 до 1000.")
        if quantity != item.quantity:
            if item.status in LOCKED_STATUSES:
                raise ActionError("Количество отгруженной позиции не меняется — сначала уберите её из отгрузки.")
            _audit(db, "OrderItem", item.id, "quantity", item.quantity, quantity, actor, "Изменено количество")
            item.quantity = quantity
            changes.append("количество")
    if p.get("wholesale_unit_price") not in (None, ""):
        price = _money(p["wholesale_unit_price"], "Базовая цена")
        if price != item.wholesale_unit_price:
            _audit(db, "OrderItem", item.id, "wholesale_unit_price", item.wholesale_unit_price, price, actor, "Изменена базовая цена")
            item.wholesale_unit_price = price
            changes.append("базовая цена")
    if not changes:
        return "Нечего менять."
    _touch(item)
    return f"{item.name or item.article}: изменено — {', '.join(changes)}."


def act_move_item(db: Session, p: dict, actor: str) -> str:
    item = _items(db, [p.get("item_id")])[0]
    customer = find_customer(db, p.get("customer"))
    quantity = int(p.get("quantity") or item.quantity)
    if not 1 <= quantity <= item.quantity:
        raise ActionError("Неверное количество для переноса.")
    target = item
    if quantity < item.quantity:
        from app.ready_importer import split_item
        target = split_item(db, item, quantity)
    before = target.customer.code if target.customer else None
    target.customer_id = customer.id
    target.allocation_status = "Allocated"
    if p.get("size"):
        target.size = str(p["size"])[:100]
    rule = effective_rule(db, target)
    if rule is not None and target.markup_source != "manual":
        target.markup_percentage = rule.markup
        target.markup_source = "rule"
    _touch(target)
    _audit(db, "OrderItem", target.id, "customer", before, customer.code, actor, "Позиция перенесена клиенту")
    return f"{target.name or target.article}: {quantity} шт. → {customer.code}."


def act_set_status(db: Session, p: dict, actor: str) -> str:
    status = p.get("status")
    if status not in ITEM_STATUSES:
        raise ActionError(f"Неизвестный статус «{status}».")
    items = _items(db, p.get("item_ids"))
    for item in items:
        if status == "Cancelled" and not (p.get("reason") or "").strip():
            raise ActionError("Укажите причину отмены.")
        if item.status != status:
            _audit(db, "OrderItem", item.id, "status", item.status, status, actor,
                   f"Статус: {STATUS_RU.get(status, status)}" + (f" — {p['reason']}" if p.get("reason") else ""))
            item.status = status
            if status == "Cancelled":
                item.cancel_reason = str(p.get("reason"))[:500]
            _touch(item)
    return f"{len(items)} поз. → {STATUS_RU.get(status, status)}."


def act_record_payment(db: Session, p: dict, actor: str) -> str:
    season = find_season(db, p.get("season"))
    amount = _money(p.get("amount"), "Оплата")
    if amount <= 0:
        raise ActionError("Сумма оплаты должна быть больше нуля.")
    currency = (p.get("currency") or "EUR").upper()
    note = p.get("note")
    if currency != "EUR":
        if not p.get("fx_rate") or not p.get("fx_date"):
            raise ActionError(f"Оплата в {currency}: укажите курс к евро и дату курса — суммы в разных валютах не складываются.")
        fx = Decimal(str(p["fx_rate"]))
        note = f"{amount} {currency} по курсу {fx} от {p['fx_date']}. " + (note or "")
        amount = money(amount * fx)
    when = date.fromisoformat(p["date"]) if p.get("date") else date.today()
    order = find_order(db, season, p.get("brand"), p.get("order_id")) if p.get("brand") or p.get("order_id") else None
    reference = (p.get("reference") or "").strip()[:300] or None
    payer = find_customer(db, p.get("payer"))
    destination = p.get("to") or "factory"
    if destination not in ("factory", "owner"):
        raise ActionError("Куда пришли деньги: фабрике или вам?")
    status = "confirmed" if p.get("confirmed", True) else "expected"
    if reference:
        clash = db.scalar(select(Payment).where(Payment.reference == reference, Payment.amount == amount)) or \
            db.scalar(select(FactoryPayment).where(FactoryPayment.reference == reference, FactoryPayment.amount == amount))
        if clash is not None:
            raise ActionError(f"Оплата с номером «{reference}» на €{amount} уже записана — второй раз не учитываю.")
    if payer.is_owner:
        if destination != "factory":
            raise ActionError("Ваша собственная оплата записывается как перевод фабрике.")
        if status != "confirmed":
            raise ActionError("Ваш перевод записывается, когда он сделан.")
        payment = FactoryPayment(season_id=season.id, order_id=order.id if order else None, date=when, amount=amount,
                                 stage=p.get("stage") or "initial", note=note, reference=reference,
                                 factory_account=p.get("factory_account"))
        db.add(payment)
        db.flush()
        _audit(db, "FactoryPayment", payment.id, "amount", None, amount, actor, "Ваш перевод фабрике")
        return f"Записан ваш перевод фабрике €{amount} ({when:%d.%m.%Y})."
    payment = Payment(season_id=season.id, customer_id=payer.id, order_id=order.id if order else None, date=when,
                      amount=amount, type="payment", note=note, destination=destination, status=status,
                      reference=reference, factory_account=p.get("factory_account"))
    db.add(payment)
    db.flush()
    where = "фабрике" if destination == "factory" else "вам"
    state = "подтверждена" if status == "confirmed" else "ожидается (не учитывается как оплаченная)"
    _audit(db, "Payment", payment.id, "amount", None, amount, actor, f"Оплата {payer.code} {where}: {state}")
    return f"Оплата {payer.code} €{amount} {where} — {state}."


def act_confirm_payment(db: Session, p: dict, actor: str) -> str:
    payment = db.get(Payment, int(p.get("payment_id") or 0))
    if payment is None:
        raise ActionError("Оплата не найдена.")
    if payment.status == "confirmed":
        return "Оплата уже подтверждена."
    payment.status = "confirmed"
    _audit(db, "Payment", payment.id, "status", "expected", "confirmed", actor, "Оплата подтверждена")
    return f"Оплата {payment.customer.code if payment.customer else ''} €{payment.amount} подтверждена."


def act_apply_credit(db: Session, p: dict, actor: str) -> str:
    season = find_season(db, p.get("season"))
    order = find_order(db, season, p.get("brand"), p.get("order_id"))
    amount = _money(p.get("amount"), "Кредит")
    source = (p.get("source") or "").strip()
    if amount <= 0 or not source:
        raise ActionError("Укажите сумму и откуда кредит (например, «переплата SS26»).")
    credit = CreditApplication(order_id=order.id, amount=amount, source=source[:300])
    db.add(credit)
    db.flush()
    _audit(db, "CreditApplication", credit.id, "amount", None, amount, actor, f"Кредит применён к {order.brand.name}: {source}")
    return f"Кредит €{amount} ({source}) применён к {order.brand.name}."


def act_reverse_credit(db: Session, p: dict, actor: str) -> str:
    credit = db.get(CreditApplication, int(p.get("credit_id") or 0))
    if credit is None or credit.reversed_at is not None:
        raise ActionError("Кредит не найден или уже отменён.")
    credit.reversed_at = datetime.now(timezone.utc)
    _audit(db, "CreditApplication", credit.id, "reversed_at", None, credit.reversed_at, actor, "Применение кредита отменено")
    return f"Применение кредита €{credit.amount} отменено."


def act_set_owner(db: Session, p: dict, actor: str) -> str:
    customer = find_customer(db, p.get("customer"))
    for other in db.scalars(select(Customer).where(Customer.is_owner.is_(True))):
        if other.id != customer.id:
            other.is_owner = False
    if not customer.is_owner:
        customer.is_owner = True
        _audit(db, "Customer", customer.id, "is_owner", False, True, actor, "Отмечен владелец бизнеса")
    return f"{customer.code} отмечен(а) как владелец: её позиции идут по цене фабрики."


def act_resolve_allocation(db: Session, p: dict, actor: str) -> str:
    """Split an unallocated row into customer allocations: [{customer, quantity, size}]."""
    item = _items(db, [p.get("item_id")])[0]
    parts = p.get("assignments") or []
    if not parts:
        raise ActionError("Укажите, кому относится изделие.")
    total = sum(int(x.get("quantity") or 1) for x in parts)
    if total != item.quantity:
        raise ActionError(f"Распределено {total} шт., а в строке {item.quantity} шт.")
    from app.ready_importer import split_item
    remaining = item
    results = []
    for index, part in enumerate(parts):
        quantity = int(part.get("quantity") or 1)
        target = remaining if index == len(parts) - 1 else split_item(db, remaining, quantity)
        customer = find_customer(db, part.get("customer"))
        target.customer_id = customer.id
        target.customer = customer
        target.allocation_status = "Allocated"
        if part.get("size"):
            target.size = str(part["size"])[:100]
        rule = effective_rule(db, target)
        if rule is not None and target.markup_source != "manual":
            target.markup_percentage = rule.markup
            target.markup_source = "rule"
        _touch(target)
        _audit(db, "OrderItem", target.id, "customer", None, customer.code, actor, "Распределение подтверждено оператором")
        results.append(f"{customer.code} {quantity} шт." + (f" (р. {target.size})" if target.size else ""))
    from app.models import AllocationReview
    for review in db.scalars(select(AllocationReview).where(AllocationReview.status == "Open",
                                                            AllocationReview.source_file == (item.source_file or ""),
                                                            AllocationReview.row == item.source_row,
                                                            AllocationReview.sheet == (item.source_sheet or ""))):
        review.status = "Resolved"
        review.resolution_note = "; ".join(results)
        review.resolved_at = datetime.now(timezone.utc)
    return f"{item.name or item.article}: " + ", ".join(results)


# --- Undo ("отмени последнее изменение") ------------------------------------------------

UNDOABLE = {"set_customer_markup", "set_item_markup", "set_manual_price", "update_item", "set_status", "set_factory_rate",
            "restore_state"}
ITEM_FIELDS = ("markup_percentage", "markup_source", "selling_unit_price", "status", "cancel_reason", "size", "quantity",
               "color", "worksheet_note")


def _plain(value):
    return str(value) if isinstance(value, Decimal) else value


def capture_state(db: Session, actions: list[dict]) -> dict | None:
    """Everything the undoable actions can change, for the seasons they touch. None when undo is not possible."""
    if not actions or any(a.get("action") not in UNDOABLE for a in actions):
        return None
    seasons = _affected_seasons(db, actions)
    if not seasons:
        return None
    orders = [o for s in seasons for o in s.orders]
    items = {str(i.id): {f: _plain(getattr(i, f)) for f in ITEM_FIELDS} for o in orders for i in o.items}
    customer_ids = sorted({i.customer_id for o in orders for i in o.items if i.customer_id})
    rules = [{"id": r.id, "customer_id": r.customer_id, "season_id": r.season_id, "brand_id": r.brand_id, "order_id": r.order_id,
              "markup": _plain(r.markup), "deposit_rate": _plain(r.deposit_rate), "rounding": r.rounding}
             for r in db.scalars(select(CustomerPricingRule).where(CustomerPricingRule.customer_id.in_(customer_ids or [0])))]
    agreements = []
    for order in orders:
        agreement = db.scalar(select(FactoryAgreement).where(FactoryAgreement.order_id == order.id))
        if agreement is not None:
            agreements.append({"order_id": order.id, "deposit_rate": _plain(agreement.deposit_rate), "confirmed": agreement.confirmed,
                               "portions": [{"label": p.label, "rate": _plain(p.rate), "section": p.section, "share": _plain(p.share),
                                             "rounding": p.rounding, "position": p.position} for p in agreement.portions]})
    return {"season": seasons[0].name, "items": items, "customers": customer_ids, "rules": rules, "agreements": agreements}


def act_restore_state(db: Session, p: dict, actor: str) -> str:
    changed = 0
    for item_id, fields in (p.get("items") or {}).items():
        item = db.get(OrderItem, int(item_id))
        if item is None:
            continue
        touched = False
        for name, value in fields.items():
            if name not in ITEM_FIELDS:
                continue
            current = getattr(item, name)
            target = Decimal(value) if isinstance(current, Decimal) and value is not None else value
            if (Decimal(current) if isinstance(current, Decimal) else current) != target:
                _audit(db, "OrderItem", item.id, name, current, target, actor, "Отмена последнего изменения")
                setattr(item, name, target)
                touched = True
        changed += touched
    keep = {r["id"] for r in p.get("rules") or []}
    customers = p.get("customers") or []
    rules_changed = 0
    for rule in list(db.scalars(select(CustomerPricingRule).where(CustomerPricingRule.customer_id.in_(customers or [0])))):
        if rule.id not in keep:
            _audit(db, "CustomerPricingRule", rule.id, "markup", rule.markup, None, actor, "Отмена: правило наценки удалено")
            db.delete(rule)
            rules_changed += 1
    for saved in p.get("rules") or []:
        rule = db.get(CustomerPricingRule, saved["id"])
        if rule is None:
            rule = CustomerPricingRule(customer_id=saved["customer_id"], season_id=saved["season_id"], brand_id=saved["brand_id"],
                                       order_id=saved["order_id"], markup=Decimal(saved["markup"]))
            db.add(rule)
            rules_changed += 1
        if Decimal(rule.markup) != Decimal(saved["markup"]):
            _audit(db, "CustomerPricingRule", rule.id, "markup", rule.markup, saved["markup"], actor, "Отмена последнего изменения")
            rule.markup = Decimal(saved["markup"])
            rules_changed += 1
        rule.deposit_rate = Decimal(saved["deposit_rate"]) if saved.get("deposit_rate") is not None else None
        rule.rounding = saved.get("rounding") or "cent"
    terms = 0
    for saved in p.get("agreements") or []:
        agreement = db.scalar(select(FactoryAgreement).where(FactoryAgreement.order_id == saved["order_id"]))
        if agreement is None:
            continue
        now = [(str(x.rate), x.section, str(x.share) if x.share is not None else None) for x in agreement.portions]
        then = [(str(Decimal(x["rate"])), x["section"], str(Decimal(x["share"])) if x["share"] is not None else None) for x in saved["portions"]]
        if now != then or Decimal(agreement.deposit_rate) != Decimal(saved["deposit_rate"]):
            for portion in list(agreement.portions):
                db.delete(portion)
            db.flush()
            for x in saved["portions"]:
                db.add(FactoryRatePortion(agreement_id=agreement.id, position=x["position"], label=x["label"], rate=Decimal(x["rate"]),
                                          section=x["section"], share=Decimal(x["share"]) if x["share"] is not None else None,
                                          rounding=x["rounding"]))
            agreement.deposit_rate = Decimal(saved["deposit_rate"])
            _audit(db, "FactoryAgreement", agreement.id, "portions", now, then, actor, "Отмена: условия фабрики восстановлены")
            db.flush()
            db.expire(agreement)
            terms += 1
    return (f"Возвращено как было: позиций {changed}, правил наценки {rules_changed}"
            + (f", условий фабрики {terms}" if terms else ""))


ACTIONS: dict[str, tuple[Callable, str]] = {
    "set_customer_markup": (act_set_customer_markup, "Наценка клиента"),
    "set_factory_rate": (act_set_factory_rate, "Условия фабрики"),
    "set_manual_price": (act_set_manual_price, "Ручная цена"),
    "set_item_markup": (act_set_item_markup, "Наценка позиции"),
    "update_item": (act_update_item, "Изменение позиции"),
    "move_item": (act_move_item, "Перенос позиции"),
    "set_status": (act_set_status, "Статус"),
    "record_payment": (act_record_payment, "Оплата"),
    "confirm_payment": (act_confirm_payment, "Подтверждение оплаты"),
    "apply_credit": (act_apply_credit, "Кредит"),
    "reverse_credit": (act_reverse_credit, "Отмена кредита"),
    "set_owner": (act_set_owner, "Владелец"),
    "resolve_allocation": (act_resolve_allocation, "Распределение"),
    "restore_state": (act_restore_state, "Отмена изменения"),
}


@dataclass
class Preview:
    ok: bool
    messages: list[str]
    error: str | None
    before: dict | None
    after: dict | None
    diff: list[dict]


def _affected_seasons(db: Session, actions: list[dict]) -> list[Season]:
    seasons = []
    for action in actions:
        p = action.get("params", {})
        season = None
        try:
            if p.get("item_id") or p.get("item_ids"):
                ids = [p["item_id"]] if p.get("item_id") else p["item_ids"]
                item = db.get(OrderItem, int(ids[0]))
                season = item.order.season if item else None
            elif p.get("order_id"):
                order = db.get(Order, int(p["order_id"]))
                season = order.season if order else None
            elif action.get("action") == "restore_state":
                season = find_season(db, p.get("season"))
            else:
                season = find_season(db, p.get("season"))
        except (ActionError, ValueError, TypeError):
            season = None
        if season is not None and season not in seasons:
            seasons.append(season)
    return seasons


def _run(db: Session, actions: list[dict], actor: str) -> list[str]:
    messages = []
    for action in actions:
        name = action.get("action")
        if name not in ACTIONS:
            raise ActionError(f"Неизвестное действие «{name}».")
        messages.append(ACTIONS[name][0](db, dict(action.get("params") or {}), actor))
        db.flush()
    return messages


def preview_actions(db: Session, actions: list[dict], actor: str = "operator") -> Preview:
    """Apply in a separate session, measure, and roll back. Nothing is saved."""
    bind = db.get_bind()
    with Session(bind=bind, autoflush=False, expire_on_commit=False) as scratch:
        seasons = _affected_seasons(scratch, actions)
        before = {s.name: settlement_summary(season_ledger(scratch, s)) for s in seasons}
        try:
            messages = _run(scratch, actions, actor)
            scratch.expire_all()
            after = {s.name: settlement_summary(season_ledger(scratch, s)) for s in _affected_seasons(scratch, actions) or seasons}
        except ActionError as exc:
            scratch.rollback()
            return Preview(False, [], str(exc), before, None, [])
        finally:
            if scratch.in_transaction():
                scratch.rollback()
    diff = []
    for name in after:
        for row in compare_snapshots(before.get(name, {}), after[name]):
            if row["delta"]:
                diff.append({"season": name, **row})
    return Preview(True, messages, None, before, after, diff)


def commit_actions(db: Session, actions: list[dict], actor: str, reason: str) -> list[str]:
    try:
        messages = _run(db, actions, actor)
        db.flush()
        db.expire_all()
        for season in _affected_seasons(db, actions):
            record_snapshot(db, season, reason)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return messages


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)
