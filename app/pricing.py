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
