"""Turn a typed description into a table draft the operator confirms before anything is created.

Two kinds: a season with brand tables (real order tables with Brand + Season rules), or a
free-form custom table with named columns. AI is used when configured; a deterministic parser
handles the common phrasings otherwise, so the feature works offline too. Nothing here writes
to the database; creation lives in ``tables_routes``.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, Field

from app import ai_intake

COLUMN_TYPES = ("text", "number", "money", "date")
SEASON_RE = re.compile(r"\b(SS|FW|AW|PF|RS)\s?-?(\d{2})\b", re.IGNORECASE)


class PlanBrand(BaseModel):
    name: str
    markup_percent: float | None = None
    initial_percent: float | None = None
    transport_percent: float | None = None


class PlanColumn(BaseModel):
    name: str
    type: Literal["text", "number", "money", "date"] = "text"


class TablePlan(BaseModel):
    kind: Literal["season", "custom"]
    title: str = ""
    season: str | None = None
    brands: list[PlanBrand] = Field(default_factory=list)
    columns: list[PlanColumn] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    notes: str | None = None


class PlannedRows(BaseModel):
    rows: list[list[str]] = Field(default_factory=list)


SYSTEM = """You turn a short description from a fashion-order business owner into a table draft.
kind "season": the user wants a new season (e.g. SS27, FW26) and/or brand tables in it. Fill season (upper case,
like FW27) and brands (name, and only the percentages the user stated: markup, initial prepayment, transport).
kind "custom": any other table (suppliers, contacts, tasks, samples...). Fill title and columns (short names; type
text, number, money or date). If the user lists example rows, put them in rows in column order.
Never invent percentages, brands, columns or rows the user did not give. Return JSON matching the schema."""


def _number(text: str) -> float | None:
    try:
        return float(Decimal(text.replace(",", ".")))
    except InvalidOperation:
        return None


def guess_type(name: str) -> str:
    low = name.casefold()
    if re.search(r"price|cost|amount|fee|total|eur|€|paid|budget", low):
        return "money"
    if re.search(r"date|deadline|due|when|day", low):
        return "date"
    if re.search(r"qty|quantity|number of|count|pieces|units|size run", low):
        return "number"
    return "text"


def _split_list(text: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|\band\b|\+|/(?!\d))\s*", text.strip(" .:"), flags=re.IGNORECASE)
    return [p.strip(" .:'\"") for p in parts if p.strip(" .:'\"")]


def _percent(text: str, *patterns: str) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _number(match.group(1))
    return None


def parse_locally(text: str) -> TablePlan:
    clean = " ".join(text.split())
    season = SEASON_RE.search(clean)
    if season:
        name = f"{season.group(1).upper()}{season.group(2)}"
        markup = _percent(clean, r"(\d+(?:[.,]\d+)?)\s*%\s*(?:markup|mark-up|margin)", r"(?:markup|mark-up|margin)\s*(?:of\s*)?(\d+(?:[.,]\d+)?)\s*%")
        initial = _percent(clean, r"(\d+(?:[.,]\d+)?)\s*/\s*\d+(?:[.,]\d+)?\s*(?:prepayment|payment|split)?", r"(\d+(?:[.,]\d+)?)\s*%\s*(?:prepayment|deposit|upfront)", r"(?:prepayment|deposit)\s*(\d+(?:[.,]\d+)?)\s*%")
        transport = _percent(clean, r"(\d+(?:[.,]\d+)?)\s*%\s*transport", r"transport\s*(?:of\s*)?(\d+(?:[.,]\d+)?)\s*%")
        brands_text = re.search(r"\bbrands?\s*:?\s*(.+?)(?=\s*(?:\bwith\b|\d+(?:[.,]\d+)?\s*%|\bmarkup\b|\btransport\b|\bprepayment\b|$))", clean, re.IGNORECASE)
        brands = [PlanBrand(name=b, markup_percent=markup, initial_percent=initial, transport_percent=transport)
                  for b in (_split_list(brands_text.group(1)) if brands_text else []) if not SEASON_RE.fullmatch(b)]
        return TablePlan(kind="season", title=f"Season {name}", season=name, brands=brands,
                         notes=None if brands else "No brand names found. Add them below or leave empty to create only the season.")
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    head = lines[0] if lines else ""
    title, columns_text = head, ""
    match = re.search(r"\b(?:with columns?|columns?|with|having|fields?)\b\s*:?\s*(.+)$", head, re.IGNORECASE) or re.search(r":\s*(.+)$", head)
    if match:
        title, columns_text = head[:match.start()], match.group(1)
    title = re.sub(r"^(?:please\s+)?(?:make|create|add|new|build|start)?\s*(?:me\s+)?(?:a|an|the)?\s*(?:new\s+)?(?:table|list|sheet)?\s*(?:of|for|called|named)?\s*",
                   "", title.strip(" :.-"), flags=re.IGNORECASE).strip(" :.-") or "New table"
    columns = [PlanColumn(name=c[:1].upper() + c[1:60], type=guess_type(c)) for c in _split_list(columns_text)][:30]
    rows = []
    for line in lines[1:]:
        cells = [c.strip() for c in re.split(r"\t|;|\|", line)] if re.search(r"\t|;|\|", line) else [c.strip() for c in line.split(",")]
        if len(cells) > 1 or columns:
            rows.append(cells)
    if not columns and rows:
        columns = [PlanColumn(name=f"Column {i + 1}") for i in range(max(len(r) for r in rows))]
    return TablePlan(kind="custom", title=title[:120][:1].upper() + title[1:120], columns=columns, rows=rows[:500],
                     notes=None if columns else "No column names found. Add them below.")


def plan_table(text: str) -> TablePlan:
    if not text.strip():
        raise ValueError("Describe the table first.")
    if ai_intake.ai_configured():
        try:
            plan = ai_intake._generate(TablePlan, SYSTEM, [{"text": text.strip()[:6000]}], timeout=35, max_models=2)
            plan.notes = (plan.notes + " " if plan.notes else "") + "Drafted by AI from your description. Check it before creating."
            return plan
        except ValueError as exc:
            plan = parse_locally(text)
            plan.notes = f"{exc} Read with the built-in parser instead." + (" " + plan.notes if plan.notes else "")
            return plan
    return parse_locally(text)


def rows_from_text(text: str, columns: list[dict]) -> tuple[list[list[str]], str | None]:
    """Rows for an existing custom table from pasted lines or a description."""
    if not text.strip():
        return [], "Type or paste the rows first."
    names = [c["name"] for c in columns]
    structured = all(re.search(r"\t|;|\||,", line) for line in text.strip().splitlines() if line.strip())
    if ai_intake.ai_configured() and not structured:
        try:
            prompt = json.dumps({"columns": names, "text": text.strip()[:6000]}, ensure_ascii=False)
            planned = ai_intake._generate(PlannedRows, "Split the text into table rows. Each row lists values in the "
                                          "given column order; use an empty string when a value is not stated. Never invent values.",
                                          [{"text": prompt}], timeout=35, max_models=2)
            return [row[:len(names)] for row in planned.rows][:500], "Rows read by AI. Check them in the table."
        except ValueError as exc:
            note = f"{exc} Read line by line instead."
    else:
        note = None
    rows = []
    for line in text.strip().splitlines():
        if not line.strip():
            continue
        cells = re.split(r"\t|;|\|", line) if re.search(r"\t|;|\|", line) else line.split(",")
        rows.append([c.strip() for c in cells][:len(names)])
    return rows[:500], note
