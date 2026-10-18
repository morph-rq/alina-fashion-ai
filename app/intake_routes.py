"""Conversation + draft table + evidence workspace (routes and background handlers)."""
from __future__ import annotations

import json
import secrets
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app import ai_intake, drafts, jobs, source_store
from app.config import settings
from app.database import get_db
from app.models import DraftQuestion, DraftRow, IntakeSession, OperationReceipt, Season, SourceItem

MAX_UPLOADS = 10
INLINE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "application/pdf"}


def _analysis_sources(db: Session, session: IntakeSession, source_ids: list[int]) -> list[tuple[str, str, bytes, int]]:
    loaded = []
    for source_id in source_ids:
        source = db.get(SourceItem, source_id)
        if source is None or source.blob is None or source.session_id != session.id:
            continue
        loaded.append((source.original_name or f"source-{source.id}", source.media_type, source_store.read_blob(source.blob), source.id))
    return loaded


@jobs.handler("intake.analyse")
def analyse_job(db: Session, payload: dict) -> dict:
    session = db.get(IntakeSession, payload["session_id"])
    message = payload.get("message", "")
    sources = _analysis_sources(db, session, payload.get("source_ids", []))
    for *_rest, source_id in sources:
        source_store.set_state(db.get(SourceItem, source_id), "analysing")
    try:
        bundle = ai_intake.analyze_submission(message, [(name, mime, content) for name, mime, content, _id in sources])
    except ValueError as exc:
        reason = str(exc)
        for *_rest, source_id in sources:
            item = db.get(SourceItem, source_id)
            item.attempts += 1
            source_store.set_state(item, "failed", reason)
        session.state, session.state_reason = "needs_answers" if session.rows else "collecting", reason
        retries = int(payload.get("retry", 0))
        if "temporarily unavailable" in reason and retries < 3:
            jobs.enqueue(db, "intake.analyse", payload | {"retry": retries + 1}, delay_seconds=60 * (retries + 1))
            reason += " A retry is scheduled automatically."
        drafts.add_message(db, session, "assistant", f"I could not analyse this yet: {reason} Everything you sent is saved; you can also add rows manually.")
        drafts.refresh(db, session)
        return {"failed": reason}
    names = {name: source_id for name, _mime, _content, source_id in sources}
    rows = drafts.merge_extraction(db, session, bundle, names, [source_id for *_rest, source_id in sources])
    drafts.refresh(db, session)
    state = "needs_answers" if session.state == "needs_answers" else "ready_for_review"
    for *_rest, source_id in sources:
        source_store.set_state(db.get(SourceItem, source_id), state)
    summary = f"I found {len(bundle.orders)} entr{'y' if len(bundle.orders) == 1 else 'ies'} and updated {len(rows)} draft row(s)."
    if bundle.source_summary:
        summary += f" {bundle.source_summary}"
    drafts.add_message(db, session, "assistant", summary)
    session.state_reason = None
    return {"rows": [row.id for row in rows]}


@jobs.handler("intake.interpret")
def interpret_job(db: Session, payload: dict) -> dict:
    session = db.get(IntakeSession, payload["session_id"])
    rows = drafts.active_rows(session)
    context = [{"row": row.position, **{name: drafts.row_view(db, session, row)["fields"][name]["text"]
                                        for name in ("customer", "order", "name", "article", "color", "size", "quantity", "price", "price_meaning")}}
               for row in rows if row.kind == "new_item"]
    try:
        interpretation = ai_intake.interpret_follow_up(payload["message"], context)
    except ValueError as exc:
        drafts.add_message(db, session, "assistant", f"I could not interpret that message automatically ({exc}). Edit the table directly or answer the questions.")
        return {"failed": str(exc)}
    by_position = {row.position: row for row in rows}
    field_map = {"customer_name": "customer", "brand": "order", "product_name": "name", "stated_price": "price", "price_type": "price_meaning",
                 "price_currency": "currency"}
    applied = []
    for update in interpretation.field_updates:
        row = by_position.get(update.row)
        if row is None:
            continue
        name = field_map.get(update.field, update.field)
        try:
            drafts.edit_field(db, session, row, name, update.value, origin="operator message (AI-interpreted)")
            applied.append(f"row {row.position} {drafts.FIELD_LABELS.get(name, name).lower()} → {update.value}")
        except drafts.DraftError as exc:
            drafts.add_message(db, session, "assistant", f"Row {row.position}: {exc}")
    if interpretation.new_orders:
        drafts.merge_extraction(db, session, ai_intake.IntakeBundle(orders=interpretation.new_orders), {}, [])
        applied.append(f"{len(interpretation.new_orders)} new row(s)")
    if interpretation.existing_goods_correction and interpretation.existing_goods_correction.field:
        target = interpretation.existing_goods_correction
        notes = drafts.handle_operator_message(db, session, f"change {target.customer_name or ''} {target.product_text or ''} from {target.old_value} to {target.new_value}"
                                               + (" size" if target.field == "size" else ""))
        applied.extend(notes)
    text = ("Applied: " + "; ".join(applied) + ".") if applied else (interpretation.unclear or "I did not find a change to apply.")
    drafts.add_message(db, session, "assistant", text)
    drafts.refresh(db, session)
    return {"applied": applied}


