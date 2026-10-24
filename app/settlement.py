"""Factory pricing, customer pricing and deposit reconciliation (pure Decimal, no database).

Two independent price concepts (docs/business-rules.md, "Factory and customer rates"):

* the factory charges the owner ``base × (1 + factory rate)`` — possibly with several
  rate portions, e.g. 25 % on one half of the eligible base and 30 % on the other;
* the owner charges each customer ``base × (1 + customer rate)`` on the SAME base.
  The customer rate is never applied on top of the factory-marked-up amount.

Customers pay their deposits straight to the factory; the owner pays the rest:

    factory_deposit_required = factory_total × factory_deposit_rate
    external_deposits        = Σ external customers' (customer_total × client_deposit_rate)
    owner_net_deposit        = factory_deposit_required − external_deposits

A negative ``owner_net_deposit`` is an overfunded plan (credit), never a negative transfer.
Expected amounts are kept apart from confirmed payments; only confirmed money reduces the
outstanding factory balance.

All percentages are fractions (0.30 = 30 %). Money is rounded half-up to cents once per
total, never per intermediate step, unless a portion's rounding policy says otherwise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP
from typing import Iterable

from app.calculations import CENT, ZERO, allocate_money, decimal_value, money

ONE = Decimal("1")
WHOLE = Decimal("1")
ROUNDING_POLICIES = {
    "cent": "Округление до цента (0,5 вверх)",
    "euro_up": "Комиссия округляется вверх до целого евро (как ROUNDUP в Excel)",
}


def rate(value) -> Decimal:
    """Accept 0.35, '0.35', 35, '35' or '35%' and return the fraction 0.35."""
    if isinstance(value, str):
        value = value.strip().replace("%", "").replace(",", ".")
    number = decimal_value(value)
    if number > 1:
        number = number / 100
    if number < 0 or number > 10:
        raise ValueError("Процент должен быть от 0 до 1000")
    return number


def commission(base: Decimal, percentage: Decimal, policy: str = "cent") -> Decimal:
    raw = decimal_value(base) * decimal_value(percentage)
    if policy == "euro_up":
        return raw.quantize(WHOLE, rounding=ROUND_CEILING).quantize(CENT)
    return raw.quantize(CENT, rounding=ROUND_HALF_UP)


def marked_up(base: Decimal, percentage: Decimal, policy: str = "cent") -> Decimal:
    """``base × (1 + percentage)`` with the commission rounded according to ``policy``."""
    base = money(base)
    return money(base + commission(base, percentage, policy))


# --- Factory side -------------------------------------------------------------------

@dataclass(frozen=True)
class Portion:
    """One agreed factory rate. Either a share of the eligible base or an explicit base."""
    rate: Decimal
    share: Decimal | None = None      # fraction of the eligible base (0.5 = half)
    base: Decimal | None = None       # explicit eligible amount (e.g. a section total)
    label: str = ""
    policy: str = "cent"


@dataclass(frozen=True)
class PortionAmount:
    label: str
    rate: Decimal
    base: Decimal
    commission: Decimal
    total: Decimal


@dataclass(frozen=True)
class FactoryPrice:
    base: Decimal
    portions: tuple[PortionAmount, ...]
    total: Decimal

    @property
    def commission(self) -> Decimal:
        return money(self.total - self.base)

    @property
    def effective_rate(self) -> Decimal:
        return (self.commission / self.base) if self.base else ZERO


def factory_price(eligible_base: Decimal, portions: Iterable[Portion]) -> FactoryPrice:
    """Weighted factory total ``Σ portion_base × (1 + portion_rate)``.

    Explicit-base portions are taken first; share portions then split the remaining
    eligible base cent-exactly by their shares. Any base not covered by a portion is an
    error, so a partly specified agreement can never silently drop money.
    """
    eligible_base = money(eligible_base)
    portions = list(portions)
    if not portions:
        portions = [Portion(rate=ZERO, share=ONE)]
    explicit = [p for p in portions if p.base is not None]
    shared = [p for p in portions if p.base is None]
    remaining = money(eligible_base - sum((money(p.base) for p in explicit), ZERO))
    if remaining < 0:
        raise ValueError("Сумма разделов соглашения больше суммы заказа")
    amounts: list[PortionAmount] = []
    for p in explicit:
        base = money(p.base)
        com = commission(base, p.rate, p.policy)
        amounts.append(PortionAmount(p.label, p.rate, base, com, money(base + com)))
    if shared:
        shares = {index: decimal_value(p.share if p.share is not None else ONE) for index, p in enumerate(shared)}
        if sum(shares.values(), ZERO) <= 0:
            raise ValueError("Доли соглашения должны быть положительными")
        bases = allocate_money(remaining, shares) if remaining else {index: ZERO for index in shares}
        for index, p in enumerate(shared):
            base = bases[index]
            com = commission(base, p.rate, p.policy)
            amounts.append(PortionAmount(p.label, p.rate, base, com, money(base + com)))
    elif remaining:
        raise ValueError(f"Соглашение не покрывает {remaining} из суммы заказа")
    return FactoryPrice(eligible_base, tuple(amounts), money(sum((a.total for a in amounts), ZERO)))


# --- Customer side ------------------------------------------------------------------

@dataclass(frozen=True)
class CustomerLine:
    """A customer's base amount at one rate, or an exact agreed final price."""
    customer: str
    base: Decimal
    rate: Decimal | None = None
    manual_total: Decimal | None = None
    policy: str = "cent"

    @property
    def total(self) -> Decimal:
        if self.manual_total is not None:
            return money(self.manual_total)
        return marked_up(self.base, self.rate or ZERO, self.policy)


