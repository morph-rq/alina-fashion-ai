"""Exact, page-by-page access to every cell of large supplier workbooks (100 MB+), for the assistant.

A factory file is mostly photos; the cells are a small part of it. The first time a workbook is
inspected, every sheet is streamed once (openpyxl read-only, images never loaded) into a small
SQLite index next to the other caches: one row per non-empty cell with its value *and* formula.
After that the assistant can list sheets, read any row range or search the whole file instantly,
always seeing the exact cell values — nothing is summarised, rounded or cut. Pages are limited
in size (``MAX_ROWS``) so a reply never has to carry the whole file; the caller asks for the next
page instead.

Sources are the stored originals of committed imports (``batch:<id>``) and uploads that are still
being reviewed (``import:<token>``). Photos are reported per row (that a row has one) but are not
part of the index; the review screens and documents show them.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.config import ROOT_DIR

CACHE_DIR = ROOT_DIR / "data" / "cache" / "inspect"
MAX_ROWS = 200          # rows per page returned to the assistant
MAX_HITS = 100
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class InspectError(ValueError):
    pass


def column_letter(index: int) -> str:
    letters = ""
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def column_index(letters: str) -> int:
    value = 0
    for char in letters.upper():
        value = value * 26 + ord(char) - 64
    return value


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return repr(value) if value != int(value) else str(int(value))
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _key(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:24]


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def index_path(path: Path, cache_dir: Path | None = None) -> Path:
    return (cache_dir or CACHE_DIR) / f"{_key(path)}.sqlite"


def build_index(path: Path, cache_dir: Path | None = None) -> Path:
    """Stream every sheet once into the cell index (values + formulas + photo anchors). Idempotent."""
    from app import xlsx_stream
    path = Path(path)
    if not path.exists():
        raise InspectError("Файл не найден.")
    if not zipfile.is_zipfile(path):
        raise InspectError("Это не файл Excel .xlsx/.xlsm.")
    target = index_path(path, cache_dir)
    with _lock(target.name):
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".part")
        temp.unlink(missing_ok=True)
        con = sqlite3.connect(temp)
        try:
            con.executescript("""
                CREATE TABLE sheets(position INTEGER, name TEXT PRIMARY KEY, rows INTEGER, cols INTEGER, cells INTEGER, images INTEGER);
                CREATE TABLE cells(sheet TEXT, row INTEGER, col INTEGER, value TEXT, formula TEXT, PRIMARY KEY(sheet, row, col)) WITHOUT ROWID;
                CREATE TABLE images(sheet TEXT, row INTEGER, col INTEGER, part TEXT);
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);""")
            with zipfile.ZipFile(path) as archive:
                parts = xlsx_stream.sheet_parts(archive)
                anchors = {name: xlsx_stream.image_anchors(archive, part) for name, part in parts}
            for position, (name, _part) in enumerate(parts):
                formulas: dict[tuple[int, int], str] = {}
                for number, cells in xlsx_stream.iter_sheet_rows(path, name, values=False):
                    for col, value in cells:
                        if isinstance(value, str) and value.startswith("="):
                            formulas[(number, col)] = value
                        elif hasattr(value, "text"):          # ArrayFormula
                            formulas[(number, col)] = str(value.text)
                count = max_row = max_col = 0
                batch = []
                seen = set()
                for number, cells in xlsx_stream.iter_sheet_rows(path, name, values=True):
                    for col, value in cells:
                        batch.append((name, number, col, _text(value), formulas.get((number, col))))
                        seen.add((number, col))
                        count += 1
                        max_row, max_col = max(max_row, number), max(max_col, col)
                    if len(batch) >= 5000:
                        con.executemany("INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?)", batch)
                        batch.clear()
                for (number, col), formula in formulas.items():      # formulas without a cached value
                    if (number, col) not in seen:
                        batch.append((name, number, col, None, formula))
                        count += 1
                        max_row, max_col = max(max_row, number), max(max_col, col)
                con.executemany("INSERT OR REPLACE INTO cells VALUES (?,?,?,?,?)", batch)
                images = anchors.get(name, [])
                con.executemany("INSERT INTO images VALUES (?,?,?,?)", [(name, a.row, a.col, a.media) for a in images])
                con.execute("INSERT INTO sheets VALUES (?,?,?,?,?,?)", (position, name, max_row, max_col, count, len(images)))
            con.execute("INSERT INTO meta VALUES ('file', ?)", (path.name,))
            con.execute("INSERT INTO meta VALUES ('size', ?)", (str(path.stat().st_size),))
            con.commit()
        finally:
            con.close()
        temp.replace(target)
        return target


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(build_index(path))
    con.row_factory = sqlite3.Row
    return con


def _sheet_name(con: sqlite3.Connection, sheet: str | None) -> str:
    names = [r["name"] for r in con.execute("SELECT name FROM sheets ORDER BY position")]
    if not names:
        raise InspectError("В файле нет листов.")
    if not sheet:
        return names[0]
    wanted = sheet.strip().casefold()
    for name in names:
        if name.strip().casefold() == wanted:
            return name
    close = [n for n in names if wanted in n.strip().casefold()]
    if len(close) == 1:
        return close[0]
    raise InspectError(f"Лист «{sheet}» не найден. Листы: {', '.join(n.strip() for n in names)}.")


def outline(path: Path) -> dict:
    with _connect(path) as con:
        sheets = [{"name": r["name"], "rows": r["rows"], "columns": r["cols"], "last_column": column_letter(r["cols"]) if r["cols"] else "",
                   "cells": r["cells"], "photos": r["images"]} for r in con.execute("SELECT * FROM sheets ORDER BY position")]
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    return {"file": meta.get("file"), "size_bytes": int(meta.get("size") or 0), "sheets": sheets,
            "note": "Значения ячеек точные, без сокращений; фото в индекс не входят (отмечено, в каких строках они есть)."}


def rows(path: Path, sheet: str | None = None, start: int = 1, end: int | None = None, columns: list[str] | None = None) -> dict:
    """Exact cells of rows ``start..end`` (at most ``MAX_ROWS`` per page)."""
    start = max(1, int(start or 1))
    end = int(end or start + 49)
    if end < start:
        start, end = end, start
    truncated = end - start + 1 > MAX_ROWS
    end = min(end, start + MAX_ROWS - 1)
    wanted = {column_index(c) for c in columns or [] if re.fullmatch(r"[A-Za-z]{1,3}", c or "")}
    with _connect(path) as con:
        name = _sheet_name(con, sheet)
        info = con.execute("SELECT rows, cols FROM sheets WHERE name = ?", (name,)).fetchone()
        grid: dict[int, dict[str, dict]] = {}
        for r in con.execute("SELECT row, col, value, formula FROM cells WHERE sheet = ? AND row BETWEEN ? AND ? ORDER BY row, col",
                             (name, start, end)):
            if wanted and r["col"] not in wanted:
                continue
            cell = {"v": r["value"]}
            if r["formula"]:
                cell["f"] = r["formula"]
            grid.setdefault(r["row"], {})[column_letter(r["col"])] = cell
        photos: dict[int, list[str]] = {}
        for r in con.execute("SELECT row, part FROM images WHERE sheet = ? AND row BETWEEN ? AND ? ORDER BY row, col", (name, start, end)):
            photos.setdefault(r["row"], []).append(r["part"])
    out = [{"row": n, "cells": grid.get(n, {}), **({"photo": True, "photos": photos[n]} if n in photos else {})}
           for n in range(start, end + 1) if n in grid or n in photos]
    return {"sheet": name, "start": start, "end": end, "sheet_rows": info["rows"], "sheet_columns": column_letter(info["cols"] or 0),
            "rows": out, "next_start": end + 1 if end < (info["rows"] or 0) else None,
            "truncated_to_page": truncated}


def search(path: Path, query: str, sheet: str | None = None, limit: int = MAX_HITS) -> dict:
    """Every cell whose value or formula contains ``query`` (case-insensitive), with its whole row."""
    query = (query or "").strip()
    if len(query) < 2:
        raise InspectError("Что искать? Нужно хотя бы 2 символа.")
    limit = max(1, min(int(limit or MAX_HITS), MAX_HITS))
    with _connect(path) as con:
        names = [_sheet_name(con, sheet)] if sheet else [r["name"] for r in con.execute("SELECT name FROM sheets ORDER BY position")]
        like = f"%{query.casefold()}%"
        con.create_function("fold", 1, lambda s: s.casefold() if isinstance(s, str) else s, deterministic=True)
        hits, total = [], 0
        for name in names:
            matched = con.execute("SELECT row, col, value, formula FROM cells WHERE sheet = ? AND (fold(value) LIKE ? OR fold(formula) LIKE ?) "
                                  "ORDER BY row, col", (name, like, like)).fetchall()
            total += len(matched)
            for r in matched:
                if len(hits) >= limit:
                    break
                row_cells = {column_letter(c["col"]): c["value"] for c in con.execute(
                    "SELECT col, value FROM cells WHERE sheet = ? AND row = ? ORDER BY col", (name, r["row"]))}
                hits.append({"sheet": name, "cell": f"{column_letter(r['col'])}{r['row']}", "row": r["row"], "value": r["value"],
                             **({"formula": r["formula"]} if r["formula"] else {}), "row_values": row_cells})
    return {"query": query, "total": total, "hits": hits, "more": total > len(hits)}


def cell_count(path: Path) -> int:
    with _connect(path) as con:
        return con.execute("SELECT COALESCE(SUM(cells), 0) FROM sheets").fetchone()[0]


# --- Which workbook ----------------------------------------------------------------------

def sources(db) -> list[dict]:
    """Workbooks the assistant may inspect: stored originals and uploads under review."""
    from sqlalchemy import select
    from app.models import ImportBatch
    found = []
    for batch in db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(30)):
        if batch.stored_path and Path(batch.stored_path).suffix.lower() in (".xlsx", ".xlsm") and Path(batch.stored_path).exists():
            found.append({"source": f"batch:{batch.id}", "file": batch.filename, "when": batch.imported_at.isoformat() if batch.imported_at else None})
    from app import factory_routes
    imports = factory_routes.IMPORT_DIR
    if imports.exists():
        for folder in sorted(imports.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
            status_file = folder / "status.json"
            if status_file.exists():
                status = json.loads(status_file.read_text())
                if status.get("workbook") and status.get("state") in ("ready", "committed"):
                    found.append({"source": f"import:{folder.name}", "file": status.get("filename"), "when": status.get("updated"),
                                  "state": status.get("state")})
    return found


def resolve(db, source: str | None, screen: dict | None = None) -> tuple[Path, str]:
    """Path of ``source`` (``batch:<id>`` / ``import:<token>`` / ``workbook:<token>``), else the workbook on screen, else the newest one."""
    from app.models import ImportBatch
    screen = screen or {}
    source = (source or "").strip() or screen.get("workbook_source") or ""
    if not source:
        listed = sources(db)
        if not listed:
            raise InspectError("Нет загруженных файлов Excel. Перетащите файл фабрики на строку ассистента.")
        source = listed[0]["source"]
    kind, _, ident = source.partition(":")
    if kind == "batch" and ident.isdigit():
        batch = db.get(ImportBatch, int(ident))
        if batch and batch.stored_path and Path(batch.stored_path).exists():
            return Path(batch.stored_path), batch.filename
    if kind == "import" and re.fullmatch(r"[a-f0-9]{24}", ident):
        from app import factory_routes
        status_file = factory_routes.IMPORT_DIR / ident / "status.json"
        if status_file.exists():
            status = json.loads(status_file.read_text())
            if status.get("workbook") and Path(status["workbook"]).exists():
                return Path(status["workbook"]), status.get("filename") or "файл"
    if kind == "workbook" and re.fullmatch(r"[a-f0-9]{32}", ident):
        folder = ROOT_DIR / "data" / "workbooks" / ident
        if (folder / "original.xlsx").exists():
            state = json.loads((folder / "state.json").read_text()) if (folder / "state.json").exists() else {}
            return folder / "original.xlsx", state.get("filename") or "файл"
    raise InspectError("Этот файл недоступен. Источники: " + ", ".join(f"{s['source']} ({s['file']})" for s in sources(db)[:8]))
