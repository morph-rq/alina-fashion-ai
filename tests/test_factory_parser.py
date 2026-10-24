"""Supplier-layout patterns seen in real SS24/SS27 files, rebuilt as small SYNTHETIC workbooks."""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.factory_import import allocate_units, analyze_workbook, canonical_token, split_allocation

KNOWN = {"AA", "ALINA", "IDS", "CC", "AB"}


def units(text, sizes, qty, known=KNOWN):
    result, ok, issue = allocate_units(split_allocation(text, known), sizes, qty)
    return [(u.customer, u.size) for u in result], ok, issue


@pytest.mark.parametrize("text,sizes,qty,expected", [
    ("AA 40, Alina 38", ["38", "40"], 2, [("AA", "40"), ("ALINA", "38")]),
    ("Alina , AA", ["38", "38"], 2, [("ALINA", "38"), ("AA", "38")]),          # one each of one size
    ("AA 40(M)", ["40"], 1, [("AA", "40")]),
    ("Alina 39, 5, 40, 41 , Cc 41", [], 4, [("ALINA", "39.5"), ("ALINA", "40"), ("ALINA", "41"), ("CC", "41")]),
    ("Alina 2", [], 2, [("ALINA", None), ("ALINA", None)]),                    # a count, not a size
    ("AB 38, Alina 42", ["38.42"], 2, [("AB", "38"), ("ALINA", "42")]),        # odd size cell; initials list sizes
    ("IDS, AA", ["UNI"], 2, [("IDS", "UNI"), ("AA", "UNI")]),
    ("Cc  44 , СС 46", ["44", "46"], 2, [("CC", "44"), ("CC", "46")]),        # Cyrillic СС = CC
])
def test_allocation_readings(text, sizes, qty, expected):
    got, ok, _ = units(text, sizes, qty)
    assert ok and got == expected


@pytest.mark.parametrize("text,sizes,qty", [
    ("IDS, Alina", ["38", "40"], 2),               # who has which size?
    ("CC, ids", [], 3),                            # 2 customers, 3 pieces
    ("YG 44 СС 46 ,Kristina Alina 42", ["42", "44", "46"], 3),
    ("", ["38"], 1),
])
def test_ambiguous_allocations_are_never_guessed(text, sizes, qty):
    got, ok, issue = units(text, sizes, qty, KNOWN | {"YG"})
    assert not ok and issue and all(customer is None for customer, _ in got)


def test_tokens_unify_scripts_and_transliteration():
    assert canonical_token("СС", KNOWN) == "CC"
    assert canonical_token("Алина", KNOWN) == "ALINA"
    assert canonical_token("CП", set()) == "СП"


def book(path: Path, rows: list[list], name="Brand") -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = name
    for r, values in enumerate(rows, 1):
        for c, value in enumerate(values, 1):
            if value is not None:
                ws.cell(r, c, value)
    wb.save(path)
    return path


def only_sheet(path):
    analysis = analyze_workbook(path, path.name, KNOWN, "SS27")
    return analysis.sheets[0]


def test_headerless_sheet_uses_line_formulas(tmp_path):
    rows = [[None, "Dior cruise SS27 deadline"], [], [],
            [None, None, "Coat", "711M05B1L99", 34, 2656, 1, "=G4*F4", "AB"],
            [None, None, "Dress", "711R07A5G49", 34, 2969, 1, "=G5*F5", "AB"],
            [None, None, "Long dress", "714L26A1S31", 42, 1641, 1, "=G6*F6", "CC"],
            [None, None, None, None, None, None, "Total", "=SUM(H4:H6)"],
            [None, None, None, None, None, None, 0.3, "=H7*G8"], [None, None, None, None, None, None, None, "=SUM(H7:H8)"]]
    sheet = only_sheet(book(tmp_path / "cruise.xlsx", rows))
    assert [i.unit_price for i in sheet.items] == ["2656.00", "2969.00", "1641.00"]
    assert [i.units[0]["customer"] for i in sheet.items] == ["AB", "AB", "CC"]
    assert [x.rate for x in sheet.sections] == ["0.3"]


def test_half_at_25_half_at_30_from_total_divided_by_two(tmp_path):
    rows = [[None, "Name", "Size", "Q ty", "Price", "Total", "Customer"],
            [None, "Jacket", 38, 1, 1000, "=D2*E2", "AA"], [None, "Pants", 40, 1, 1000, "=D3*E3", "IDS"],
            [None, None, None, None, None, "=SUM(F2:F3)"], [None, None, None, None, None, "=F4/2"],
            [None, None, None, None, 0.25, "=F5*E6"], [None, None, None, None, None, "=F6+F5"],
            [None, None, None, None, None, "=F5"], [None, None, None, None, 0.3, "=F8*E9"]]
    sheet = only_sheet(book(tmp_path / "split.xlsx", rows))
    assert [(x.rate, x.share) for x in sheet.sections] == [("0.25", "0.5"), ("0.3", "0.5")]
    assert all(i.section is None for i in sheet.items)


def test_sections_with_their_own_rates_and_roundup(tmp_path):
    rows = [[None, "Name", "Article", "Q ty", "Price", "Total", None],
            [None, "Pant", "A1", 1, 500, "=D2*E2", "AB"], [None, "Dress", "A2", 1, 1500, "=D3*E3", "AB"],
            [None, None, None, None, "Total RTW", "=SUM(F2:F3)"], [None, None, None, None, 0.23, "=F4*E5"],
            [None, "Tote", "B1", 1, 1000, "=D6*E6", "Alina"],
            [None, None, None, None, "Total bags", "=SUM(F6:F6)"], [None, None, None, None, None, "=ROUNDUP(F7*0.25,0)"]]
    sheet = only_sheet(book(tmp_path / "sections.xlsx", rows))
    assert [(x.label, x.rate, x.rounding) for x in sheet.sections] == [("RTW", "0.23", "cent"), ("bags", "0.25", "euro_up")]
    assert [i.section for i in sheet.items] == ["RTW", "RTW", "bags"]


def test_cancelled_rows_and_code_name_cells(tmp_path):
    rows = [[None, None, None, None, 38, 40, 42, None, None, None, None],
            [None, "Alina 42", "D50CI064L016V0 - 8SINGLE-BREASTED JACKET", None, None, None, 1, "=SUM(E2:G2)", 1246, "=I2*H2", "ok"],
            [None, "CC 40", "D50CL090T108V0 - 9BRA", None, None, 1, None, "=SUM(E3:G3)", 211, None, "cancelled"],
            [None, None, None, None, None, None, None, None, None, "=SUM(J2:J3)"], [None, None, None, None, None, None, None, None, 0.15, "=J4*I5"]]
    sheet = only_sheet(book(tmp_path / "es.xlsx", rows))
    first, second = sheet.items
    assert first.article == "D50CI064L016V0" and first.name.startswith("SINGLE-BREASTED")
    assert [(u["customer"], u["size"]) for u in first.units] == [("ALINA", "42")]
    assert second.status_hint == "Cancelled" and D(sheet.computed_base) == D("1246.00")