@dataclass
class CustomerAmounts:
    customer: str
    is_owner: bool
    base: Decimal = ZERO
    total: Decimal = ZERO            # what the customer owes (for the owner: her nominal factory cost)
    deposit_due: Decimal = ZERO
    factory_share: Decimal = ZERO    # this customer's goods at factory prices
    spread: Decimal = ZERO           # total − factory_share (theoretical, not realised)
    confirmed_to_factory: Decimal = ZERO
    confirmed_to_owner: Decimal = ZERO
    expected: Decimal = ZERO         # promised, not confirmed

    @property
    def effective_rate(self) -> Decimal:
        return (self.total - self.base) / self.base if self.base else ZERO

    @property
    def deposit_outstanding(self) -> Decimal:
        return max(money(self.deposit_due - self.confirmed_to_factory - self.confirmed_to_owner), ZERO)


@dataclass
class Settlement:
    currency: str
    factory: FactoryPrice
    factory_deposit_rate: Decimal
    client_deposit_rate: Decimal
    factory_deposit_required: Decimal
    customers: dict[str, CustomerAmounts]
    owner: str | None
    external_deposits_expected: Decimal
    owner_net_deposit_planned: Decimal          # may be negative = overfunded
    owner_nominal_cost: Decimal
    owner_nominal_deposit: Decimal
    confirmed_factory_payments: Decimal         # by anyone, directly to the factory
    owner_paid_to_factory: Decimal
    credits_applied: Decimal
    factory_deposit_outstanding: Decimal
    factory_balance_outstanding: Decimal
    factory_overpaid: Decimal
    received_by_owner_to_forward: Decimal
    theoretical_spread: Decimal
    notes: list[str] = field(default_factory=list)

    @property
    def owner_transfer_now(self) -> Decimal:
        """What the owner still has to send for the deposit if customers pay as planned."""
        return max(money(self.owner_net_deposit_planned - self.owner_paid_to_factory - self.credits_applied), ZERO)

    @property
    def overfunded(self) -> Decimal:
        return max(-self.owner_net_deposit_planned, ZERO)


def settle(lines: Iterable[CustomerLine], factory_portions: Iterable[Portion], **kwargs) -> Settlement:
    """Reconcile one factory invoice. See :func:`settle_many`."""
    return settle_many([(list(lines), list(factory_portions))], **kwargs)


