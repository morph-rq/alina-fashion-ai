"""Screens for the factory workflow: import → review → orders → factories → clients → documents → assistant.

Every change from these screens goes through ``ledger_actions`` (preview, then commit), the
same path the assistant uses, so the numbers shown before saving are the numbers saved.
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import agent, customer_documents, factory_import, ledger_actions as la
from app.calculations import item_amounts, money
from app.config import ROOT_DIR
from app.database import SessionLocal, get_db
from app.factory_ledger import compare_snapshots, season_ledger, settlement_summary
from app.models import (AgentMessage, AllocationReview, AuditLog, Customer, CustomerPricingRule, FactoryAgreement,
                        GeneratedDocument, ImportBatch, Order, OrderItem, Season, SettlementSnapshot)

IMPORT_DIR = ROOT_DIR / "data" / "imports"
ALLOWED = {".xlsx", ".xlsm", ".xls", ".pdf"}
_jobs_lock = threading.Lock()
_cancelled: set[str] = set()


def _token_dir(token: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{24}", token or ""):
        raise HTTPException(404, "Импорт не найден")
    return IMPORT_DIR / token


def _status(token: str) -> dict:
    path = _token_dir(token) / "status.json"
    return json.loads(path.read_text()) if path.exists() else {"state": "missing"}


def _write_status(token: str, **values) -> None:
    path = _token_dir(token) / "status.json"
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(values, updated=datetime.now(timezone.utc).isoformat())
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(current, ensure_ascii=False, default=str))
    temp.replace(path)


def _known_tokens(db: Session) -> set[str]:
    tokens = {c.code.upper() for c in db.scalars(select(Customer))}
    tokens |= {(c.name or "").upper() for c in db.scalars(select(Customer)) if c.name}
    from app.models import CustomerAlias
    tokens |= {a.alias.upper() for a in db.scalars(select(CustomerAlias))}
    return {t for t in tokens if t}


def _analyse(token: str, path: Path, filename: str, season_hint: str | None) -> None:
    """Background job: analyse one uploaded file (nothing is saved to the ledger)."""
    try:
        _write_status(token, state="analysing", progress=0.1, message="Читаю файл…")
        if token in _cancelled:
            _write_status(token, state="cancelled", message="Отменено")
            return
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:5])
            _write_status(token, state="reference", progress=1, pages=len(reader.pages), text=text[:6000],
                          message="PDF сохранён как справочный документ. Суммы из PDF не переносятся автоматически.")
            return
        if suffix == ".xls":
            path = factory_import.convert_legacy(path)
        with SessionLocal() as db:
            known = _known_tokens(db)
        analysis = factory_import.analyze_workbook(path, filename, known, season_hint)
        if token in _cancelled:
            _write_status(token, state="cancelled", message="Отменено")
            return
        (_token_dir(token) / "analysis.json").write_text(analysis.to_json())
        items = analysis.items
        _write_status(token, state="ready", progress=1, message="Готово к проверке", items=len(items),
                      unclear=sum(1 for i in items if not i.confident), workbook=str(path))
    except Exception as exc:  # noqa: BLE001 - shown to the operator; the original file stays stored
        _write_status(token, state="failed", progress=1, message=f"Не удалось прочитать файл: {exc}")


async def accept_uploads(files: list[UploadFile], season: str = "") -> list[str]:
    """Store uploaded factory files (streamed, ≤ 1 GB each) and analyse them in the background."""
    IMPORT_DIR.mkdir(parents=True, exist_ok=True)
    tokens = []
    for upload_file in files:
        name = Path(upload_file.filename or "file").name
        if Path(name).suffix.lower() not in ALLOWED:
            raise HTTPException(422, f"«{name}»: поддерживаются .xlsx, .xlsm, .xls и .pdf")
        token = secrets.token_hex(12)
        folder = IMPORT_DIR / token
        folder.mkdir(parents=True)
        target = folder / ("source" + Path(name).suffix.lower())
        size = 0
        with open(target, "wb") as out:
            while chunk := await upload_file.read(1 << 20):
                size += len(chunk)
                if size > factory_import.MAX_IMPORT_BYTES:
                    out.close()
                    shutil.rmtree(folder, ignore_errors=True)
                    raise HTTPException(413, f"«{name}» больше 1 ГБ.")
                out.write(chunk)
        _write_status(token, state="queued", progress=0, filename=name, size=size, message="В очереди")
        threading.Thread(target=_analyse, args=(token, target, name, (season or "").strip().upper() or None), daemon=True).start()
        tokens.append(token)
    return tokens


def _season(db: Session, name: str | None) -> Season | None:
    if name:
        return db.scalar(select(Season).where(Season.name == name))
    seasons = [s for s in db.scalars(select(Season).order_by(Season.id.desc())) if s.orders]
    return seasons[0] if seasons else db.scalar(select(Season).order_by(Season.id.desc()))


def _seasons(db: Session) -> list[Season]:
    return list(db.scalars(select(Season).order_by(Season.id.desc())))


def _pct(value) -> str:
    return (f"{Decimal(value) * 100:.2f}".rstrip("0").rstrip(".")) if value is not None else ""


def install_factory(app, templates, context):
    templates.env.filters["pct"] = _pct
    # Stored times are UTC; the owner reads her local time.
    templates.env.filters["local"] = lambda value, fmt="%d.%m.%Y %H:%M": (
        (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone().strftime(fmt) if value else "")
    templates.env.filters["media_url"] = lambda path: ("/media/" + path.split("data/images/", 1)[1]) if path and "data/images/" in path else ""

    # --- Import ---------------------------------------------------------------------
    @app.get("/factory-import")
    def import_page(request: Request, db: Session = Depends(get_db)):
        batches = list(db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(20)))
        jobs = []
        if IMPORT_DIR.exists():
            for folder in sorted(IMPORT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
                if (folder / "status.json").exists():
                    jobs.append({"token": folder.name, **json.loads((folder / "status.json").read_text())})
        return templates.TemplateResponse(request, "fx_import.html", context(request, batches=batches, jobs=jobs, seasons=_seasons(db)))

    @app.post("/factory-import")
    async def upload(request: Request, files: list[UploadFile] = File(...), season: str = ""):
        tokens = await accept_uploads(files, season)
        if request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"tokens": tokens})
        return RedirectResponse(f"/factory-import/{tokens[0]}" if len(tokens) == 1 else "/factory-import", 303)

    @app.get("/factory-import/{token}/status")
    def import_status(token: str):
        status = _status(token)
        status.pop("workbook", None)
        return JSONResponse(status)

    @app.get("/factory-import/{token}/image")
    def import_image(token: str, part: str):
        """A photo straight from the uploaded workbook, for the review screen (only parts the analysis listed)."""
        folder = _token_dir(token)
        status = _status(token)
        analysis_path = folder / "analysis.json"
        if not analysis_path.exists() or not status.get("workbook"):
            raise HTTPException(404)
        parts = {i["image_part"] for s in json.loads(analysis_path.read_text())["sheets"] for i in s["items"] if i["image_part"]}
        if part not in parts:
            raise HTTPException(404)
        import zipfile
        from fastapi.responses import Response
        from app.xlsx_stream import read_media
        with zipfile.ZipFile(status["workbook"]) as archive:
            data = read_media(archive, part)
        if data is None:
            raise HTTPException(404, "Фото повреждено в исходном файле")
        kind = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(part.rsplit(".", 1)[-1].lower())
        if kind is None:
            raise HTTPException(415)
        return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/factory-import/{token}/cancel")
    def import_cancel(token: str):
        _token_dir(token)
        _cancelled.add(token)
        status = _status(token)
        if status.get("state") in ("queued", "analysing", "ready", "reference", "failed"):
            _write_status(token, state="cancelled", message="Отменено — в учёт ничего не записано")
        return RedirectResponse("/factory-import", 303)

    @app.get("/factory-import/{token}")
    def review(request: Request, token: str, db: Session = Depends(get_db)):
        status = _status(token)
        analysis = None
        if (_token_dir(token) / "analysis.json").exists():
            analysis = factory_import.Analysis.from_json((_token_dir(token) / "analysis.json").read_text())
        customers = list(db.scalars(select(Customer).order_by(Customer.code)))
        known = {c.code.upper(): c for c in customers}
        from app.models import CustomerAlias
        for alias in db.scalars(select(CustomerAlias)):
            known.setdefault(alias.alias.upper(), alias.customer)
        new_tokens = sorted(t for t in (analysis.tokens if analysis else {}) if t not in known)
        return templates.TemplateResponse(request, "fx_review.html", context(
            request, token=token, status=status, analysis=analysis, customers=customers, known=known,
            new_tokens=new_tokens, owner=next((c for c in customers if c.is_owner), None)))

    @app.post("/factory-import/{token}/commit")
    async def import_commit(request: Request, token: str, db: Session = Depends(get_db)):
        status = _status(token)
        if status.get("state") != "ready":
            raise HTTPException(409, "Файл ещё не готов к сохранению.")
        analysis = factory_import.Analysis.from_json((_token_dir(token) / "analysis.json").read_text())
        form = await request.form(max_fields=20000)
        decisions: dict = {"season": str(form.get("season") or analysis.season or "").upper(), "token_map": {},
                           "rates": {}, "allocations": {}, "brand_names": {}, "skip_sheets": [],
                           "owner": form.get("owner") or None, "rates_confirmed": form.get("rates_confirmed") == "on"}
        for key, value in form.multi_items():
            if key.startswith("map:"):
                decisions["token_map"][key[4:]] = str(value)
            elif key.startswith("brand:"):
                decisions["brand_names"][key[6:]] = str(value).strip()[:200]
            elif key.startswith("skip:"):
                decisions["skip_sheets"].append(key[5:])
            elif key.startswith("rate:"):
                _, sheet, index = key.split(":", 2)
                if str(value).strip():
                    original = next(s for s in analysis.sheets if s.name == sheet)
                    section = original.sections[int(index)] if int(index) < len(original.sections) else None
                    decisions["rates"].setdefault(sheet, []).append({
                        "label": section.label if section else "", "rate": str(la._pct(value)),
                        "rounding": section.rounding if section else "cent",
                        "section": section.label if section and not section.share and len({x.rate for x in original.sections}) > 1 else None})
            elif key.startswith("alloc:"):
                _, sheet, row, unit = key.split(":", 3)
                if str(value):
                    size = str(form.get(f"size:{sheet}:{row}:{unit}") or "") or None
                    decisions["allocations"].setdefault(f"{sheet}:{row}", []).append({"customer": str(value), "size": size, "quantity": 1})
        for key in list(decisions["allocations"]):
            sheet, row = key.rsplit(":", 1)
            item = next((i for s in analysis.sheets if s.name == sheet for i in s.items if i.row == int(row)), None)
            if item is None or len(decisions["allocations"][key]) != item.quantity:
                decisions["allocations"].pop(key)       # partly answered rows stay in review
        # The workbook that was analysed is stored unchanged (for .xls: its .xlsx conversion; the .xls stays in the import folder).
        original = factory_import.store_original(Path(status["workbook"]), analysis.sha256,
                                                 Path(analysis.filename).stem + Path(status["workbook"]).suffix)
        result = factory_import.commit_import(db, analysis, original, decisions)
        payments = [p for i, p in enumerate(analysis.payments) if form.get(f"pay:{i}") == "on"]
        if payments:
            actions = []
            for p in payments:
                payer = str(form.get(f"payer:{analysis.payments.index(p)}") or "")
                if payer:
                    actions.append({"action": "record_payment", "params": {"payer": payer, "amount": p["amount"], "to": "factory",
                                                                           "date": p["date"], "season": result["season"],
                                                                           "reference": f"{analysis.filename}:{p['row']}", "note": p["label"]}})
            if actions:
                la.commit_actions(db, actions, "operator", f"Оплаты из {analysis.filename}")
        _write_status(token, state="committed", batch_id=result.get("batch_id"), message="Сохранено в учёт")
        return RedirectResponse(f"/board?season={result.get('season', '')}", 303)

    @app.get("/originals/{batch_id}")
    def original_file(batch_id: int, db: Session = Depends(get_db)):
        batch = db.get(ImportBatch, batch_id)
        if batch is None or not batch.stored_path or not Path(batch.stored_path).exists():
            raise HTTPException(404, "Оригинал не найден")
        return FileResponse(batch.stored_path, filename=batch.filename)

    # --- Shared preview / commit ----------------------------------------------------
    @app.post("/api/actions/preview")
    async def actions_preview(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        preview = la.preview_actions(db, body.get("actions") or [])
        return JSONResponse({"ok": preview.ok, "error": preview.error, "messages": preview.messages,
                             "diff": [{**r, "before": str(r["before"]), "after": str(r["after"]), "delta": str(r["delta"])} for r in preview.diff]})

    @app.post("/api/actions/commit")
    async def actions_commit(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        try:
            messages = la.commit_actions(db, body.get("actions") or [], "operator", str(body.get("reason") or "Изменение в таблице")[:300])
        except la.ActionError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        return JSONResponse({"ok": True, "messages": messages})

    # --- Orders grid ------------------------------------------------------------------
    @app.get("/board")
    def board(request: Request, season: str = "", brand: str = "", customer: str = "", status: str = "", q: str = "",
              db: Session = Depends(get_db)):
        chosen = _season(db, season)
        rows = []
        if chosen:
            items = db.scalars(select(OrderItem).join(Order).where(Order.season_id == chosen.id)
                               .options(selectinload(OrderItem.order).selectinload(Order.brand), selectinload(OrderItem.customer))).all()
            for item in items:
                if brand and item.order.brand.name != brand:
                    continue
                if customer == "—" and item.customer_id is not None or customer and customer != "—" and (not item.customer or item.customer.code != customer):
                    continue
                if status and item.status != status:
                    continue
                if q and q.casefold() not in " ".join(str(x or "") for x in (item.name, item.article, item.color, item.size,
                                                                               item.source_allocation_description)).casefold():
                    continue
                rows.append(item)
            rows.sort(key=lambda i: (i.order.brand.name.lower(), i.source_row or 0, i.id))
        ledger = season_ledger(db, chosen) if chosen else None
        return templates.TemplateResponse(request, "fx_board.html", context(
            request, season=chosen, seasons=_seasons(db), rows=rows, ledger=ledger,
            brands=sorted({o.brand.name for o in chosen.orders}) if chosen else [],
            customers=list(db.scalars(select(Customer).order_by(Customer.code))), statuses=la.ITEM_STATUSES,
            status_ru=la.STATUS_RU, f={"brand": brand, "customer": customer, "status": status, "q": q},
            amounts={i.id: item_amounts(i) for i in rows},
            reviews=db.scalar(select(func.count()).select_from(AllocationReview).where(AllocationReview.status == "Open")) or 0))

    # --- Factories ------------------------------------------------------------------
    @app.get("/factories")
    def factories(request: Request, season: str = "", brand: str = "", db: Session = Depends(get_db)):
        chosen = _season(db, season)
        order_ids = None
        if chosen and brand:
            order_ids = {o.id for o in chosen.orders if o.brand.name == brand}
        ledger = season_ledger(db, chosen, order_ids) if chosen else None
        agreements = {}
        if chosen:
            for order in chosen.orders:
                agreements[order.id] = db.scalar(select(FactoryAgreement).where(FactoryAgreement.order_id == order.id))
        return templates.TemplateResponse(request, "fx_factories.html", context(
            request, season=chosen, seasons=_seasons(db), ledger=ledger, brand=brand, agreements=agreements,
            describe=la.describe_agreement, customers=list(db.scalars(select(Customer).order_by(Customer.code)))))

    # --- Client pricing ---------------------------------------------------------------
    @app.get("/pricing")
    def pricing(request: Request, season: str = "", db: Session = Depends(get_db)):
        chosen = _season(db, season)
        ledger = season_ledger(db, chosen) if chosen else None
        rules = list(db.scalars(select(CustomerPricingRule).order_by(CustomerPricingRule.customer_id)))
        return templates.TemplateResponse(request, "fx_pricing.html", context(
            request, season=chosen, seasons=_seasons(db), ledger=ledger, rules=rules,
            customers=list(db.scalars(select(Customer).order_by(Customer.code))),
            brands=sorted({o.brand.name for o in chosen.orders}) if chosen else []))

    # --- Documents --------------------------------------------------------------------
    @app.get("/documents")
    def documents(request: Request, season: str = "", db: Session = Depends(get_db)):
        chosen = _season(db, season)
        generated = list(db.scalars(select(GeneratedDocument).order_by(GeneratedDocument.id.desc()).limit(200)))
        outdated = {}
        for doc in generated:
            key = (doc.customer_id, doc.season_id, doc.brand_filter, doc.version)
            if key not in outdated:
                outdated[key] = doc.kind == "customer" and customer_documents.is_outdated(db, doc)
        customers = [c for c in db.scalars(select(Customer).order_by(Customer.code)) if not c.is_owner]
        return templates.TemplateResponse(request, "fx_documents.html", context(
            request, season=chosen, seasons=_seasons(db), generated=generated, outdated=outdated, customers=customers,
            batches=list(db.scalars(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(40))),
            brands=sorted({o.brand.name for o in chosen.orders}) if chosen else []))

    @app.post("/documents/generate")
    async def documents_generate(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        season = la.find_season(db, str(form.get("season") or "") or None)
        brands = [b for b in form.getlist("brands") if b] or None
        codes = [c for c in form.getlist("customer") if c]
        if form.get("all") == "on":
            codes = [c.code for c in db.scalars(select(Customer).where(Customer.is_owner.is_(False)))
                     if any(i.customer_id == c.id and i.status != "Cancelled" for o in season.orders for i in o.items)]
        errors = []
        for code in codes:
            try:
                customer_documents.generate(db, la.find_customer(db, code), season, brands=brands)
            except (ValueError, la.ActionError) as exc:
                errors.append(f"{code}: {exc}")
        suffix = "&error=" + "; ".join(errors) if errors else ""
        return RedirectResponse(f"/documents?season={season.name}{suffix}", 303)

    @app.post("/documents/{doc_id}/issue")
    def documents_issue(doc_id: int, db: Session = Depends(get_db)):
        doc = db.get(GeneratedDocument, doc_id)
        if doc is None:
            raise HTTPException(404)
        try:
            customer_documents.issue(db, doc)
        except ValueError as exc:
            return RedirectResponse(f"/documents?error={exc}", 303)
        return RedirectResponse("/documents", 303)

    @app.get("/documents/bundle")
    def documents_bundle(ids: str = "", db: Session = Depends(get_db)):
        from app.selective_export import bundle
        wanted = [int(x) for x in re.findall(r"\d+", ids)][:200]
        docs = [d for d in (db.get(GeneratedDocument, i) for i in wanted) if d is not None]
        if not docs:
            raise HTTPException(404)
        from fastapi.responses import Response
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        return Response(bundle(docs), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="documents_{stamp}.zip"'})

    @app.get("/documents/{doc_id}/download")
    def documents_download(doc_id: int, db: Session = Depends(get_db)):
        doc = db.get(GeneratedDocument, doc_id)
        if doc is None:
            raise HTTPException(404)
        path = (ROOT_DIR / doc.stored_path).resolve()
        if not path.is_relative_to((ROOT_DIR / "data").resolve()) or not path.exists():
            raise HTTPException(404, "Файл не найден")
        return FileResponse(path, filename=path.name)

    # --- Assistant ----------------------------------------------------------------------
    @app.get("/assistant")
    def assistant_page(request: Request, c: str = ""):
        from app.config import settings
        conversation = c if re.fullmatch(r"[a-f0-9]{16}", c or "") else ""
        return templates.TemplateResponse(request, "fx_assistant.html", context(
            request, conversation=conversation, c=bool(conversation), ai_connected=bool(settings.active_ai_api_key)))

    @app.post("/api/agent/message")
    async def agent_message(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        conversation = str(body.get("conversation") or "")
        if not re.fullmatch(r"[a-f0-9]{16}", conversation):
            raise HTTPException(422, "Неверный диалог")
        channel = "voice" if body.get("channel") == "voice" else "text"
        screen = body.get("screen") if isinstance(body.get("screen"), dict) else {}
        try:
            from starlette.concurrency import run_in_threadpool
            result = await run_in_threadpool(agent.handle, db, conversation, str(body.get("text") or "")[:6000], screen,
                                             None, channel, (str(body.get("raw"))[:6000] if body.get("raw") else None))
        except ValueError as exc:
            return JSONResponse({"reply": str(exc), "error": True, "speech": str(exc)})
        except Exception:       # never a bare 500 in the dialog: say what happened and keep the conversation usable
            import logging
            logging.getLogger(__name__).exception("assistant turn failed")
            db.rollback()
            return JSONResponse({"reply": "Не получилось выполнить запрос — ничего не изменено. Попробуйте ещё раз или скажите иначе.",
                                 "error": True, "retry": True, "speech": "Не получилось. Ничего не изменено, попробуйте ещё раз."})
        from app.voice import to_speech
        codes = [c.code for c in db.scalars(select(Customer))]
        return JSONResponse({"reply": result.reply, "proposal": result.proposal, "question": result.question,
                             "panel": result.panel, "speech": to_speech(result.speech or result.reply, codes),
                             "selection": (result.data or {}).get("selection"), "client": result.client,
                             "tools": [c["name"] for c in result.tool_calls], "steps": agent.describe_steps(result.tool_calls)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/api/agent/proposals/{public_id}/commit")
    def agent_commit(public_id: str, db: Session = Depends(get_db)):
        try:
            return JSONResponse({"ok": True, **agent.commit_proposal(db, public_id)})
        except (ValueError, la.ActionError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)

    @app.post("/api/agent/proposals/{public_id}/cancel")
    def agent_cancel(public_id: str, db: Session = Depends(get_db)):
        try:
            agent.cancel_proposal(db, public_id)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True})

    # --- History ----------------------------------------------------------------------
    @app.get("/history")
    def history_page(request: Request, season: str = "", start: str = "", end: str = "", db: Session = Depends(get_db)):
        chosen = _season(db, season)
        events = list(db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(300)))
        comparison, first, second = [], None, None
        if chosen:
            snaps = list(db.scalars(select(SettlementSnapshot).where(SettlementSnapshot.season_id == chosen.id)
                                    .order_by(SettlementSnapshot.created_at)))

            def at(day: str | None):
                if not day:
                    return None
                limit = datetime.fromisoformat(day + "T23:59:59").replace(tzinfo=timezone.utc)
                found = [s for s in snaps if (s.created_at if s.created_at.tzinfo else s.created_at.replace(tzinfo=timezone.utc)) <= limit]
                return found[-1] if found else None
            first = at(start) if start else (snaps[0] if snaps else None)
            current = json.loads(at(end).data_json) if end and at(end) else settlement_summary(season_ledger(db, chosen))
            if first:
                comparison = compare_snapshots(json.loads(first.data_json), current)
            second = at(end)
        return templates.TemplateResponse(request, "fx_history.html", context(
            request, season=chosen, seasons=_seasons(db), events=events, comparison=comparison, first=first, second=second,
            start=start, end=end))
