"""Database side of factory/customer reconciliation (see app/settlement.py for the formulas).

Items stay the single source of truth for customer pricing: a customer rule is applied by
writing ``OrderItem.markup_percentage`` (``markup_source='rule'``) through the repricing
preview/commit in ``app/pricing_rules.py``. Manual markups and exact prices are never
overwritten by rules. Factory pricing comes only from ``FactoryAgreement``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.calculations import ZERO, allocate_money, base_total, money, signed_payment_amount
from app.models import (CreditApplication, Customer, FactoryAgreement, FactoryPayment, Order, OrderItem, Payment,
                        Season, SettlementSnapshot)
from app.settlement import CustomerLine, Portion, Settlement, factory_price, marked_up, settle_many

READY_STATES = {"Ready", "Assigned to shipment", "In transit", "Arrived", "Delivered", "Shipped", "Partially shipped"}
UNALLOCATED = "—"


def owner_customer(db: Session) -> Customer | None:
    return db.scalar(select(Customer).where(Customer.is_owner.is_(True)).order_by(Customer.id))


def active_items(order: Order) -> list[OrderItem]:
    return [item for item in order.items if item.status != "Cancelled"]


def is_ready(item: OrderItem) -> bool:
    return item.status in READY_STATES or item.ready_batch_id is not None or item.shipment_link is not None


def item_code(item: OrderItem, owner_code: str | None) -> str:
    if item.customer is not None:
        return item.customer.code
    return owner_code or UNALLOCATED


def item_rate_and_manual(item: OrderItem) -> tuple[Decimal, Decimal | None]:
    if item.selling_unit_price is not None:
        return Decimal(item.markup_percentage or 0), money(item.selling_unit_price * item.quantity)
    return Decimal(item.markup_percentage or 0), None


def order_portions(order: Order, items: list[OrderItem] | None = None) -> tuple[list[Portion], list[str]]:
    """Factory portions for an order's active items, and warnings about the agreement."""
    items = active_items(order) if items is None else items
    agreement: FactoryAgreement | None = getattr(order, "_agreement_cache", None) or _agreement(order)
    warnings: list[str] = []
    if agreement is None or not agreement.portions:
        warnings.append(f"{order.brand.name}: условия фабрики не заданы — фабричная наценка считается 0 %.")
        return [Portion(rate=ZERO, share=Decimal("1"), label="без наценки")], warnings
    if agreement.meaning != "markup":
        warnings.append(f"{order.brand.name}: значение процента фабрики «{agreement.meaning}» не подтверждено.")
    if not agreement.confirmed:
        warnings.append(f"{order.brand.name}: условия фабрики распознаны из файла и ещё не подтверждены.")
    portions: list[Portion] = []
    sectioned = {p.section for p in agreement.portions if p.section}
    for p in agreement.portions:
        if p.section:
            base = money(sum((base_total(i.wholesale_unit_price, i.quantity) for i in items if i.pricing_section == p.section), ZERO))
            portions.append(Portion(rate=Decimal(p.rate), base=base, label=p.label or p.section, policy=p.rounding))
    shared = [p for p in agreement.portions if not p.section]
    leftover = [i for i in items if i.pricing_section not in sectioned]
    if shared:
        for p in shared:
            portions.append(Portion(rate=Decimal(p.rate), share=Decimal(p.share if p.share is not None else 1),
                                    label=p.label, policy=p.rounding))
    elif leftover and any(i.wholesale_unit_price for i in leftover):
        warnings.append(f"{order.brand.name}: позиции без раздела соглашения посчитаны без фабричной наценки.")
        portions.append(Portion(rate=ZERO, share=Decimal("1"), label="вне соглашения"))
    return portions, warnings


def _agreement(order: Order) -> FactoryAgreement | None:
    from sqlalchemy.orm import object_session
    db = object_session(order)
    return db.scalar(select(FactoryAgreement).where(FactoryAgreement.order_id == order.id)) if db else None


