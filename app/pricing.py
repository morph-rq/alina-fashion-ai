"""Price-meaning conversions shared by manual entry, AI intake and imports.

Policy for a stated customer-final price (documented in docs/business-rules.md):

1. Search the cent values around ``final / (1 + markup)`` for a wholesale unit
   price whose forward calculation (``final_unit_price``) gives exactly the stated
   final. When one exists it is used and no override is stored.
2. When no cent wholesale reproduces the final (for example EUR 100.00 at 35%),
   the stated final is kept exactly as the item's ``selling_unit_price`` and the
   nearest derived wholesale is recorded. The result says so, so the draft can show
   the operator that the wholesale amount is derived, not quoted.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.calculations import CENT, final_unit_price, money


@dataclass(frozen=True)
class ReversePrice:
    wholesale: Decimal
    exact: bool
    selling_unit_price: Decimal | None
    reason: str | None = None


def wholesale_from_final(final: Decimal, markup: Decimal) -> ReversePrice:
    final = money(final)
    markup = Decimal(markup)
    if final <= 0:
        raise ValueError("Final price must be positive")
    estimate = money(final / (Decimal("1") + markup))
    candidates = sorted({money(estimate + CENT * offset) for offset in range(-3, 4) if estimate + CENT * offset > 0},
                        key=lambda value: (abs(value - estimate), value))
    for candidate in candidates:
        if final_unit_price(candidate, markup) == final:
            return ReversePrice(wholesale=candidate, exact=True, selling_unit_price=None)
    return ReversePrice(
        wholesale=estimate, exact=False, selling_unit_price=final,
        reason=(f"No wholesale price in whole cents gives exactly {final} at {markup * 100:.0f}% markup; "
                f"the stated final is kept as the selling price and the wholesale {estimate} is derived."),
    )


CURRENCY_MARKERS = {"€": "EUR", "EUR": "EUR", "EURO": "EUR", "EUROS": "EUR", "$": "USD", "USD": "USD", "£": "GBP",
                    "GBP": "GBP", "CHF": "CHF", "₽": "RUB", "RUB": "RUB", "РУБ": "RUB"}


@dataclass(frozen=True)
class AmountReading:
    amount: Decimal | None
    currency: str | None
    problem: str | None = None


def parse_amount_text(text: str | None) -> AmountReading:
    """Read an amount as written in a message, without guessing decimal locale.

    "1.200,50" and "1,200.50" are unambiguous. "1.200" or "1,200" could mean
    1200 or 1.2, so they are reported as ambiguous instead of being guessed.
    """
    import re
    raw = (text or "").strip()
    if not raw:
        return AmountReading(None, None, "No amount was stated.")
    upper = raw.upper()
    currencies = {code for marker, code in CURRENCY_MARKERS.items() if marker in upper}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    if len(currencies) > 1:
        return AmountReading(None, None, f"Several currencies appear in {raw!r}.")
    numbers = re.findall(r"\d{1,3}(?:[\s'\u00a0\u202f]\d{3}(?!\d))+(?:[.,]\d+)?|\d[\d.,]*\d|\d", raw)
    if len(numbers) != 1:
        return AmountReading(None, currency, f"Could not find exactly one amount in {raw!r}.")
    number = re.sub(r"[\s'\u00a0\u202f]", "", numbers[0])
    if "." in number and "," in number:
        decimal_mark = "." if number.rfind(".") > number.rfind(",") else ","
        thousands = "," if decimal_mark == "." else "."
        number = number.replace(thousands, "").replace(decimal_mark, ".")
    elif "," in number or "." in number:
        mark = "," if "," in number else "."
        head, _, tail = number.rpartition(mark)
        if number.count(mark) > 1:
            number = number.replace(mark, "")  # 1.200.000 -> thousands only
        elif len(tail) == 3:
            return AmountReading(None, currency, f"{raw!r} could mean {head}{tail} or {head}.{tail}; state the amount without a thousands separator.")
        else:
            number = f"{head}.{tail}"
    try:
        amount = Decimal(number)
    except Exception:  # noqa: BLE001 - reported as a reading problem
        return AmountReading(None, currency, f"Could not read {raw!r} as an amount.")
    if amount != money(amount):
        return AmountReading(None, currency, f"{raw!r} has more than two decimals.")
    return AmountReading(money(amount), currency)
