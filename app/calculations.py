from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Protocol


ZERO = Decimal("0.00")
CENT = Decimal("0.01")
WHOLE = Decimal("1")


def decimal_value(value: Decimal | int | str | float | None) -> Decimal:
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Decimal | int | str | float | None) -> Decimal:
    return decimal_value(value).quantize(CENT, rounding=ROUND_HALF_UP)


def display_money(value: Decimal | int | str | float | None) -> Decimal:
    return decimal_value(value).quantize(WHOLE, rounding=ROUND_HALF_UP)


PAYMENT_TYPE_LABELS = {"payment": "Payment received", "refund": "Refund"}


def signed_payment_amount(amount: Decimal | int | str | None, payment_type: str | None) -> Decimal:
    """Customer receipts are stored as positive amounts; a refund reduces net payments."""
    value = money(amount)
    return -value if payment_type == "refund" else value


def base_total(unit_price: Decimal | None, quantity: int) -> Decimal:
    return money(decimal_value(unit_price) * Decimal(quantity))


def markup_amount(base: Decimal, percentage: Decimal) -> Decimal:
    return money(decimal_value(base) * decimal_value(percentage))


def final_total(unit_price: Decimal | None, quantity: int, percentage: Decimal) -> Decimal:
    base = base_total(unit_price, quantity)
    return money(base + markup_amount(base, percentage))


def final_unit_price(unit_price: Decimal | None, percentage: Decimal) -> Decimal:
    base = money(unit_price)
    return money(base + markup_amount(base, percentage))


def transport_fee(eligible_value: Decimal, percentage: Decimal) -> Decimal:
    return money(decimal_value(eligible_value) * decimal_value(percentage))


class ItemLike(Protocol):
    wholesale_unit_price: Decimal | None
    quantity: int
    markup_percentage: Decimal
    status: str
    shipment_link: object | None
    ready_batch_id: int | None


@dataclass(frozen=True)
class ItemAmounts:
    base: Decimal
    markup: Decimal
    final: Decimal


def item_amounts(item: ItemLike) -> ItemAmounts:
    base = base_total(item.wholesale_unit_price, item.quantity)
    selling_price = getattr(item, "selling_unit_price", None)
    if selling_price is not None:
        final = money(selling_price * item.quantity)
        return ItemAmounts(base=base, markup=money(final - base), final=final)
    markup = markup_amount(base, item.markup_percentage)
    return ItemAmounts(base=base, markup=markup, final=money(base + markup))


@dataclass
class OrderAmounts:
    base: Decimal = ZERO
    markup: Decimal = ZERO
    final: Decimal = ZERO
    required_prepayment: Decimal = ZERO
    ready_balance_due: Decimal = ZERO
    ready: Decimal = ZERO
    shipments: dict[int, Decimal] = field(default_factory=dict)
    saldo: Decimal = ZERO
    current_merchandise_charge: Decimal = ZERO


def calculate_order(
    items: Iterable[ItemLike],
    prepayment_percentage: Decimal,
    ready_balance_percentage: Decimal | None = None,
) -> OrderAmounts:
    ready_percentage = (
        decimal_value(ready_balance_percentage)
        if ready_balance_percentage is not None
        else decimal_value(prepayment_percentage)
    )
    result = OrderAmounts()
    initial_buckets: dict[Decimal, Decimal] = {}
    for item in items:
        if item.status == "Cancelled":
            continue
        amounts = item_amounts(item)
        result.base += amounts.base
        result.markup += amounts.markup
        result.final += amounts.final
        group = getattr(item, 'purchase_group', None)
        initial_rate = group.initial_percentage if group else prepayment_percentage
        balance_rate = group.shipment_percentage if group else ready_percentage
        initial_buckets[initial_rate] = initial_buckets.get(initial_rate, ZERO) + amounts.final
        if getattr(item, "ready_batch_id", None) is not None:
            result.ready_balance_due += money(amounts.final * balance_rate)
        shipment_link = item.shipment_link
        if shipment_link is not None:
            shipment_id = shipment_link.shipment_id
            result.shipments[shipment_id] = result.shipments.get(shipment_id, ZERO) + amounts.final
        elif getattr(item, "ready_batch_id", None) is not None or item.status == "Ready":
            result.ready += amounts.final
    result.base = money(result.base)
    result.markup = money(result.markup)
    result.final = money(result.final)
    result.required_prepayment = money(sum((value * rate for rate, value in initial_buckets.items()), ZERO))
    result.ready_balance_due = money(result.ready_balance_due)
    result.ready = money(result.ready)
    result.shipments = {key: money(value) for key, value in result.shipments.items()}
    fulfilled = result.ready + sum(result.shipments.values(), ZERO)
    result.saldo = money(result.final - fulfilled)
    result.current_merchandise_charge = money(
        result.required_prepayment + result.ready_balance_due
    )
    return result


@dataclass(frozen=True)
class AccountAmounts:
    total_paid: Decimal
    total_charged: Decimal
    outstanding: Decimal
    overpayment: Decimal


def calculate_account(total_paid: Decimal, total_charged: Decimal) -> AccountAmounts:
    paid = money(total_paid)
    charged = money(total_charged)
    delta = money(charged - paid)
    return AccountAmounts(
        total_paid=paid,
        total_charged=charged,
        outstanding=max(delta, ZERO),
        overpayment=max(-delta, ZERO),
    )