def order_lines(order: Order, owner_code: str | None) -> list[CustomerLine]:
    """One line per customer and rate (base summed first, like the factory workbook), plus exact-price lines."""
    grouped: dict[tuple[str, Decimal], Decimal] = {}
    lines: list[CustomerLine] = []
    for item in active_items(order):
        code = item_code(item, owner_code)
        base = base_total(item.wholesale_unit_price, item.quantity)
        rate, manual = item_rate_and_manual(item)
        if manual is not None and code != owner_code:
            lines.append(CustomerLine(code, base, rate, manual_total=manual))
        else:
            grouped[(code, rate)] = money(grouped.get((code, rate), ZERO) + base)
    lines.extend(CustomerLine(code, base, rate) for (code, rate), base in grouped.items())
    return lines


def deposit_rate(order: Order) -> Decimal:
    agreement = _agreement(order)
    if agreement is not None:
        return Decimal(agreement.deposit_rate)
    rule = next((r for r in order.season.rules if r.brand_id == order.brand_id), None)
    return Decimal(rule.initial_prepayment_percentage) if rule else Decimal("0.50")


@dataclass
class OrderView:
    order: Order
    factory_base: Decimal
    factory_total: Decimal
    deposit_rate: Decimal
    deposit_required: Decimal
    portions: list
    ready_factory_value: Decimal
    ready_factory_balance: Decimal
    source_total: Decimal | None
    source_difference: Decimal | None
    warnings: list[str]
    unallocated: int


@dataclass
class SeasonLedger:
    season: Season
    settlement: Settlement
    orders: list[OrderView]
    payments: list[dict]
    warnings: list[str] = field(default_factory=list)
    customer_ready_balance: dict[str, Decimal] = field(default_factory=dict)

    @property
    def ready_factory_balance(self) -> Decimal:
        return money(sum((o.ready_factory_balance for o in self.orders), ZERO))


def _order_view(order: Order, owner_code: str | None) -> tuple[OrderView, list[CustomerLine], list[Portion], dict[str, Decimal]]:
    items = active_items(order)
    portions, warnings = order_portions(order, items)
    lines = order_lines(order, owner_code)
    rate = deposit_rate(order)
    base = money(sum((line.base for line in lines), ZERO))
    price = factory_price(base, portions)
    deposit = money(price.total * rate)
    # Item shares: factory value and factory deposit spread over items cent-exactly.
    weights = {item.id: base_total(item.wholesale_unit_price, item.quantity) for item in items
               if base_total(item.wholesale_unit_price, item.quantity) > 0}
    values = allocate_money(price.total, weights) if weights else {}
    deposits = allocate_money(deposit, values) if values and sum(values.values(), ZERO) > 0 else {}
    ready_value = money(sum((values.get(i.id, ZERO) for i in items if is_ready(i)), ZERO))
    ready_balance = money(sum((values.get(i.id, ZERO) - deposits.get(i.id, ZERO) for i in items if is_ready(i)), ZERO))
    # Customer side: each customer's order total and deposit spread over that customer's items.
    customer_ready: dict[str, Decimal] = {}
    by_code: dict[str, list[OrderItem]] = {}
    for item in items:
        by_code.setdefault(item_code(item, owner_code), []).append(item)
    for code, code_items in by_code.items():
        if code == owner_code:
            total = money(sum((values.get(i.id, ZERO) for i in code_items), ZERO))
        else:
            total = money(sum((line.total for line in lines if line.customer == code), ZERO))
        code_weights = {i.id: base_total(i.wholesale_unit_price, i.quantity) for i in code_items
                        if base_total(i.wholesale_unit_price, i.quantity) > 0}
        if not code_weights:
            continue
        item_totals = allocate_money(total, code_weights)
        item_deposits = allocate_money(money(total * rate), item_totals) if total > 0 else {k: ZERO for k in item_totals}
        due = sum((item_totals[i.id] - item_deposits[i.id] for i in code_items if is_ready(i) and i.id in item_totals), ZERO)
        if due:
            customer_ready[code] = money(due)
    unallocated = sum(1 for i in items if i.customer_id is None)
    if unallocated:
        warnings.append(f"{order.brand.name}: {unallocated} поз. без клиента — посчитаны как ваши, пока не распределены.")
    source = order.source_total
    view = OrderView(order, price.base, price.total, rate, deposit, list(price.portions), ready_value, ready_balance,
                     source, money(price.total - source) if source is not None else None, warnings, unallocated)
    return view, lines, portions, customer_ready


