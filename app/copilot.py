"""The always-present assistant ("Fashion AI" bar): screen context, navigation, files, hints, history.

Every page publishes what it shows as structured data (page, season, brand, customer, workbook,
sheet, filters, selected rows, visible totals); the bar sends it with each message as ``screen``.
The assistant therefore reads the app's own state instead of screenshots of it.

``CopilotMixin`` adds to the offline interpreter what the bar needs beyond the order conversation:
"Открой фабрики", "Что сейчас открыто?", "Открой загрузку файла", dropped files ("Добавь к
существующему и проверь изменения"), exact workbook inspection ("Покажи строки 20–40 листа Dior cruise",
"Найди 711R07 в файле") and the season-wide deposit table. The same functions back the language-model
tools, so typed, spoken and clicked requests use one implementation.

Nothing here commits data: imports and edits become proposals that the owner confirms.
"""
from __future__ import annotations

import base64
import json
import re
import threading
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ledger_actions as la
from app.calculations import ZERO, money
from app.models import AgentMessage, AgentProposal, AllocationReview, Customer, GeneratedDocument, OrderItem, Season

PAGE_NAMES = {"overview": "Обзор", "workspace": "Заказы", "factories": "Фабрики", "customers": "Клиенты",
              "inbox": "Документы", "assistant": "Ассистент", "settings": "Настройки"}

# Section words → (url, label). Checked in order; the first match wins.
SECTIONS = (
    (r"загрузк|импорт|добав\w* (нов\w* )?(excel|эксел|файл)|загрузи", "/factory-import", "Загрузка файлов"),
    (r"настройк\w* голос|голос\w* настройк", "/settings/voice", "Настройки › Голос"),
    (r"настройк", "/settings", "Настройки"),
    (r"истори|журнал", "/history", "История изменений"),
    (r"фабрик", "/factories", "Фабрики"),
    (r"оплат|платеж|платёж|деньг|финанс", "/money", "Оплаты"),
    (r"клиент|наценк|процент\w* клиент|цены клиент", "/pricing", "Клиенты"),
    (r"документ", "/documents", "Документы"),
    (r"бренд", "/brands", "Бренды"),
    (r"обзор|главн|дашборд|сводк", "/", "Обзор"),
    (r"заказ|таблиц\w* заказ|доск", "/board", "Заказы"),
    (r"ассистент|чат", "/assistant", "Ассистент"),
)
OPEN = re.compile(r"^\s*(открой|перейди|покажи|зайди|давай|переключи|верни\w*|иди|пойд[её]м|открыть)\b", re.I)


def eur(value) -> str:
    return f"€{Decimal(value):,.2f}".replace(",", " ")


def section_for(text: str) -> tuple[str, str] | None:
    lowered = text.casefold()
    for pattern, url, label in SECTIONS:
        if re.search(pattern, lowered):
            return url, label
    return None


def board_url(season: str | None, brand: str | None = None, customer: str | None = None, status: str | None = None) -> str:
    query = {k: v for k, v in (("season", season), ("brand", brand), ("customer", customer), ("status", status)) if v}
    return "/board" + ("?" + urlencode(query) if query else "")


def describe_screen(db: Session, screen: dict) -> tuple[str, list[str]]:
    """A short Russian description of what is open, from the page's own structured state."""
    screen = screen or {}
    page = PAGE_NAMES.get(screen.get("area") or "", screen.get("title") or "страница")
    parts = [page]
    for key in ("season", "brand", "customer", "factory"):
        if screen.get(key):
            parts.append(str(screen[key]))
    facts = []
    if screen.get("status"):
        facts.append(f"Фильтр статуса: {la.STATUS_RU.get(screen['status'], screen['status'])}")
    if screen.get("q"):
        facts.append(f"Поиск: «{screen['q']}»")
    if screen.get("workbook"):
        wb = screen["workbook"]
        facts.append(f"Открыт файл Excel «{wb.get('file')}», лист «{wb.get('sheet') or '—'}»"
                     + (f", ячейка {wb['cell']}" if wb.get("cell") else ""))
    if screen.get("import_token"):
        facts.append("Открыта проверка загруженного файла.")
    if screen.get("rows_visible") is not None:
        facts.append(f"На экране строк: {screen['rows_visible']}")
    for figure in (screen.get("figures") or [])[:6]:
        if figure.get("label") and figure.get("value") not in (None, ""):
            facts.append(f"{figure['label']}: {figure['value']}")
    ids = [int(i) for i in screen.get("selected_item_ids") or [] if str(i).isdigit()][:500]
    if ids:
        items = list(db.scalars(select(OrderItem).where(OrderItem.id.in_(ids[:6]))))
        names = ", ".join(f"{i.name or i.article} ({i.customer.code if i.customer else '—'}, р. {i.size or '—'})" for i in items)
        facts.append(f"Выделено позиций: {len(ids)}" + (f" — {names}" + ("…" if len(ids) > 6 else "") if names else ""))
    if screen.get("document"):
        facts.append(f"Документ: {screen['document']}")
    proposal = screen.get("proposal") or {}
    if proposal.get("id"):
        pending = db.scalar(select(AgentProposal).where(AgentProposal.public_id == proposal["id"]))
        if pending is not None and pending.status == "pending":
            facts.append(f"Ждёт подтверждения: «{pending.summary}»")
    return " → ".join(parts), facts


# --- Proactive hints (grounded in the ledger; at most two per page) ---------------------------