def _run_now(bind, kind: str) -> None:
    # Use the request's database, so tests and alternative workspaces stay isolated.
    jobs.run_pending(sessionmaker(bind=bind, expire_on_commit=False), kinds={kind}, limit=5)


def install_intake(app, templates, context):
    def render(request, db, session, **extra):
        drafts.refresh(db, session)
        db.commit()
        views = [drafts.row_view(db, session, row) for row in session.rows if row.status != "excluded"]
        open_questions = [q for q in session.questions if q.status == "open"]
        receipts = db.scalars(select(OperationReceipt).where(OperationReceipt.session_id == session.id)
                              .order_by(OperationReceipt.created_at.desc())).all()
        return templates.TemplateResponse(request, "intake_session.html", context(
            request, session=session, views=views, questions=open_questions[:3], more_questions=max(len(open_questions) - 3, 0),
            options={q.id: json.loads(q.options_json or "[]") for q in open_questions},
            ready=bool(drafts.active_rows(session)) and not drafts.session_issues(db, session),
            operation_key=secrets.token_urlsafe(24), ai_configured=ai_intake.ai_configured(),
            receipts=[json.loads(r.result_json) | {"key": r.key} for r in receipts], **extra))

    def load(db, public_id):
        try:
            return drafts.get_session(db, public_id)
        except drafts.DraftError:
            raise HTTPException(404, "Intake session not found") from None

    def capture_uploads(db, session, uploads: list[UploadFile]) -> list[int]:
        files = [upload for upload in uploads if upload.filename]
        if len(files) > MAX_UPLOADS:
            raise HTTPException(422, f"Upload at most {MAX_UPLOADS} files at once.")
        ids = []
        for upload in files:
            content = upload.file.read(source_store.MAX_SOURCE_BYTES + 1)
            try:
                result = source_store.capture(db, channel="upload", content=content, original_name=upload.filename,
                                              media_type=upload.content_type, session_id=session.id,
                                              metadata={"uploaded_via": "intake"}, state="queued")
            except source_store.CaptureError as exc:
                db.rollback()
                raise HTTPException(422, str(exc)) from None
            ids.append(result.item.id)
        return ids

    @app.get("/intake")
    def intake_list(request: Request, db: Session = Depends(get_db)):
        sessions = db.scalars(select(IntakeSession).order_by(IntakeSession.updated_at.desc()).limit(100)).all()
        seasons = db.scalars(select(Season).order_by(Season.id.desc())).all()
        return templates.TemplateResponse(request, "intake_list.html", context(request, sessions=sessions, seasons=seasons,
                                                                                ai_configured=ai_intake.ai_configured()))

    @app.post("/intake")
    def intake_create(background: BackgroundTasks, season_id: int = Form(...), title: str = Form(""), message: str = Form(""),
                      attachments: list[UploadFile] = File(default=[]), db: Session = Depends(get_db)):
        season = db.get(Season, season_id)
        if season is None:
            raise HTTPException(422, "Choose an existing season.")
        session = drafts.new_session(db, season, title.strip() or (message.strip()[:60] or "New intake"))
        _post_message(db, session, message, capture_uploads(db, session, attachments), background)
        return RedirectResponse(f"/intake/{session.public_id}", 303)

    def _post_message(db, session, message: str, source_ids: list[int], background: BackgroundTasks):
        text = message.strip()
        if text:
            source_store.capture(db, channel="text", content=text.encode("utf-8"), original_name="message.txt", media_type="text/plain",
                                 session_id=session.id, state="captured")
        if text or source_ids:
            drafts.add_message(db, session, "operator", text or "(attachments)", source_ids)
        notes = []
        needs_ai = bool(source_ids) or (text and not drafts.active_rows(session))
        if text and drafts.active_rows(session) and not source_ids:
            before = session.revision
            notes = drafts.handle_operator_message(db, session, text)
            if session.revision == before and ai_intake.ai_configured() and not notes:
                jobs.enqueue(db, "intake.interpret", {"session_id": session.id, "message": text})
                notes.append("Interpreting your message…")
                background.add_task(_run_now, db.get_bind(), "intake.interpret")
        elif text and not source_ids:
            notes = drafts.handle_operator_message(db, session, text)
            needs_ai = needs_ai and not notes
        if needs_ai:
            if ai_intake.ai_configured():
                jobs.enqueue(db, "intake.analyse", {"session_id": session.id, "message": text, "source_ids": source_ids})
                session.state = "analysing"
                notes.append("Your sources are saved. Analysing…")
                background.add_task(_run_now, db.get_bind(), "intake.analyse")
            else:
                for source_id in source_ids:
                    source_store.set_state(db.get(SourceItem, source_id), "captured", "AI is not configured; enter facts manually.")
                notes.append("Your sources are saved. AI extraction is not configured here, so add the rows with Add row and fill the table from the evidence.")
        for note in notes:
            drafts.add_message(db, session, "assistant", note)
        drafts.touch(session)
        drafts.refresh(db, session)
        db.commit()

    @app.get("/intake/{public_id}")
    def intake_page(public_id: str, request: Request, db: Session = Depends(get_db)):
        return render(request, db, load(db, public_id), error=request.query_params.get("error"))

    @app.post("/intake/{public_id}/messages")
    def intake_message(public_id: str, background: BackgroundTasks, message: str = Form(""),
                       attachments: list[UploadFile] = File(default=[]), db: Session = Depends(get_db)):
        session = load(db, public_id)
        _post_message(db, session, message, capture_uploads(db, session, attachments), background)
        return RedirectResponse(f"/intake/{public_id}#conversation", 303)

    def _back(public_id, error=None, anchor="draft"):
        suffix = f"?error={quote(error)}" if error else ""
        return RedirectResponse(f"/intake/{public_id}{suffix}#{anchor}", 303)

    @app.post("/intake/{public_id}/rows")
    def intake_add_row(public_id: str, kind: str = Form("new_item"), db: Session = Depends(get_db)):
        session = load(db, public_id)
        if kind not in {"new_item", "payment"}:
            raise HTTPException(422, "Unsupported row type.")
        data = {}
        if kind == "payment":
            drafts.set_field(data, "stage", "unallocated", "defaulted", "default:allocate on the Payments page")
            drafts.set_field(data, "payment_type", "payment", "defaulted", "default")
            drafts.set_field(data, "currency", settings.currency, "defaulted", "default")
        drafts.add_row(db, session, kind, data)
        drafts.touch(session)
        drafts.refresh(db, session)
        db.commit()
        return _back(public_id)

    @app.post("/intake/{public_id}/rows/{row_id}/fields")
    async def intake_edit(public_id: str, row_id: int, request: Request, db: Session = Depends(get_db)):
        session = load(db, public_id)
        form = await request.form()
        row = next((row for row in session.rows if row.id == row_id), None)
        if row is None:
            raise HTTPException(404, "Draft row not found")
        expected = int(form["expected_revision"]) if str(form.get("expected_revision", "")).isdigit() else None
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            for name in [key for key in form.keys() if key.startswith("field:")]:
                drafts.edit_field(db, session, row, name.removeprefix("field:"), str(form[name]), expected_revision=expected)
                expected = None  # One revision check per submission.
            drafts.refresh(db, session)
            db.commit()
        except drafts.DraftError as exc:
            db.rollback()
            if wants_json:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=409 if isinstance(exc, drafts.StaleDraftError) else 422)
            return _back(public_id, str(exc))
        if wants_json:
            return JSONResponse({"ok": True, "revision": row.revision, "session_revision": session.revision})
        return _back(public_id)

    @app.post("/intake/{public_id}/rows/{row_id}/exclude")
    def intake_exclude(public_id: str, row_id: int, db: Session = Depends(get_db)):
        session = load(db, public_id)
        row = next((row for row in session.rows if row.id == row_id and row.status == "draft"), None)
        if row is None:
            raise HTTPException(404, "Draft row not found")
        row.status = "excluded"
        drafts.add_message(db, session, "system", f"Row {row.position} removed from the draft (kept in history).")
        drafts.touch(session)
        drafts.refresh(db, session)
        db.commit()
        return _back(public_id)

    @app.post("/intake/{public_id}/questions/{question_id}")
    def intake_answer(public_id: str, question_id: int, choice: str = Form(""), text: str = Form(""), db: Session = Depends(get_db)):
        session = load(db, public_id)
        question = db.get(DraftQuestion, question_id)
        if question is None or question.session_id != session.id:
            raise HTTPException(404, "Question not found")
        try:
            drafts.answer_question(db, session, question, choice=int(choice) if choice.isdigit() else None, text=text)
            drafts.refresh(db, session)
            db.commit()
        except (drafts.DraftError, ValueError) as exc:
            db.rollback()
            return _back(public_id, str(exc), "conversation")
        return _back(public_id, anchor="conversation")

    @app.post("/intake/{public_id}/retry")
    def intake_retry(public_id: str, background: BackgroundTasks, db: Session = Depends(get_db)):
        session = load(db, public_id)
        failed = [source.id for source in session.sources if source.state == "failed" and source.blob_id and source.channel != "text"]
        if not failed:
            return _back(public_id, "Nothing to retry.", "evidence")
        message = next((m.text for m in reversed(session.messages) if m.role == "operator"), "")
        jobs.enqueue(db, "intake.analyse", {"session_id": session.id, "message": message, "source_ids": failed})
        for source_id in failed:
            source_store.set_state(db.get(SourceItem, source_id), "queued", "Retry requested")
        session.state = "analysing"
        db.commit()
        background.add_task(_run_now, db.get_bind(), "intake.analyse")
        return _back(public_id, anchor="evidence")

    @app.post("/intake/{public_id}/commit")
    def intake_commit(request: Request, public_id: str, operation_key: str = Form(...), expected_revision: int = Form(...),
                      then: str = Form(""), db: Session = Depends(get_db)):
        session = load(db, public_id)
        try:
            result = drafts.commit_session(db, session, operation_key=operation_key, expected_revision=expected_revision)
        except drafts.DraftError as exc:
            db.rollback()
            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=409 if isinstance(exc, drafts.StaleDraftError) else 422)
            return _back(public_id, str(exc), "review")
        if "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"ok": True, **result})
        target = f"/intake/{public_id}/receipts/{result['operation_key']}"
        return RedirectResponse(target + ("?download=excel" if then == "excel" else ""), 303)

    @app.get("/intake/{public_id}/receipts/{key}")
    def intake_receipt(public_id: str, key: str, request: Request, db: Session = Depends(get_db)):
        session = load(db, public_id)
        receipt = db.get(OperationReceipt, key)
        if receipt is None or receipt.session_id != session.id:
            raise HTTPException(404, "Receipt not found")
        return templates.TemplateResponse(request, "intake_receipt.html", context(
            request, session=session, receipt=json.loads(receipt.result_json), key=key,
            auto_download=request.query_params.get("download") == "excel"))

    @app.get("/sources/{source_id}/content")
    def source_content(source_id: int, db: Session = Depends(get_db)):
        source = db.get(SourceItem, source_id)
        if source is None or source.blob is None:
            raise HTTPException(404, "Source not found")
        try:
            content = source_store.read_blob(source.blob)
        except (FileNotFoundError, source_store.CaptureError) as exc:
            raise HTTPException(410, f"Stored evidence is unavailable: {exc}") from None
        inline = source.media_type in INLINE_TYPES
        media_type = source.media_type if inline else ("text/plain; charset=utf-8" if source.media_type.startswith("text/") else "application/octet-stream")
        from urllib.parse import quote
        original = (source.original_name or f"source-{source.id}")[:150]
        ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._- ") else "_" for ch in original) or f"source-{source.id}"
        disposition = "inline" if inline or media_type.startswith("text/") else "attachment"
        return Response(content, media_type=media_type, headers={
            "Content-Disposition": f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(original, safe='')}",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'",
            "Cache-Control": "private, no-store"})
