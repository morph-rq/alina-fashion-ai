"""Customer matching and code generation shared by intake paths.

Matching never decides on its own: an exact unique name, code or alias match is
reported as such; anything weaker is a list of candidates for the operator.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Customer


@dataclass(frozen=True)
class CustomerCandidate:
    id: int
    code: str
    name: str | None
    score: float
    reason: str


def _norm(value: str | None) -> str:
    return " ".join(re.findall(r"[^\W_]+", (value or "").casefold()))


def initials_code(name: str | None) -> str:
    words = re.findall(r"[^\W_]+", name or "", flags=re.UNICODE)
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    if words:
        return words[0][:2].upper()
    return "CUST"


def new_customer_code(db: Session, name: str) -> str:
    """A short unique internal code for a genuinely new customer."""
    base = initials_code(name)
    used = {code.casefold() for code in db.scalars(select(Customer.code)).all()}
    if base.casefold() not in used:
        return base
    suffix = 2
    while f"{base}{suffix}".casefold() in used:
        suffix += 1
    return f"{base}{suffix}"


def match_customers(db: Session, text: str | None, code: str | None = None, limit: int = 5) -> tuple[Customer | None, list[CustomerCandidate]]:
    """Return (exact unique match or None, ranked candidates)."""
    query = _norm(text)
    code_query = (code or "").strip().upper()
    customers = db.scalars(select(Customer).options(selectinload(Customer.aliases))).all()
    exact: list[Customer] = []
    candidates: list[CustomerCandidate] = []
    for customer in customers:
        names = [_norm(customer.name)]
        tokens = {customer.code.upper(), *(alias.alias.upper() for alias in customer.aliases)}
        if (query and query in names) or (code_query and code_query in tokens) or (query and query.upper() in tokens):
            exact.append(customer)
            continue
        score, reason = 0.0, ""
        if query and customer.name:
            ratio = SequenceMatcher(None, query, _norm(customer.name)).ratio()
            words = set(query.split())
            shared = words & set(_norm(customer.name).split())
            if shared:
                ratio = max(ratio, 0.6 + 0.1 * len(shared))
                reason = "shares a name word"
            score, reason = ratio, reason or "similar name"
        if query and initials_code(text) == customer.code.upper():
            score, reason = max(score, 0.7), "same initials as the code"
        if score >= 0.55:
            candidates.append(CustomerCandidate(customer.id, customer.code, customer.name, round(score, 2), reason))
    candidates.sort(key=lambda candidate: (-candidate.score, candidate.code))
    if len(exact) == 1:
        return exact[0], candidates[:limit]
    for customer in exact:  # Several exact matches: all become candidates.
        candidates.insert(0, CustomerCandidate(customer.id, customer.code, customer.name, 1.0, "exact match (several)"))
    return None, candidates[:limit]