def hints(db: Session, screen: dict) -> list[dict]:
    from app.factory_ledger import season_ledger
    screen = screen or {}
    area = screen.get("area") or ""
    out: list[dict] = []
    open_reviews = db.scalar(select(func.count()).select_from(AllocationReview).where(AllocationReview.status == "Open")) or 0
    if open_reviews and area in ("workspace", "inbox", "overview"):
        out.append({"id": f"reviews-{open_reviews}", "text": f"В файлах фабрик {open_reviews} строк(и) без понятного клиента.",
                    "ask": "Какие строки без клиента?"})
    season = None
    try:
        season = la.find_season(db, screen.get("season"))
    except la.ActionError:
        season = None
    if season is not None and area in ("factories", "customers", "overview", "workspace") and "Money" not in str(screen.get("title")):
        try:
            ledger = season_ledger(db, season)
            waiting = [code for code, c in ledger.settlement.customers.items()
                       if not c.is_owner and c.deposit_due > 0 and c.confirmed_to_factory + c.confirmed_to_owner < c.deposit_due]
        except Exception:  # noqa: BLE001 - a hint must never break a page
            waiting = []
        if waiting:
            out.append({"id": f"deposits-{season.name}-{','.join(sorted(waiting))}",
                        "text": f"{season.name}: у {len(waiting)} клиент(ов) ещё не подтверждена предоплата ({', '.join(sorted(waiting)[:4])}).",
                        "ask": "Посчитай предоплаты"})
    if area == "inbox":
        from app import customer_documents
        stale = [d for d in db.scalars(select(GeneratedDocument).where(GeneratedDocument.kind == "customer")
                                       .order_by(GeneratedDocument.id.desc()).limit(40)) if customer_documents.is_outdated(db, d)]
        codes = sorted({d.customer.code for d in stale if d.customer})
        if codes:
            out.append({"id": f"stale-{','.join(codes)}", "text": f"Документы клиентов {', '.join(codes[:4])} устарели после изменений.",
                        "ask": f"Сделай Excel и PDF заново для {', '.join(codes[:4])}"})
    if area == "workspace" and screen.get("brand") and season is not None:
        from app.assistant_flow import audit_findings
        try:
            findings = audit_findings(db, season, [screen["brand"]])
        except Exception:  # noqa: BLE001
            findings = []
        mismatch = [f for f in findings if "файл" in f.casefold() or "итог" in f.casefold()]
        if mismatch:
            out.append({"id": f"audit-{season.name}-{screen['brand']}-{len(findings)}", "text": mismatch[0],
                        "ask": "Проверь суммы — что не совпадает?"})
    return out[:2]


# --- Conversations ---------------------------------------------------------------------------

def conversations(db: Session, limit: int = 20) -> list[dict]:
    latest = (select(AgentMessage.conversation, func.max(AgentMessage.id).label("last"), func.min(AgentMessage.id).label("first"),
                     func.count().label("n")).group_by(AgentMessage.conversation).order_by(func.max(AgentMessage.id).desc()).limit(limit))
    out = []
    for conversation, last, first, count in db.execute(latest):
        opening = db.scalar(select(AgentMessage).where(AgentMessage.conversation == conversation, AgentMessage.role == "user")
                            .order_by(AgentMessage.id).limit(1))
        last_row = db.get(AgentMessage, last)
        out.append({"id": conversation, "title": (opening.text if opening else "Диалог")[:90], "messages": count,
                    "when": last_row.created_at.isoformat() if last_row and last_row.created_at else None})
    return out


def conversation_payload(db: Session, conversation: str) -> dict:
    from app import agent
    rows = agent.history(db, conversation, 80)
    ids = [json.loads(r.data_json or "{}").get("proposal", {}) for r in rows if r.role == "assistant"]
    wanted = [p.get("id") for p in ids if isinstance(p, dict) and p.get("id")]
    proposals = {p.public_id: agent.proposal_view(p) for p in db.scalars(select(AgentProposal).where(AgentProposal.public_id.in_(wanted)))} if wanted else {}
    messages = []
    for row in rows:
        data = json.loads(row.data_json or "{}")
        entry = {"role": row.role, "text": row.text}
        if row.role == "user" and data.get("raw"):
            entry["raw"] = data["raw"]
        if row.role == "user" and data.get("attachment"):
            entry["attachment"] = data["attachment"]
        if row.role == "assistant":
            proposal = data.get("proposal")
            if isinstance(proposal, dict) and proposal.get("id"):
                entry["proposal"] = proposals.get(proposal["id"], proposal)
            if data.get("question"):
                entry["question"] = data["question"]
        messages.append(entry)
    panel = next((json.loads(r.data_json or "{}").get("panel") for r in reversed(rows)
                  if r.role == "assistant" and json.loads(r.data_json or "{}").get("panel")), None)
    return {"conversation": conversation, "messages": messages, "panel": panel,
            "selection": agent.memory_of(db, conversation).get("selection")}


# --- Attached files --------------------------------------------------------------------------

def import_status(token: str) -> dict:
    from app.factory_routes import _status
    return _status(token)


def load_analysis(token: str):
    from app import factory_import
    from app.factory_routes import _token_dir
    path = _token_dir(token) / "analysis.json"
    return factory_import.Analysis.from_json(path.read_text()) if path.exists() else None


def _index_later(path: str | None) -> None:
    if not path:
        return
    from app import workbook_inspect
    cache_dir = workbook_inspect.CACHE_DIR       # fixed now: the thread may outlive the caller's settings

    def run():
        try:
            workbook_inspect.build_index(Path(path), cache_dir)
        except Exception:  # noqa: BLE001 - the index is built again on first use
            pass
    threading.Thread(target=run, daemon=True).start()