def season_ledger(db: Session, season: Season, order_ids: set[int] | None = None) -> SeasonLedger:
    owner = owner_customer(db)
    owner_code = owner.code if owner else None
    orders = [o for o in sorted(season.orders, key=lambda o: o.brand.name.lower()) if order_ids is None or o.id in order_ids]
    views, invoices, rates, warnings = [], [], [], []
    customer_ready: dict[str, Decimal] = {}
    for order in orders:
        if not active_items(order):
            continue
        view, lines, portions, ready = _order_view(order, owner_code)
        views.append(view)
        invoices.append((lines, portions))
        rates.append(view.deposit_rate)
        warnings.extend(view.warnings)
        for code, amount in ready.items():
            customer_ready[code] = money(customer_ready.get(code, ZERO) + amount)
    if owner is None:
        warnings.insert(0, "Владелец (вы) не отмечен среди клиентов — отметьте себя в разделе «Клиенты», чтобы считать ваш взнос.")
    payments = season_payments(db, season, order_ids)
    credits = money(sum((c.amount for c in db.scalars(select(CreditApplication).where(CreditApplication.reversed_at.is_(None)))
                         if c.order.season_id == season.id and (order_ids is None or c.order_id in order_ids)), ZERO))
    settlement = settle_many(invoices, deposit_rates=rates, owner=owner_code, payments=payments, credits_applied=credits,
                             currency="EUR")
    return SeasonLedger(season, settlement, views, payments, warnings, customer_ready)


def season_payments(db: Session, season: Season, order_ids: set[int] | None = None) -> list[dict]:
    owner = owner_customer(db)
    result = []
    for fp in db.scalars(select(FactoryPayment).where(FactoryPayment.season_id == season.id).order_by(FactoryPayment.date)):
        if order_ids is not None and fp.order_id not in order_ids:
            continue
        result.append({"id": f"f{fp.id}", "payer": owner.code if owner else None, "amount": money(fp.amount), "to": "factory",
                       "confirmed": True, "date": fp.date, "label": "Ваш перевод фабрике", "reference": fp.reference,
                       "order_id": fp.order_id, "note": fp.note})
    for p in db.scalars(select(Payment).where(Payment.season_id == season.id).order_by(Payment.date)):
        if order_ids is not None and p.order_id not in order_ids:
            continue
        result.append({"id": f"p{p.id}", "payer": p.customer.code if p.customer else None,
                       "amount": signed_payment_amount(p.amount, p.type), "to": p.destination or "owner",
                       "confirmed": (p.status or "confirmed") == "confirmed", "date": p.date,
                       "label": ("Клиент → фабрика" if p.destination == "factory" else "Клиент → вам"),
                       "reference": p.reference, "order_id": p.order_id, "note": p.note})
    return result


def customer_order_total(order: Order, customer: Customer, owner_code: str | None) -> tuple[Decimal, Decimal]:
    """(base, total) for one customer in one order, with the same grouping the settlement uses."""
    lines = [line for line in order_lines(order, owner_code) if line.customer == customer.code]
    base = money(sum((line.base for line in lines), ZERO))
    if customer.code == owner_code:
        portions, _ = order_portions(order)
        price = factory_price(money(sum((line.base for line in order_lines(order, owner_code)), ZERO)), portions)
        weights = {line.customer: line.base for line in order_lines(order, owner_code) if line.base > 0}
        shares: dict[str, Decimal] = {}
        for code, amount in weights.items():
            shares[code] = money(shares.get(code, ZERO) + amount)
        return base, allocate_money(price.total, shares).get(customer.code, ZERO) if shares else ZERO
    return base, money(sum((line.total for line in lines), ZERO))


