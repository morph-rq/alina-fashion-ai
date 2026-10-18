from decimal import Decimal
from types import SimpleNamespace

from app.calculations import (
    calculate_account,
    calculate_order,
    display_money,
    final_total,
    markup_amount,
    money,
    transport_fee,
)


D = Decimal


def test_example_a_wholesale_1028_at_35_percent() -> None:
    mathematical_exact = D("1028") * (D("1") + D("0.35"))
    accounting_stored = final_total(D("1028"), 1, D("0.35"))
    display_value = display_money(accounting_stored)
    assert mathematical_exact == D("1387.80")
    assert accounting_stored == D("1387.80")
    assert display_value == D("1388")


def test_example_b_wholesale_1185_at_35_percent() -> None:
    mathematical_exact = D("1185") * (D("1") + D("0.35"))
    accounting_stored = final_total(D("1185"), 1, D("0.35"))
    display_value = display_money(accounting_stored)
    assert mathematical_exact == D("1599.75")
    assert accounting_stored == D("1599.75")
    assert display_value == D("1600")


def test_example_c_base_3464_at_30_percent() -> None:
    mathematical_markup = D("3464") * D("0.30")
    accounting_markup = markup_amount(D("3464"), D("0.30"))
    accounting_final = money(D("3464") + accounting_markup)
    assert mathematical_markup == D("1039.20")
    assert accounting_markup == D("1039.20")
    assert accounting_final == D("4503.20")
    assert display_money(accounting_final) == D("4503")


def test_example_d_transport_34038_at_5_percent() -> None:
    mathematical_exact = D("34038") * D("0.05")
    accounting_stored = transport_fee(D("34038"), D("0.05"))
    display_value = display_money(accounting_stored)
    assert mathematical_exact == D("1701.90")
    assert accounting_stored == D("1701.90")
    assert display_value == D("1702")


def test_workbook_current_charge_rule() -> None:
    items = [
        SimpleNamespace(wholesale_unit_price=D("100"), quantity=1, markup_percentage=D("0.30"), status="Ready", shipment_link=None, ready_batch_id=1),
        SimpleNamespace(wholesale_unit_price=D("200"), quantity=1, markup_percentage=D("0.30"), status="Ordered", shipment_link=None, ready_batch_id=None),
    ]
    totals = calculate_order(items, D("0.50"))
    assert totals.final == D("390.00")
    assert totals.ready == D("130.00")
    assert totals.saldo == D("260.00")
    assert totals.current_merchandise_charge == D("260.00")
    assert totals.required_prepayment == D("195.00")
    assert totals.ready_balance_due == D("65.00")


def test_account_separates_payable_and_overpayment() -> None:
    payable = calculate_account(D("90"), D("100"))
    overpaid = calculate_account(D("110"), D("100"))
    assert payable.outstanding == D("10.00") and payable.overpayment == D("0.00")
    assert overpaid.outstanding == D("0.00") and overpaid.overpayment == D("10.00")