def settle_many(
    invoices: Iterable[tuple[list[CustomerLine], list[Portion]]],
    *,
    factory_deposit_rate: Decimal = Decimal("0.50"),
    client_deposit_rate: Decimal | None = None,
    deposit_rates: list[Decimal] | None = None,
    owner: str | None = None,
    payments: Iterable[dict] = (),
    credits_applied: Decimal = ZERO,
    currency: str = "EUR",
) -> Settlement:
    """Reconcile factory invoices (e.g. every brand order of a season) against their customers.

    Each invoice is priced and its deposit rounded on its own, then the amounts are added.
    ``payments`` are dicts ``{payer, amount, to: 'factory'|'owner', confirmed: bool}``.
    Only confirmed payments to the factory (plus explicitly applied credit) reduce the
    factory balance. Confirmed payments a customer made to the owner are listed as money
    to forward. Promised payments are shown but counted nowhere.
    """
    invoices = list(invoices)
    client_deposit_rate = factory_deposit_rate if client_deposit_rate is None else client_deposit_rate
    customers: dict[str, CustomerAmounts] = {}
    all_portions: list[PortionAmount] = []
    factory_base = factory_total = deposit_required = ZERO
    for index, (lines, portions) in enumerate(invoices):
        invoice_rate = deposit_rates[index] if deposit_rates else factory_deposit_rate
        per: dict[str, CustomerAmounts] = {}
        for line in lines:
            entry = per.setdefault(line.customer, CustomerAmounts(line.customer, line.customer == owner))
            entry.base = money(entry.base + line.base)
            if not entry.is_owner:
                entry.total = money(entry.total + line.total)
        base = money(sum((c.base for c in per.values()), ZERO))
        price = factory_price(base, portions)
        all_portions.extend(price.portions)
        factory_base, factory_total = money(factory_base + price.base), money(factory_total + price.total)
        deposit_required = money(deposit_required + money(price.total * invoice_rate))
        weights = {code: c.base for code, c in per.items() if c.base > 0}
        shares = allocate_money(price.total, weights) if weights else {}
        for code, entry in per.items():
            entry.factory_share = shares.get(code, ZERO)
            if entry.is_owner:
                entry.total = entry.factory_share
            entry.deposit_due = money(entry.total * (invoice_rate if entry.is_owner else client_deposit_rate))
            target = customers.setdefault(code, CustomerAmounts(code, entry.is_owner))
            for name in ("base", "total", "deposit_due", "factory_share"):
                setattr(target, name, money(getattr(target, name) + getattr(entry, name)))
    for entry in customers.values():
        entry.spread = ZERO if entry.is_owner else money(entry.total - entry.factory_share)
    factory = FactoryPrice(factory_base, tuple(all_portions), factory_total)
    external = money(sum((c.deposit_due for c in customers.values() if not c.is_owner), ZERO))
    confirmed_factory = owner_paid = to_forward = ZERO
    for payment in payments:
        amount = money(payment["amount"])
        payer = payment.get("payer")
        entry = customers.get(payer) if payer else None
        if not payment.get("confirmed", True):
            if entry is not None:
                entry.expected = money(entry.expected + amount)
            continue
        if payment.get("to", "factory") == "factory":
            confirmed_factory = money(confirmed_factory + amount)
            if payer == owner or payer is None:
                owner_paid = money(owner_paid + amount)
            if entry is not None:
                entry.confirmed_to_factory = money(entry.confirmed_to_factory + amount)
        else:
            to_forward = money(to_forward + amount)
            if entry is not None:
                entry.confirmed_to_owner = money(entry.confirmed_to_owner + amount)
    credits_applied = money(credits_applied)
    paid = money(confirmed_factory + credits_applied)
    owner_entry = customers.get(owner) if owner else None
    result = Settlement(
        currency=currency, factory=factory, factory_deposit_rate=factory_deposit_rate,
        client_deposit_rate=client_deposit_rate, factory_deposit_required=deposit_required,
        customers=customers, owner=owner, external_deposits_expected=external,
        owner_net_deposit_planned=money(deposit_required - external),
        owner_nominal_cost=owner_entry.total if owner_entry else ZERO,
        owner_nominal_deposit=owner_entry.deposit_due if owner_entry else ZERO,
        confirmed_factory_payments=confirmed_factory, owner_paid_to_factory=owner_paid,
        credits_applied=credits_applied,
        factory_deposit_outstanding=max(money(deposit_required - paid), ZERO),
        factory_balance_outstanding=max(money(factory.total - paid), ZERO),
        factory_overpaid=max(money(paid - factory.total), ZERO),
        received_by_owner_to_forward=to_forward,
        theoretical_spread=money(sum((c.spread for c in customers.values()), ZERO)),
    )
    if result.owner_net_deposit_planned < 0:
        result.notes.append("Депозиты клиентов покрывают предоплату фабрике полностью; излишек — кредит, а не перевод.")
    if to_forward:
        result.notes.append("Часть оплат клиентов пришла вам, а не фабрике — их нужно переслать фабрике.")
    return result


def split_line_finals(lines: list[tuple[object, Decimal]], total: Decimal) -> dict:
    """Spread an authoritative total over lines by base so line amounts add up exactly."""
    weights = {key: money(base) for key, base in lines if money(base) > 0}
    result = {key: ZERO for key, _ in lines}
    result.update(allocate_money(total, weights) if weights else {})
    return result