def settlement_summary(ledger: SeasonLedger) -> dict:
    s = ledger.settlement
    return {
        "season": ledger.season.name,
        "factory_base": str(s.factory.base), "factory_total": str(s.factory.total),
        "factory_deposit_required": str(s.factory_deposit_required),
        "external_deposits_expected": str(s.external_deposits_expected),
        "owner_net_deposit_planned": str(s.owner_net_deposit_planned),
        "confirmed_factory_payments": str(s.confirmed_factory_payments),
        "factory_balance_outstanding": str(s.factory_balance_outstanding),
        "factory_deposit_outstanding": str(s.factory_deposit_outstanding),
        "ready_factory_balance": str(ledger.ready_factory_balance),
        "customers": {code: {"base": str(c.base), "total": str(c.total), "deposit": str(c.deposit_due),
                             "ready_due": str(ledger.customer_ready_balance.get(code, ZERO)),
                             "paid": str(money(c.confirmed_to_factory + c.confirmed_to_owner)),
                             "effective_rate": str(round(c.effective_rate * 100, 2))}
                      for code, c in sorted(s.customers.items())},
        "orders": {view.order.brand.name: {"factory_total": str(view.factory_total), "deposit": str(view.deposit_required)}
                   for view in ledger.orders},
    }


def record_snapshot(db: Session, season: Season, reason: str) -> SettlementSnapshot:
    snapshot = SettlementSnapshot(season_id=season.id, reason=reason[:300],
                                  data_json=json.dumps(settlement_summary(season_ledger(db, season)), ensure_ascii=False))
    db.add(snapshot)
    return snapshot


def compare_snapshots(before: dict, after: dict) -> list[dict]:
    rows = []
    for key, label in (("factory_total", "Сумма фабрике"), ("factory_deposit_required", "Предоплата фабрике"),
                       ("external_deposits_expected", "Депозиты клиентов"), ("owner_net_deposit_planned", "Ваш взнос"),
                       ("confirmed_factory_payments", "Оплачено фабрике"), ("factory_balance_outstanding", "Остаток фабрике"),
                       ("ready_factory_balance", "Фабрике за готовые изделия")):
        a, b = Decimal(before.get(key, "0")), Decimal(after.get(key, "0"))
        rows.append({"label": label, "before": a, "after": b, "delta": money(b - a)})
    for code in sorted(set(before.get("customers", {})) | set(after.get("customers", {}))):
        a = Decimal(before.get("customers", {}).get(code, {}).get("total", "0"))
        b = Decimal(after.get("customers", {}).get(code, {}).get("total", "0"))
        rows.append({"label": f"Заказ {code}", "before": a, "after": b, "delta": money(b - a)})
        da = Decimal(before.get("customers", {}).get(code, {}).get("deposit", "0"))
        db_ = Decimal(after.get("customers", {}).get(code, {}).get("deposit", "0"))
        if da or db_:
            rows.append({"label": f"{code}: предоплата", "before": da, "after": db_, "delta": money(db_ - da)})
        ra = Decimal(before.get("customers", {}).get(code, {}).get("ready_due", "0"))
        rb = Decimal(after.get("customers", {}).get(code, {}).get("ready_due", "0"))
        if ra or rb:
            rows.append({"label": f"{code}: к оплате за готовые", "before": ra, "after": rb, "delta": money(rb - ra)})
    return rows


__all__ = ["season_ledger", "SeasonLedger", "OrderView", "owner_customer", "record_snapshot", "compare_snapshots",
           "customer_order_total", "marked_up", "is_ready", "order_portions", "order_lines"]