def pdf_articles(text: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{6,}\b", text or "")))


def attachment_turn(db: Session, conversation: str, token: str) -> dict:
    """What the assistant says when a dropped file has been read. Returns TurnResult kwargs."""
    status = import_status(token)
    state = status.get("state")
    name = status.get("filename") or "файл"
    memory_update = {"attachment": {"token": token, "filename": name}}
    if state in ("queued", "analysing"):
        return {"reply": f"Читаю «{name}»…", "pending": True}
    if state == "failed":
        return {"reply": f"«{name}»: {status.get('message')}", "context": memory_update}
    if state == "reference":          # a PDF: compare its article numbers with the orders
        articles = pdf_articles(status.get("text") or "")
        memory_update["attachment"]["kind"] = "pdf"
        lines = [f"PDF «{name}», страниц: {status.get('pages')}. Суммы из PDF автоматически не переносятся."]
        rows = []
        if articles:
            normalized = {re.sub(r"\s+", "", a).upper(): a for a in articles}
            items = list(db.scalars(select(OrderItem).where(OrderItem.article.is_not(None))))
            found: dict[str, list[OrderItem]] = {}
            for item in items:
                key = re.sub(r"\s+", "", item.article or "").upper()
                if key in normalized:
                    found.setdefault(key, []).append(item)
            missing = [a for k, a in normalized.items() if k not in found]
            lines.append(f"Артикулов в PDF: {len(normalized)}; есть в заказах: {len(found)}; нет в заказах: {len(missing)}.")
            if missing:
                lines.append("Нет в заказах: " + ", ".join(missing[:15]) + ("…" if len(missing) > 15 else ""))
            for key, group in found.items():
                first = group[0]
                rows.append([normalized[key], first.order.brand.name, first.name or "", ", ".join(sorted({i.customer.code for i in group if i.customer})),
                             str(sum(i.quantity for i in group if i.status != "Cancelled"))])
            for article in missing:
                rows.append([article, "—", "нет в заказах", "", ""])
        else:
            lines.append("Артикулов (кодов изделий) в тексте PDF не нашла — возможно, это скан. Откройте его в Документах.")
        panel = {"attachment": {"name": name, "kind": "pdf", "pages": status.get("pages")},
                 **({"table": {"title": "Артикулы из PDF и заказы", "columns": ["Артикул", "Заказ", "Изделие", "Клиенты", "Шт."], "rows": rows[:200]}} if rows else {})}
        return {"reply": "\n".join(lines), "context": memory_update, "panel": panel,
                "speech": f"Прочитала PDF. Сравнила артикулы с заказами — результат на экране."}
    analysis = load_analysis(token)
    if analysis is None:
        return {"reply": f"«{name}»: {status.get('message') or 'файл ещё не прочитан'}", "context": memory_update}
    _index_later(status.get("workbook"))
    from app.revision import known_customers
    known = known_customers(db)
    orders = [s for s in analysis.sheets if s.kind == "order"]
    unclear = sum(1 for s in orders for i in s.items if not i.confident)
    unknown = sorted(t for t, n in analysis.tokens.items() if t.upper() not in known and n)
    season = db.scalar(select(Season).where(Season.name == (analysis.season or ""))) if analysis.season else None
    brands = [s.brand for s in orders]
    existing = {o.brand.name.casefold() for o in season.orders} if season else set()
    overlap = [b for b in brands if b.casefold() in existing]
    memory_update["attachment"].update(kind="xlsx", season=analysis.season, brands=brands, workbook_source=f"import:{token}")
    title = (f"новый файл {brands[0]}" if len(brands) == 1 else f"файл с {len(brands)} заказами ({', '.join(brands[:4])}{'…' if len(brands) > 4 else ''})")
    lines = [f"Я вижу {title}: «{name}», сезон {analysis.season or 'не указан'}, позиций {sum(len(s.items) for s in orders)}."]
    findings = []
    if unclear:
        findings.append(f"{unclear} строк(и) без понятного клиента.")
    if unknown:
        findings.append("Новые обозначения клиентов: " + ", ".join(unknown[:10]) + ".")
    findings += analysis.warnings[:4]
    if findings:
        lines.append("Заметила:\n" + "\n".join(f"• {f}" for f in findings))
    if season and overlap:
        question = {"question": f"Добавить к существующему сезону {season.name} (там уже есть {', '.join(overlap[:3])}) или создать новый?",
                    "options": ["Добавь к существующему и проверь изменения", "Создай новый сезон", "Открой проверку импорта"]}
    elif season:
        question = {"question": f"Добавить эти заказы в сезон {season.name}?",
                    "options": ["Добавь к существующему и проверь изменения", "Создай новый сезон", "Открой проверку импорта"]}
    else:
        question = {"question": f"Сезона {analysis.season or ''} ещё нет. Создать его и добавить заказы?".replace("  ", " "),
                    "options": ["Создай новый сезон и добавь", "Открой проверку импорта"]}
    lines.append(question["question"])
    sheets = [{"brand": s.brand, "sheet": s.name.strip(), "items": len(s.items), "photos": sum(1 for i in s.items if i.image_part),
               "unclear": sum(1 for i in s.items if not i.confident), "exists": s.brand.casefold() in existing} for s in orders]
    panel = {"attachment": {"name": name, "kind": "xlsx", "size": status.get("size"), "season": analysis.season, "sheets": sheets,
                            "review": f"/factory-import/{token}"}}
    speech = f"Я вижу {title}. {question['question']}"
    return {"reply": "\n".join(lines), "question": question, "context": memory_update, "panel": panel, "speech": speech}


# --- Routes ----------------------------------------------------------------------------------

def install_copilot(app, templates, context, get_db):
    from app import agent

    def copilot_config() -> dict:
        from app.config import settings
        from app.voice import status as voice_status
        try:
            voice = voice_status()
        except Exception:  # noqa: BLE001 - the bar must render even if the secret store is unreadable
            voice = {"configured": False}
        return {"voice": {k: voice.get(k) for k in ("configured", "speak_replies", "review_before_send")},
                "ai": bool(settings.active_ai_api_key)}
    templates.env.globals["copilot_config"] = copilot_config

    def _conversation(value) -> str:
        value = str(value or "")
        if not re.fullmatch(r"[a-f0-9]{16}", value):
            raise HTTPException(422, "Неверный диалог")
        return value

    @app.get("/api/agent/conversations")
    def list_conversations(db: Session = Depends(get_db)):
        return JSONResponse({"conversations": conversations(db)}, headers={"Cache-Control": "no-store"})

    @app.get("/api/agent/conversations/{conversation}")
    def get_conversation(conversation: str, db: Session = Depends(get_db)):
        return JSONResponse(conversation_payload(db, _conversation(conversation)), headers={"Cache-Control": "no-store"})

    @app.post("/api/agent/hints")
    async def page_hints(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        screen = body.get("screen") if isinstance(body.get("screen"), dict) else {}
        return JSONResponse({"hints": hints(db, screen)}, headers={"Cache-Control": "no-store"})

    @app.post("/api/agent/attach")
    async def attach(request: Request, files: list[UploadFile] = File(...), conversation: str = Form(""), season: str = Form("")):
        """Files dropped on the bar: stored and analysed exactly like Загрузка (nothing enters the ledger)."""
        from app.factory_routes import accept_uploads
        _conversation(conversation)
        tokens = await accept_uploads(files, season)
        return JSONResponse({"tokens": tokens})

    @app.post("/api/agent/attachment")
    async def attachment(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        conversation = _conversation(body.get("conversation"))
        token = str(body.get("token") or "")
        if not re.fullmatch(r"[a-f0-9]{24}", token):
            raise HTTPException(422, "Неверный файл")
        out = attachment_turn(db, conversation, token)
        if out.get("pending"):
            return JSONResponse({"pending": True, "reply": out["reply"]})
        status = import_status(token)
        memory = agent.memory_of(db, conversation)
        db.add(AgentMessage(conversation=conversation, role="user", text=f"📎 {status.get('filename') or 'файл'}",
                            data_json=json.dumps({"attachment": {"token": token, "name": status.get("filename")}}, ensure_ascii=False)))
        context_data = {**memory, **(out.get("context") or {})}
        db.add(AgentMessage(conversation=conversation, role="assistant", text=out["reply"][:8000],
                            data_json=json.dumps({"question": out.get("question"), "context": context_data, "panel": out.get("panel"),
                                                  "speech": out.get("speech")}, ensure_ascii=False, default=str)))
        db.commit()
        from app.voice import to_speech
        codes = [c.code for c in db.scalars(select(Customer))]
        return JSONResponse({"reply": out["reply"], "question": out.get("question"), "panel": out.get("panel"),
                             "speech": to_speech(out.get("speech") or out["reply"], codes)})

    @app.post("/api/agent/screen-frame")
    async def screen_frame(request: Request, db: Session = Depends(get_db)):
        """One still frame from a screen the owner chose to share. Kept in memory for this request only."""
        from app.config import settings
        body = await request.json()
        conversation = _conversation(body.get("conversation"))
        question = str(body.get("text") or "Что на этом экране?")[:2000]
        image = str(body.get("image") or "")
        match = re.fullmatch(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=]+)", image)
        if not match or len(image) > 12_000_000:
            raise HTTPException(422, "Нет снимка экрана")
        if not settings.active_ai_api_key or "claude" not in (settings.active_ai_model or "").lower():
            reply = ("Снимок окна получен, но прочитать изображение я могу только с подключённым ИИ Claude (Настройки › ИИ). "
                     "Без него я вижу данные Fashion напрямую — спросите про открытую страницу, или перетащите сам файл Excel/PDF сюда.")
            return JSONResponse({"reply": reply, "speech": "Чтобы читать чужие окна, нужно подключить ИИ в настройках.", "vision": False})
        from starlette.concurrency import run_in_threadpool
        try:
            body_out = await run_in_threadpool(agent.ClaudeClient().complete, agent.SYSTEM + "\nТебе показали снимок окна Mac. Опиши и ответь на вопрос; "
                                               "числа переписывай точно как на снимке и скажи, если что-то не читается.",
                                               [{"role": "user", "content": [
                                                   {"type": "image", "source": {"type": "base64", "media_type": f"image/{match.group(1)}", "data": match.group(2)}},
                                                   {"type": "text", "text": question}]}], [])
        except ValueError as exc:
            return JSONResponse({"reply": str(exc), "vision": False})
        reply = "".join(b.get("text", "") for b in body_out.get("content", []) if b.get("type") == "text").strip() or "Не смогла прочитать снимок."
        db.add(AgentMessage(conversation=conversation, role="user", text=f"🖥 {question}", data_json=json.dumps({"screen_share": True})))
        db.add(AgentMessage(conversation=conversation, role="assistant", text=reply[:8000], data_json="{}"))
        db.commit()
        return JSONResponse({"reply": reply, "vision": True})

    @app.get("/documents/{doc_id}/view")
    def document_view(request: Request, doc_id: int, db: Session = Depends(get_db)):
        """Preview a generated file in the browser: PDF inline, Excel as a table with its embedded photos."""
        from fastapi.responses import FileResponse, HTMLResponse
        from app.config import ROOT_DIR
        doc = db.get(GeneratedDocument, doc_id)
        if doc is None:
            raise HTTPException(404)
        path = (ROOT_DIR / doc.stored_path).resolve()
        if not path.is_relative_to((ROOT_DIR / "data").resolve()) or not path.exists():
            raise HTTPException(404, "Файл не найден")
        if path.suffix.lower() == ".pdf":
            return FileResponse(path, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{path.name}"'})
        return HTMLResponse(xlsx_preview_html(path, doc_id))

    @app.get("/documents/{doc_id}/image")
    def document_image(doc_id: int, part: str, db: Session = Depends(get_db)):
        import zipfile
        from fastapi.responses import Response
        from app.config import ROOT_DIR
        from app.xlsx_stream import read_media
        doc = db.get(GeneratedDocument, doc_id)
        if doc is None or not part.startswith("xl/media/"):
            raise HTTPException(404)
        path = (ROOT_DIR / doc.stored_path).resolve()
        if not path.is_relative_to((ROOT_DIR / "data").resolve()) or path.suffix.lower() not in (".xlsx", ".xlsm"):
            raise HTTPException(404)
        with zipfile.ZipFile(path) as archive:
            data = read_media(archive, part)
        if data is None:
            raise HTTPException(404)
        kind = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(part.rsplit(".", 1)[-1].lower())
        return Response(data, media_type=kind or "application/octet-stream")

    @app.get("/api/inspect/rows")
    def inspect_rows(source: str, sheet: str = "", start: int = 1, end: int = 0, db: Session = Depends(get_db)):
        from app import workbook_inspect as wi
        try:
            path, _ = wi.resolve(db, source)
            return JSONResponse(wi.rows(path, sheet or None, start, end or start + 49))
        except wi.InspectError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)


# --- The interpreter part (mixed into agent.RuleInterpreter) -------------------------------------

class CopilotMixin:
    """Needs .db, .screen, .memory, .done, .tool, .pending (see agent.RuleInterpreter)."""

    def copilot_flow(self, text: str):
        lowered = text.casefold().strip()
        if re.search(r"что (сейчас )?(открыто|на экране|я смотрю|выделено|видишь)|где (я|мы)( сейчас)?\b|что ты видишь", lowered):
            return self.what_is_open()
        if re.search(r"(открой|покажи|начни|запусти|выбери)\s+(окно |форму )?(загрузк|выбор файл)|загрузи (нов\w* )?(файл|excel|эксел)|добавь нов\w* (excel|эксел|файл)|выбрать файл",
                     lowered):
            return self.done("Открываю выбор файла. Можно и просто перетащить Excel или PDF на строку ассистента.", None, self.memory,
                             client={"open_upload": True}, speech="Выберите файл.")
        attachment = self.memory.get("attachment") or {}
        if attachment.get("token") and import_status(attachment["token"]).get("state") not in ("committed", "cancelled", "missing"):
            handled = self.attachment_followup(text, attachment)
            if handled is not None:
                return handled
        exporting = re.search(r"экспорт|выгруз|сделай|подготов|\bpdf\b|пдф|отправ", lowered)
        if not exporting and (
                re.search(r"(строк\w*|ряд\w*|ячейк\w*)\s*(с\s*)?[A-Z]?\d", text, re.I) and re.search(r"лист|файл|оригинал|excel|эксел", lowered)
                or re.search(r"^\s*(найди|поиск|ищи|где)\b.*\b(в файле|в excel|в эксель|в оригинале|в таблице фабрики)", lowered)
                or re.search(r"какие листы|что в файле|структур\w* файла|^\s*дальше\s*$", lowered) and (re.search(r"дальше", lowered) is None or self.memory.get("inspect"))):
            return self.inspect(text)
        if (re.search(r"^\s*(посчитай|рассчитай|пересчитай|покажи)\s+(все\s+)?предоплат|сколько\s+кажд\w*.*(заплат|внест|долж)|кажд\w* (девушк|клиент)\w*.*(заплат|внест|долж)", lowered)
                and not re.search(r"\d\s?%|\b(убери|удали|отмени|поставь)\b", lowered)):
            result = self.deposit_table(text)
            if OPEN.search(lowered) and result.data and result.data.get("brand"):
                result.client = {"navigate": board_url(result.data.get("season"), result.data["brand"])}
            return result
        if OPEN.search(lowered):
            return self.navigate(text)
        return None

    # --- screen -------------------------------------------------------------------------------
    def what_is_open(self):
        where, facts = describe_screen(self.db, self.screen)
        selection = self.memory.get("selection")
        reply = f"Сейчас открыто: {where}." + ("\n" + "\n".join(f"• {f}" for f in facts) if facts else "")
        if selection and (selection.get("brands") or selection.get("customers")):
            reply += "\nВ разговоре выбрано: " + " → ".join(filter(None, [selection.get("season"), ", ".join(selection.get("brands") or []),
                                                                            ", ".join(selection.get("customers") or [])]))
        return self.done(reply, None, self.memory, speech=f"Сейчас открыто: {where}.")

    # --- navigation ---------------------------------------------------------------------------
    def navigate(self, text: str):
        from app import selection as sel
        lowered = text.casefold()
        season = self.screen.get("season") or self.memory.get("season")
        if not season:
            try:
                season = la.find_season(self.db, None).name
            except la.ActionError:
                season = None
        previous = sel.Filters.from_dict(self.memory["selection"]) if self.memory.get("selection") else None
        filters, question, changed = sel.parse(self.db, text, previous, self.screen)
        names_scope = bool(changed & {"brands", "customers"})
        section = section_for(text)
        if question and not section:
            return self.done(question["question"], question, self.memory)
        if names_scope and (section is None or section[0] in ("/board", "/factories", "/pricing")):
            selection = sel.resolve(self.db, filters, self.screen)
            brand = filters.brands[0] if len(filters.brands) == 1 else None
            customer = filters.customers[0] if len(filters.customers) == 1 else None
            if section and section[0] == "/factories":
                url = "/factories?" + urlencode({k: v for k, v in (("season", selection.season.name if selection.season else season), ("brand", brand)) if v})
            elif section and section[0] == "/pricing":
                url = "/pricing?" + urlencode({"season": selection.season.name}) if selection.season else "/pricing"
            else:
                url = board_url(selection.season.name if selection.season else season, brand, customer,
                                {"ready": "Ready", "cancelled": "Cancelled"}.get(filters.readiness or "", None))
            table = sel.selection_table(selection)
            count = len(selection.items)
            reply = f"{selection.summary}. Открываю." if count else f"{selection.summary}. Позиции не найдены — проверьте бренд или клиента."
            context = self.context_from(selection)
            return self.done(reply, None, context, panel={"selection": table}, client={"navigate": url},
                             speech=f"Открываю. {count} позиций." if count else "Ничего не нашла.")
        if section:
            url, label = section
            if url in ("/board", "/factories", "/pricing", "/documents") and season:
                url = f"{url}?{urlencode({'season': season})}"
            return self.done(f"Открываю: {label}.", None, self.memory, client={"navigate": url}, speech=f"Открываю {label}.")
        return None

    # --- attached workbook -----------------------------------------------------------------------
    def attachment_followup(self, text: str, attachment: dict):
        from app import revision
        lowered = text.casefold()
        token = attachment["token"]
        if re.search(r"проверк\w* импорт|открой (импорт|проверк|файл)|сам\w* провер|вручную", lowered):
            return self.done("Открываю проверку файла: там видно каждую строку с фото и кому она относится.", None, self.memory,
                             client={"navigate": f"/factory-import/{token}"})
        if attachment.get("kind") == "pdf":
            return None
        merge = re.search(r"добав|существ|сравни|провер\w* измен|обнов|что (нового|изменилось)|изменени", lowered)
        new_season = re.search(r"нов\w* сезон|созда\w* (нов\w* )?сезон", lowered)
        create = re.search(r"созда\w* (их|новых|клиент)|нов\w* клиент|добав\w* (их|новых) клиент", lowered)
        skip = re.search(r"без клиента|оставь (без|пуст)|разбер\w* потом|не созда", lowered)
        cancel_missing = re.search(r"отмени\w* (те, )?(которых|чего|отсутству)|(нет|пропал\w*) .*отмени|отмени .*нет в файле", lowered)
        if not (merge or new_season or create or skip or cancel_missing):
            return None
        analysis = load_analysis(token)
        if analysis is None:
            return self.done("Файл ещё читается — подождите несколько секунд.", None, self.memory)
        season_name = attachment.get("season") or analysis.season
        if new_season:
            named = re.search(r"\b(SS|FW|AW|PF|RS)\s?(\d{2})\b", text, re.I)
            if named:
                season_name = named.group(1).upper() + named.group(2)
            elif self.db.scalar(select(Season).where(Season.name == season_name)) is not None:
                return self.done(f"Сезон {season_name} уже есть. Как назвать новый сезон?",
                                 {"question": "Название нового сезона?", "options": []}, self.memory)
        if not season_name:
            return self.done("В файле не указан сезон. Для какого сезона?", {"question": "Какой сезон?", "options": [
                s.name for s in self.db.scalars(select(Season).order_by(Season.id.desc()).limit(4))]}, self.memory)
        options = dict(attachment.get("options") or {})
        if create:
            options["create_unknown"] = True
        if skip:
            options["create_unknown"] = False
        if cancel_missing:
            options["cancel_missing"] = True
        diff = revision.compare(self.db, analysis, season_name, {t: "new" for t in (analysis.tokens if options.get("create_unknown") else {})})
        lines = revision.summary_lines(diff)
        actions = revision.update_actions(diff, analysis.filename, bool(options.get("cancel_missing")))
        new_units = sum(int(r["quantity"]) for r in diff["new"] if options.get("create_unknown") or not r["key"][3].startswith("?"))
        nothing = not actions and not new_units and not diff["new_orders"] and not diff["unresolved_new"]
        attachment = {**attachment, "season": season_name, "options": options}
        context = {**self.memory, "attachment": attachment, "season": season_name}
        panel = {"revision": {**{k: diff[k][:200] for k in ("new", "changed", "cancelled", "missing", "duplicates", "unresolved", "new_orders")},
                              "unknown_customers": diff["unknown_customers"], "warnings": diff["warnings"], "season": season_name,
                              "file": analysis.filename, "review": f"/factory-import/{token}"}}
        if nothing:
            reply = f"Сравнила «{analysis.filename}» с {season_name}: новых позиций и изменений нет." + \
                ("\n" + "\n".join(f"• {l}" for l in lines) if lines else "")
            return self.done(reply, None, context, panel=panel, speech="Сравнила: изменений нет.")
        preview = la.preview_actions(self.db, actions) if actions else None
        if preview is not None and not preview.ok:
            return self.done(f"Не могу подготовить изменения: {preview.error}", None, context, panel=panel)
        will_do = list(preview.messages if preview else [])
        if new_units:
            will_do.append(f"Добавить {new_units} шт. новых позиций из файла.")
        for order in diff["new_orders"]:
            will_do.append(f"Создать заказ {order['brand']} в {season_name}: {order['items']} поз., база {eur(order['base'])}.")
        if diff["unresolved_new"]:
            will_do.append(f"{len(diff['unresolved_new'])} строк(и) без понятного клиента — добавлю на проверку (без клиента).")
        if diff["missing"] and not options.get("cancel_missing"):
            will_do.append(f"{len(diff['missing'])} позиций нет в новом файле — оставляю как есть (скажите «отмени те, которых нет в файле»).")
        if diff["unknown_customers"]:
            will_do.append(("Новые клиенты будут созданы: " if options.get("create_unknown") else "Без клиента останутся строки с новыми обозначениями: ")
                           + ", ".join(diff["unknown_customers"]))
        if re.search(r"раздел|\d\s?%|предоплат|excel|эксел|pdf|пдф|документ", lowered):
            self.queue_text = text          # the rest of a multi-step request runs after the import is confirmed
        self.pending.summary = f"Файл {analysis.filename} → {season_name}"
        self.pending.actions.extend(actions)
        self.pending.imports.append({"token": token, "season": season_name, **options})
        self.pending.preview = {"messages": will_do, "diff": preview.diff if preview else []}
        reply = (f"Сравнила «{analysis.filename}» с заказами {season_name}:\n" + "\n".join(f"• {l}" for l in lines)
                 + "\n\nПредлагаю:\n" + "\n".join(f"• {w}" for w in will_do) + "\n\nПодтвердите кнопкой — без неё ничего не запишется.")
        question = None
        if diff["unknown_customers"] and "create_unknown" not in options:
            question = {"question": "Новые обозначения клиентов: " + ", ".join(diff["unknown_customers"][:8]) + ". Что с ними?",
                        "options": ["Создай новых клиентов", "Оставь без клиента"]}
        count = len(diff["new"]) + len(diff["changed"]) + len(diff["cancelled"])
        return self.done(reply, question, context, panel=panel,
                         speech=f"Сравнила файл. Изменений: {count}. Проверьте список на экране и подтвердите кнопкой.")

    # --- exact workbook inspection ------------------------------------------------------------------
    def inspect(self, text: str):
        from app import workbook_inspect as wi
        attachment = self.memory.get("attachment") or {}
        source = (self.screen.get("workbook") or {}).get("source") or (self.memory.get("inspect") or {}).get("source") \
            or attachment.get("workbook_source")
        try:
            path, filename = wi.resolve(self.db, source, self.screen)
        except wi.InspectError as exc:
            return self.done(str(exc), None, self.memory)
        lowered = text.casefold()
        outline = wi.outline(path)
        sheet = None
        for s in outline["sheets"]:
            if s["name"].strip().casefold() in lowered:
                sheet = s["name"]
                break
        if sheet is None:
            sheet = (self.screen.get("workbook") or {}).get("sheet")
        source_ref = source or wi.sources(self.db)[0]["source"]
        context = {**self.memory, "inspect": {"source": source_ref, "sheet": sheet}}
        found = re.search(r"(?:найди|поиск|ищи|где)\s+[«\"']?([^«»\"']{2,60}?)[»\"']?\s+(?:в файле|в excel|в эксель|в оригинале|в таблице)", text, re.I)
        if found:
            out = wi.search(path, found.group(1).strip(), sheet if sheet and "лист" in lowered else None)
            rows = [[h["sheet"].strip(), h["cell"], h["value"] or h.get("formula") or "",
                     " | ".join(f"{k}: {v}" for k, v in h["row_values"].items() if v not in (None, ""))] for h in out["hits"]]
            reply = (f"«{out['query']}» в «{filename}»: найдено {out['total']}" + (f", показываю первые {len(rows)}" if out["more"] else "") + "."
                     if out["total"] else f"«{out['query']}» в «{filename}» не найдено.")
            return self.done(reply, None, context, panel={"table": {"title": f"Поиск: {out['query']}", "columns": ["Лист", "Ячейка", "Значение", "Вся строка"],
                                                                     "rows": rows}}, speech=f"Нашла {out['total']}." if out["total"] else "Не нашла.")
        if re.search(r"^\s*дальше\s*$", lowered) and (self.memory.get("inspect") or {}).get("next"):
            last = self.memory["inspect"]
            sheet, lowered = last.get("sheet"), f"строки {last['next']}-{last['next'] + 49} лист"
        span = re.search(r"(?:строк\w*|ряд\w*)\s*(?:с\s*)?(\d{1,7})\s*(?:-|–|—|по|до)\s*(\d{1,7})", lowered)
        single = re.search(r"(?:строк\w*|ряд\w*)\s*(\d{1,7})\b", lowered)
        if span or single or sheet and "лист" in lowered:
            start = int(span.group(1)) if span else int(single.group(1)) if single else 1
            end = int(span.group(2)) if span else start if single else start + 49
            try:
                page = wi.rows(path, sheet, start, end)
            except wi.InspectError as exc:
                return self.done(str(exc), None, context)
            letters = sorted({c for r in page["rows"] for c in r["cells"]}, key=wi.column_index)
            rows = [[str(r["row"]) + (" 📷" if r.get("photo") else "")] + [
                (r["cells"].get(c, {}).get("v") or "") + (f"  ⟨{r['cells'][c]['f']}⟩" if r["cells"].get(c, {}).get("f") else "") for c in letters]
                for r in page["rows"]]
            reply = (f"«{filename}», лист «{page['sheet'].strip()}», строки {page['start']}–{page['end']} из {page['sheet_rows']} "
                     f"(колонки до {page['sheet_columns']}). Значения точные, как в файле; формулы — в ⟨ ⟩.")
            if page["truncated_to_page"]:
                reply += f" Показываю по {wi.MAX_ROWS} строк — скажите «дальше»."
            return self.done(reply, None, {**context, "inspect": {"source": source_ref, "sheet": page["sheet"], "next": page["next_start"]}},
                             panel={"table": {"title": f"{page['sheet'].strip()} · строки {page['start']}–{page['end']}", "columns": ["№"] + letters,
                                              "rows": rows, "grid": True}}, speech="Показала строки на экране.")
        rows = [[s["name"].strip(), str(s["rows"]), s["last_column"], str(s["cells"]), str(s["photos"])] for s in outline["sheets"]]
        reply = f"«{filename}»: {len(rows)} лист(ов). Могу показать любые строки («покажи строки 1–40 листа …») или найти значение («найди 711R07 в файле»)."
        return self.done(reply, None, context, panel={"table": {"title": filename, "columns": ["Лист", "Строк", "До колонки", "Ячеек", "Фото"], "rows": rows}},
                         speech=f"В файле {len(rows)} листов. Список на экране.")

    # --- every customer's deposit ---------------------------------------------------------------------
    def deposit_table(self, text: str):
        from app import selection as sel
        filters, question, _ = sel.parse(self.db, text, sel.Filters.from_dict(self.memory["selection"]) if self.memory.get("selection") else None, self.screen)
        if question:
            return self.done(question["question"], question, self.memory)
        try:
            season = la.find_season(self.db, filters.season)
        except la.ActionError as exc:
            return self.done(str(exc), None, self.memory)
        brand = filters.brands[0] if len(filters.brands) == 1 else None
        s = self.tool("calculate_factory_settlement", {"season": season.name, "brand": brand})
        if "error" in s:
            return self.done(s["error"], None, self.memory)
        return self.done(*settlement_answer(s, brand), self.context_from(sel.resolve(self.db, filters, self.screen)) if filters.brands else
                         {**self.memory, "season": season.name},
                         panel=settlement_panel(s), speech=settlement_speech(s))


def settlement_rows(s: dict) -> list[list[str]]:
    return [[code, eur(c["total"]), eur(c["deposit_due"]), eur(c["paid_confirmed"]), eur(c["deposit_outstanding"])]
            for code, c in sorted(s["customers"].items()) if not c["owner"]]


def settlement_answer(s: dict, brand: str | None) -> tuple[str, None]:
    own = Decimal(s["owner_net_deposit_planned"])
    total = Decimal(s["factory_total"])
    rate = f"{(Decimal(s['factory_deposit_required']) * 100 / total).quantize(Decimal('0.01')).normalize():f}%" if total else "—"
    lines = [f"{s['season']} · {brand or 'весь сезон'} — предоплата {rate} от суммы фабрики {eur(s['factory_total'])}:"]
    for code, c in sorted(s["customers"].items()):
        if not c["owner"]:
            lines.append(f"• {code}: заказ {eur(c['total'])}, предоплата {eur(c['deposit_due'])}, оплачено {eur(c['paid_confirmed'])}, "
                         f"осталось {eur(c['deposit_outstanding'])}")
    lines.append(f"Предоплата фабрике: {eur(s['factory_deposit_required'])}; депозиты клиентов: {eur(s['external_customer_deposits_expected'])}.")
    if own >= 0:
        lines.append(f"Ваш взнос: {eur(own)} (предоплата фабрике минус депозиты клиентов).")
    else:
        lines.append(f"Депозиты клиентов покрывают предоплату фабрике с запасом {eur(-own)} — ваш взнос не нужен.")
    if Decimal(s["confirmed_paid_to_factory"]):
        lines.append(f"Фабрике уже подтверждено: {eur(s['confirmed_paid_to_factory'])}; по предоплате осталось {eur(s['factory_deposit_still_outstanding'])}.")
    return "\n".join(lines), None


def settlement_panel(s: dict) -> dict:
    own = Decimal(s["owner_net_deposit_planned"])
    return {"table": {"title": f"Предоплаты · {s['season']} · {s['scope']}", "columns": ["Клиент", "Заказ", "Предоплата", "Оплачено", "Осталось"],
                      "rows": settlement_rows(s), "numeric": [1, 2, 3, 4]},
            "figures": [{"label": "Сумма фабрике", "value": s["factory_total"]},
                        {"label": "Предоплата фабрике", "value": s["factory_deposit_required"]},
                        {"label": "Депозиты клиентов", "value": s["external_customer_deposits_expected"]},
                        {"label": "Ваш взнос", "value": str(max(own, ZERO))}],
            "bars": [{"label": code, "value": c["deposit_due"], "paid": c["paid_confirmed"]}
                     for code, c in sorted(s["customers"].items()) if not c["owner"] and Decimal(c["deposit_due"])]}


def settlement_speech(s: dict) -> str:
    parts = [f"{code} — {eur(c['deposit_due'])}" for code, c in sorted(s["customers"].items()) if not c["owner"]][:5]
    own = Decimal(s["owner_net_deposit_planned"])
    return ("Предоплаты: " + ", ".join(parts) + f". Ваш взнос — {eur(max(own, ZERO))}. Таблица на экране.") if parts else \
        f"Предоплата фабрике {eur(s['factory_deposit_required'])}. Таблица на экране."


def xlsx_preview_html(path: Path, doc_id: int) -> str:
    """A read-only look at a generated workbook: every sheet's values, photos at their rows."""
    import html
    import zipfile
    from app import xlsx_stream
    from app.workbook_inspect import column_letter
    with zipfile.ZipFile(path) as archive:
        parts = xlsx_stream.sheet_parts(archive)
        anchors = {name: xlsx_stream.image_anchors(archive, part) for name, part in parts}
    blocks = []
    for name, _part in parts:
        photos: dict[int, list[str]] = {}
        for a in anchors.get(name, []):
            photos.setdefault(a.row, []).append(a.media)
        rows = list(xlsx_stream.iter_sheet_rows(path, name))
        width = max([max((c for c, _ in cells), default=0) for _, cells in rows] + [0])
        head = "".join(f"<th>{column_letter(i)}</th>" for i in range(1, width + 1))
        body = []
        for number, cells in rows:
            values = dict(cells)
            imgs = "".join(f'<img src="/documents/{doc_id}/image?part={html.escape(m)}" alt="">' for m in photos.pop(number, []))
            body.append(f"<tr><th>{number}</th><td class=ph>{imgs}</td>" + "".join(
                f"<td>{html.escape(str(values.get(i, '')) if values.get(i) is not None else '')}</td>" for i in range(1, width + 1)) + "</tr>")
        blocks.append(f"<h2>{html.escape(name)}</h2><p>Фото в листе: {len(anchors.get(name, []))}</p><div class=wrap><table><thead><tr><th>№</th><th>Фото</th>{head}</tr></thead>"
                      f"<tbody>{''.join(body)}</tbody></table></div>")
    return ("<!doctype html><html lang=ru><head><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
            f"<title>{html.escape(path.name)}</title><style>body{{font:14px -apple-system,system-ui,sans-serif;margin:24px;color:#1c2130;background:#fff}}"
            "h1{font-size:20px}.wrap{overflow:auto;border:1px solid #dadfd6;border-radius:8px}table{border-collapse:collapse;font-size:13px}"
            "th,td{border:1px solid #e4e7e0;padding:4px 8px;vertical-align:top;text-align:left;white-space:nowrap}thead th{background:#f4f5f2;position:sticky;top:0}"
            "td.ph img{max-width:90px;max-height:90px;display:block}.bar{display:flex;gap:12px;align-items:center}</style></head><body>"
            f"<div class=bar><h1>{html.escape(path.name)}</h1><a href='/documents/{doc_id}/download' download>Скачать Excel</a></div>"
            "<p>Просмотр без изменений: так файл откроется в Excel или Numbers (значения и фото).</p>" + "".join(blocks) + "</body></html>")
