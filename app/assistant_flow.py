"""Conversation flow shared by typed and spoken requests: selection context, follow-ups, confirmation, undo.

``intercept`` runs before any language model: a spoken or typed "да, сохрани" can only confirm the
newest pending proposal *of this conversation*, in the version that was shown (the preview is
recomputed; if the numbers moved, the new figures are shown and confirmation is asked again).
Payments, credits, owner changes, factory terms and moves always need the button.

``FlowMixin`` gives the offline ``RuleInterpreter`` the business conversation:
"Открой Dior Cruise" → "Покажи IDS" (keeps the brand) → "Поставь ей 35%" → "А сколько теперь
предоплата?" → "Сделай PDF" → "И Excel тоже" → "Теперь это для AA, но с 30%". Money in replies
always comes from the settlement engine or the document snapshot.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ledger_actions as la
from app import selection as sel
from app.calculations import ZERO, money
from app.models import AgentProposal, Customer, GeneratedDocument, OrderItem

VOICE_SAFE = {"set_customer_markup", "set_item_markup", "set_status", "update_item", "restore_state"}
CONFIRM = re.compile(r"^\s*(да|ага|угу|ок|окей|okay|ok|yes|хорошо|сохрани\w*|подтвер\w*|верно|давай|применя\w*|делай|согласн\w*)\b", re.I)
REJECT = re.compile(r"^\s*(нет|не надо|не нужно|отмена|отменяй|стоп|no)\b", re.I)


def eur(value) -> str:
    return f"€{Decimal(value):,.2f}".replace(",", " ")


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else many


def proposal_fingerprint(preview: la.Preview) -> str:
    return json.dumps({"m": preview.messages, "d": [{k: str(v) for k, v in r.items()} for r in preview.diff]},
                      sort_keys=True, ensure_ascii=False)


def latest_pending(db: Session, conversation: str) -> AgentProposal | None:
    return db.scalar(select(AgentProposal).where(AgentProposal.conversation == conversation, AgentProposal.status == "pending")
                     .order_by(AgentProposal.id.desc()))


class FlowMixin:
    """Mixed into ``agent.RuleInterpreter`` (needs .db, .conversation, .screen, .memory, .tool, .done, .pending)."""

    # --- context ------------------------------------------------------------------------
    def previous_filters(self) -> sel.Filters | None:
        data = self.memory.get("selection")
        return sel.Filters.from_dict(data) if data else None

    def context_from(self, selection: sel.Selection, extra: dict | None = None) -> dict:
        context = {"season": selection.season.name if selection.season else self.memory.get("season"),
                   "selection": selection.filters.to_dict(), "last_docs": self.memory.get("last_docs"),
                   "last_op": self.memory.get("last_op"), "last_markup": self.memory.get("last_markup")}
        if len(selection.filters.brands) == 1:
            context["brand"] = selection.filters.brands[0]
        if len(selection.filters.customers) == 1:
            context["customer"] = selection.filters.customers[0]
        return context | (extra or {})

    def current_selection(self, text: str) -> tuple[sel.Selection | None, dict | None]:
        filters, question, _ = sel.parse(self.db, text, self.previous_filters(), self.screen)
        if question:
            return None, question
        return sel.resolve(self.db, filters, self.screen), None

    # --- the flow ---------------------------------------------------------------------
    def flow(self, text: str):
        lowered = text.casefold()
        if re.search(r"не совпада|проверь (файл|сумм|данн|заказ)|найди ошибк|дубл|почему.*сумм|расхожд", lowered):
            return self.check_data(text)
        if re.search(r"почему.*(зел|жёлт|желт|красн|цвет|выделен)", lowered):
            return self.explain(text, colour=True)
        if re.search(r"сравни.*фото|фото.*(похож|совпада|одинаков)", lowered):
            return self.compare_photos(text)
        if re.search(r"сколько (это |эта |этот |эти |он |она )?(стоит|стоят)|почём|какая (у н\w+ )?цена|(какой|какого|какая) у (него|неё|нее|этого|этой)|откуда (эта|это|сумма|цифра)|что за (вещь|позиция)|расскажи про (эт|позиц)", lowered):
            return self.explain(text)
        if re.search(r"\b(убери|удали|отмени|вычеркни)\b", lowered) and not re.search(r"последн|изменени", lowered):
            return self.cancel_items(text)
        if re.search(r"(отметь|пометь|поставь статус|сделай)\s.*готов", lowered) and not re.search(r"excel|pdf|эксель|пдф|документ|таблиц", lowered):
            return self.mark_ready(text)
        percents = re.findall(r"(\d{1,2}(?:[.,]\d+)?)\s?%", text)
        deposit_rate = re.search(r"\d\s?%\s*(предоплат|депозит)|(предоплат|депозит)\w*\s*(в\s*)?\d+\s?%|^\s*сколько", lowered)
        if percents and "фабрик" not in lowered and not deposit_rate:
            if self.percent_pairs(text):
                return None         # "IDS 35%, AA 30% (+ Excel/PDF)" — the customer-rule path below handles it
            return self.markup_for_selection(text, percents)
        if re.search(r"excel|эксел|эксель|xlsx|pdf|пдф|документ|выгруз|экспорт|zip|архив|таблиц\w* как|оригинал", lowered):
            return self.documents(text)
        if re.search(r"\bто же( самое)?\b|тоже самое|\bтак же\b|такой же|такие же|то же для", lowered) and not percents:
            last = self.memory.get("last_op")
            if last == "markup" and self.memory.get("last_markup"):
                return self.markup_for_selection(text, [self.memory["last_markup"]])
            if self.memory.get("last_docs"):
                return self.documents(text + " тоже")
        if re.search(r"сколько", lowered) and re.search(r"предоплат|внести|должн|заплатить", lowered) \
                and not re.search(r"фабрик|кажд", lowered):
            return self.deposit_answer(text)
        if re.search(r"^\s*(покажи|открой|выбери|выдели|найди|отфильтруй|оставь только|какие|список|перейди|давай)\b", lowered) \
                or self._only_scope_words(text):
            return self.show_selection(text)
        return None

    def _only_scope_words(self, text: str) -> bool:
        filters, question, changed = sel.parse(self.db, text, None, self.screen)
        return bool(changed & {"brands", "customers"}) and len(re.findall(r"\w+", text)) <= 5 and not re.search(r"\d\s?%", text)

    # --- selection ------------------------------------------------------------------------
    def show_selection(self, text: str):
        selection, question = self.current_selection(text)
        if question:
            return self.done(question["question"], question, self.memory)
        table = sel.selection_table(selection)
        context = self.context_from(selection)
        if not selection.items:
            reply = selection.summary + ". Ничего не найдено — проверьте бренд, клиента или фильтр."
            return self.done(reply, None, context, panel={"selection": table}, speech="Ничего не нашла по этому запросу.")
        customers = ", ".join(selection.customer_codes) or "без клиента"
        reply = (f"{selection.summary}.\nКлиенты: {customers}. Сумма по ценам клиентов {eur(table['customer_total'])}"
                 f" (база {eur(table['base_total'])}).")
        count = len(selection.items)
        speech = f"Нашла {count} {_plural(count, 'позицию', 'позиции', 'позиций')}. Показала на экране."
        return self.done(reply, None, context, panel={"selection": table}, speech=speech)

    # --- markups for the current selection --------------------------------------------------
    def markup_for_selection(self, text: str, percents: list[str]):
        selection, question = self.current_selection(text)
        if question:
            return self.done(question["question"], question, self.memory)
        customers = [c for c in (self.db.scalar(select(Customer).where(Customer.code == code)) for code in selection.filters.customers) if c]
        if not customers:
            options = [c for c in selection.customer_codes if not self._is_owner(c)]
            return self.done(f"Кому поставить {percents[0]}%?", {"question": f"Кому поставить {percents[0]}%?", "options": options[:8]},
                             self.context_from(selection))
        if len(customers) > 1 and len(percents) == 1:
            return self.done(f"{percents[0]}% — для всех: {', '.join(c.code for c in customers)}? Или назовите одного клиента.",
                             {"question": f"{percents[0]}% кому?", "options": [c.code for c in customers] + ["Всем"]}, self.context_from(selection))
        value = percents[0].replace(",", ".")
        actions, notes = [], []
        narrowed = bool({"readiness", "categories", "sizes", "colors", "item_ids", "rows", "article", "price_min", "price_max"}
                        & set(selection.filters.to_dict()))
        for customer in customers:
            if customer.is_owner:
                notes.append(f"{customer.code} отмечен(а) как владелец: её позиции всегда по цене фабрики, {value}% не применяю.")
                continue
            if narrowed:
                for item in selection.items:
                    if item.customer_id == customer.id:
                        actions.append({"action": "set_item_markup", "params": {"item_id": item.id, "markup": value}})
            else:
                params = {"customer": customer.code, "markup": value, "season": selection.season.name}
                if len(selection.filters.brands) == 1:
                    params["brand"] = selection.filters.brands[0]
                actions.append({"action": "set_customer_markup", "params": params})
        if not actions:
            return self.done("\n".join(notes) or "Нечего менять.", None, self.context_from(selection))
        scope = " · ".join(selection.filters.brands) or selection.season.name
        out = self.tool("propose_changes", {"summary": f"Наценка {value}%: {', '.join(c.code for c in customers if not c.is_owner)} ({scope})",
                                            "actions": actions})
        result = self._proposal_reply(out, self.context_from(selection, {"last_op": "markup", "last_markup": value}))
        if notes:
            result.reply = "\n".join(notes) + "\n\n" + result.reply
        return result

    def _is_owner(self, code: str) -> bool:
        customer = self.db.scalar(select(Customer).where(Customer.code == code))
        return bool(customer and customer.is_owner)

    # --- deposit / amounts --------------------------------------------------------------------
    def deposit_answer(self, text: str):
        from app.customer_documents import customer_snapshot
        selection, question = self.current_selection(text)
        if question:
            return self.done(question["question"], question, self.memory)
        context = self.context_from(selection)
        codes = selection.filters.customers
        if not codes:
            return None       # the season-wide answer ("Сколько каждый должен внести?") stays with the base interpreter
        lines, speech_parts, figures = [], [], []
        pending = latest_pending(self.db, self.conversation)
        pending_diff = json.loads(pending.preview_json or "{}").get("diff", []) if pending else []
        for code in codes:
            customer = la.find_customer(self.db, code)
            scope = selection.filters.brands or None
            if customer.is_owner:
                s = self.tool("calculate_factory_settlement", {"season": selection.season.name})
                own = Decimal(s["owner_net_deposit_planned"])
                lines.append(f"{code} (вы): ваш взнос в предоплату фабрике {eur(max(own, ZERO))}")
                speech_parts.append(f"Ваш взнос в предоплату — {eur(max(own, ZERO))}")
                figures.append({"label": f"{code}: ваш взнос", "value": str(max(own, ZERO))})
                continue
            try:
                snap = customer_snapshot(self.db, customer, selection.season, scope)
            except ValueError as exc:
                lines.append(str(exc))
                continue
            where = " · ".join(snap["brands"])
            lines.append(f"{code} · {where}: заказ {eur(snap['total'])}, предоплата {eur(snap['deposit'])}, "
                         f"оплачено {eur(snap['paid'])}, осталось внести {eur(snap['deposit_outstanding'])}")
            figures += [{"label": f"{code}: заказ", "value": snap["total"]}, {"label": f"{code}: предоплата", "value": snap["deposit"]},
                        {"label": f"{code}: оплачено", "value": snap["paid"]}, {"label": f"{code}: осталось внести", "value": snap["deposit_outstanding"]}]
            phrase = f"По заказу {where} {code} должна внести предоплату в размере {eur(snap['deposit'])}"
            if Decimal(snap["paid"]) > 0:
                phrase += f", оплачено {eur(snap['paid'])}, осталось {eur(snap['deposit_outstanding'])}"
            speech_parts.append(phrase)
            future = next((r for r in pending_diff if r.get("label") == f"{code}: предоплата"), None)
            if future:
                lines.append(f"После сохранения предложенных изменений (по сезону): {eur(future['before'])} → {eur(future['after'])}")
                speech_parts.append("после сохранения изменений она станет " + eur(future["after"]) + " по сезону")
        speech = ". ".join(speech_parts) + ". Я также показала подробный расчёт на экране." if speech_parts else None
        return self.done("\n".join(lines), None, context, panel={"figures": figures}, speech=speech)

    # --- documents --------------------------------------------------------------------------
    def documents(self, text: str):
        from app import selective_export as sx
        lowered = text.casefold()
        last = self.memory.get("last_docs") or {}
        wants_xlsx = bool(re.search(r"excel|эксел|эксель|xlsx|таблиц", lowered))
        wants_pdf = bool(re.search(r"pdf|пдф", lowered))
        formats = tuple(f for f, on in (("xlsx", wants_xlsx), ("pdf", wants_pdf)) if on) or \
            (("xlsx", "pdf") if re.search(r"документ|файл|оба|два", lowered) or not last else tuple(last.get("formats") or ("xlsx", "pdf")))
        if re.search(r"оба|два файла|excel и pdf|pdf и excel", lowered):
            formats = ("xlsx", "pdf")
        audiences = []
        if re.search(r"оригинал|точно такую же|как в файле фабрики|как у фабрики", lowered):
            audiences.append("original")
        if re.search(r"фабрик|внутрен|для себя|для меня|общий|сводн", lowered):
            audiences.append("internal")
        if re.search(r"клиент|кажд|отдельн", lowered) or not audiences:
            audiences.append("customer")
        follow_up = bool(re.search(r"\bтоже\b|\bтакже\b|такой же|такую же|то же самое", lowered)) and last
        previous = self.previous_filters()
        if follow_up and last.get("filters"):
            previous = sel.Filters.from_dict(last["filters"])
            if not re.search(r"оригинал|фабрик|внутрен", lowered):
                audiences = list(last.get("audiences") or audiences)
        filters, question, changed = sel.parse(self.db, text, previous, self.screen)
        if question:
            return self.done(question["question"], question, self.memory)
        selection = sel.resolve(self.db, filters, self.screen)
        if not selection.items:
            return self.done(selection.summary + ". Нет позиций для документа.", None, self.context_from(selection))
        made, lines, notes = [], [], []
        stale = latest_pending(self.db, self.conversation)
        if stale:
            notes.append("Предложенные изменения ещё не сохранены — документы сделаны по сохранённым данным.")
        try:
            if "customer" in audiences:
                specs = sx.plan(self.db, selection)
                owners = [c for c in selection.customer_codes if self._is_owner(c)]
                if owners:
                    notes.append(f"{', '.join(owners)} — это вы: для вас документ клиента не делаю (есть внутренний отчёт).")
                for spec in specs:
                    docs = sx.make_customer_documents(self.db, spec, formats, "assistant")
                    made += docs
                    snap = json.loads(docs[0].snapshot_json)
                    count = spec["items"]
                    lines.append(f"{spec['customer']}: {count} {_plural(count, 'позиция', 'позиции', 'позиций')}, "
                                 f"{'по выборке ' if snap.get('subset') else ''}сумма {eur(snap['total'])}, предоплата {eur(snap['deposit'])}"
                                 + (f" · {snap['label']}" if snap.get("label") else ""))
            if "internal" in audiences:
                docs = sx.make_internal(self.db, selection, formats, "assistant")
                made += docs
                lines.append(f"Внутренний отчёт: {len(selection.items)} поз. (с клиентами и суммами фабрики) — не для клиентов.")
            if "original" in audiences:
                docs = sx.original_with_prices(self.db, selection, "assistant")
                made += docs
                lines.append("Оригинал фабрики с добавленными колонками «Клиент · цена / сумма» — всё остальное как в исходном файле.")
        except ValueError as exc:
            return self.done(str(exc), None, self.context_from(selection))
        if not made:
            return self.done("Документы не получились: в выборке нет клиентов (кроме вас).", None, self.context_from(selection))
        files = [{"id": d.id, "format": d.file_format, "kind": d.kind, "customer": d.customer.code if d.customer else None,
                  "version": d.version, "url": f"/documents/{d.id}/download", "name": d.stored_path.rsplit("/", 1)[-1]} for d in made]
        bundle = f"/documents/bundle?ids={','.join(str(d.id) for d in made)}" if len(made) > 1 else None
        context = self.context_from(selection, {"last_docs": {"formats": list(formats), "audiences": audiences,
                                                              "filters": selection.filters.to_dict()}, "last_op": "documents"})
        kinds = " и ".join({"xlsx": "Excel", "pdf": "PDF"}[f] for f in formats)
        reply = f"{selection.summary}.\nГотово ({kinds}):\n" + "\n".join(f"• {l}" for l in lines)
        if "customer" in audiences:
            reply += "\nКаждый файл проверен: других клиентов, процентов фабрики и вашей маржи в нём нет."
        if notes:
            reply += "\n" + "\n".join(notes)
        reply += "\nФайлы — предпросмотр: клиентам ничего не отправлено."
        count = len(made)
        speech = f"Готово: {count} {_plural(count, 'файл', 'файла', 'файлов')}, {kinds}. Ссылки на экране."
        return self.done(reply, None, context, panel={"documents": files, "bundle": bundle, "selection": sel.selection_table(selection)},
                         speech=speech)

    # --- item edits by voice -----------------------------------------------------------------
    def _targets(self, text: str) -> tuple[sel.Selection | None, dict | None]:
        selection, question = self.current_selection(text)
        if question or selection is None:
            return None, question
        return selection, None

    def cancel_items(self, text: str):
        selection, question = self._targets(text)
        if question:
            return self.done(question["question"], question, self.memory)
        items = selection.items
        if not items:
            return self.done(selection.summary + ". Не нашла такую позицию.", None, self.context_from(selection))
        if len(items) > 1 and not re.search(r"\bвсе\b|все эти|выделенн", text.casefold()):
            options = [f"#{i.id} {i.name or i.article} {i.size or ''} {i.customer.code if i.customer else ''}".strip() for i in items[:6]]
            return self.done(f"Подходит {len(items)} позиций. Какую убрать?", {"question": "Какую позицию убрать?", "options": options},
                             self.context_from(selection, {"choose": [i.id for i in items[:6]]}))
        out = self.tool("propose_changes", {"summary": f"Убрать {len(items)} поз.: " + ", ".join(f"{i.name or i.article} ({i.customer.code if i.customer else '—'})" for i in items[:5]),
                                            "actions": [{"action": "set_status", "params": {"item_ids": [i.id for i in items], "status": "Cancelled",
                                                                                            "reason": f"По просьбе: «{text[:200]}»"}}]})
        return self._proposal_reply(out, self.context_from(selection))

    def mark_ready(self, text: str):
        selection, question = self._targets(text.replace("готов", ""))
        if question:
            return self.done(question["question"], question, self.memory)
        if not selection.items:
            return self.done(selection.summary + ". Нет таких позиций.", None, self.context_from(selection))
        out = self.tool("propose_changes", {"summary": f"Отметить готовыми: {len(selection.items)} поз.",
                                            "actions": [{"action": "set_status", "params": {"item_ids": [i.id for i in selection.items], "status": "Ready"}}]})
        return self._proposal_reply(out, self.context_from(selection))

    # --- explain / inspect --------------------------------------------------------------------
    def _one_item(self, text: str) -> tuple[OrderItem | None, sel.Selection | None]:
        ids = self.screen.get("selected_item_ids") or []
        if len(ids) == 1:
            return self.db.get(OrderItem, int(ids[0])), None
        choose = self.memory.get("choose")
        number = re.search(r"#?(\d{1,6})\b", text)
        if number and choose and int(number.group(1)) in choose:
            return self.db.get(OrderItem, int(number.group(1))), None
        selection, _ = self.current_selection(text)
        if selection and len(selection.items) == 1:
            return selection.items[0], selection
        return None, selection

    def explain(self, text: str, colour: bool = False):
        item, selection = self._one_item(text)
        if item is None:
            count = len(selection.items) if selection else 0
            return self.done(f"Выделите одну строку (сейчас подходит {count}) или назовите позицию точнее.", None, self.memory)
        from app.calculations import item_amounts
        a = item_amounts(item)
        facts = [f"{item.name or '—'} · {item.article or ''} · цвет {item.color or '—'} · размер {item.size or '—'} · {item.quantity} шт.",
                 f"Клиент: {item.customer.code if item.customer else 'не распределено'} · статус: {la.STATUS_RU.get(item.status, item.status)}",
                 f"База {eur(a.base)} → цена клиента {eur(a.final)} (наценка {(Decimal(item.markup_percentage or 0) * 100).normalize():f}%"
                 + (", ручная цена" if item.selling_unit_price is not None else "") + ")",
                 f"Источник: {item.source_file or '—'}, лист «{item.source_sheet or '—'}», строка {item.source_row or '—'}"
                 + (f", в файле: «{item.source_allocation_description}»" if item.source_allocation_description else "")]
        speech = f"{item.name or 'Позиция'}, размер {item.size or 'не указан'}, клиент {item.customer.code if item.customer else 'не назначен'}."
        if colour:
            meaning = fill_meaning(item.source_fill)
            facts.append(f"Цвет строки в файле фабрики: {item.source_fill or 'без заливки'}"
                         + (f" — {meaning} (так поставщик обычно размечает строки; это толкование, а не запись в базе)." if meaning else "."))
            facts.append(f"Подтверждённый статус в приложении: {la.STATUS_RU.get(item.status, item.status)}.")
            speech = (f"В файле фабрики строка отмечена цветом, {meaning}" if meaning else "У строки нет цветовой отметки") + \
                     f". В приложении статус: {la.STATUS_RU.get(item.status, item.status)}."
        panel = {"item": {"id": item.id, "image": item.image_path, "name": item.name, "facts": facts}}
        return self.done("\n".join(facts), None, self.memory | {"item": item.id}, panel=panel, speech=speech)

    def compare_photos(self, text: str):
        ids = [int(i) for i in self.screen.get("selected_item_ids") or []][:2]
        if len(ids) < 2:
            selection, _ = self.current_selection(text)
            ids = [i.id for i in (selection.items if selection else [])][:2]
        if len(ids) < 2:
            return self.done("Выделите две позиции, фото которых сравнить.", None, self.memory)
        first, second = (self.db.get(OrderItem, i) for i in ids)
        score = photo_similarity(first.image_path, second.image_path)
        if score is None:
            return self.done("У одной из позиций нет фото.", None, self.memory)
        verdict = "очень похожи — возможно, это одно изделие" if score >= 0.9 else "похожи" if score >= 0.75 else "разные"
        reply = (f"Фото #{first.id} «{first.name}» и #{second.id} «{second.name}»: сходство {score:.0%} — {verdict}.\n"
                 "Это автоматическое сравнение изображений (оценка, не факт). Артикулы: "
                 f"{first.article or '—'} / {second.article or '—'}" + (" — совпадают." if first.article and first.article == second.article else "."))
        return self.done(reply, None, self.memory, panel={"photos": [{"id": first.id, "image": first.image_path, "name": first.name},
                                                                      {"id": second.id, "image": second.image_path, "name": second.name}]},
                         speech=f"Фото {verdict}, сходство {round(score * 100)} процентов.")

    def check_data(self, text: str):
        selection, question = self.current_selection(text)
        if question:
            return self.done(question["question"], question, self.memory)
        season = selection.season
        if season is None:
            return self.done("Нет сезона для проверки.", None, self.memory)
        findings = audit_findings(self.db, season, selection.filters.brands or None)
        if not findings:
            reply = f"{season.name}: суммы совпадают с файлами фабрик, дублей и нераспределённых строк нет."
            return self.done(reply, None, self.context_from(selection), speech="Проверила: расхождений не нашла.")
        reply = f"Проверка {season.name}" + (f" · {', '.join(selection.filters.brands)}" if selection.filters.brands else "") + ":\n" + \
            "\n".join(f"• {f}" for f in findings[:25])
        count = len(findings)
        return self.done(reply, None, self.context_from(selection), panel={"findings": findings[:60]},
                         speech=f"Нашла {count} {_plural(count, 'замечание', 'замечания', 'замечаний')}. Первое: {findings[0]}")


# --- Confirmation / correction / undo (also used with the language model) ------------------

def intercept(db: Session, conversation: str, text: str, screen: dict, channel: str, memory: dict):
    """Returns a TurnResult for "да / нет / покажи расчёт / отмени последнее изменение", or None."""
    from app.agent import TurnResult, commit_proposal, proposal_view
    lowered = text.casefold().strip()
    pending = latest_pending(db, conversation)
    if re.search(r"отмени (последн|это изменени|что сделал)|верни как было|откати", lowered):
        return undo_last(db, conversation, memory)
    if pending is None:
        return None
    shown = (screen or {}).get("proposal") or {}
    if re.search(r"(сначала|сперва)?\s*покажи (расч[её]т|изменени|что поменя)|какой расч[её]т", lowered):
        view = proposal_view(pending)
        lines = [f"{r['label']}: {eur(r['before'])} → {eur(r['after'])}" for r in view["diff"]] or ["Суммы не изменятся."]
        return TurnResult("Расчёт по предложению «" + pending.summary + "»:\n" + "\n".join(lines) + "\n\nСохранить?",
                          proposal=view, data=memory, speech="Показала расчёт на экране. Сохранить?")
    if CONFIRM.search(lowered) and not REJECT.search(lowered) and len(lowered) <= 60:
        if shown.get("id") and shown["id"] != pending.public_id:
            return TurnResult("Подтверждение относится к другому предложению. Подтвердите нужное кнопкой.", data=memory,
                              speech="Это подтверждение не к последнему предложению. Нажмите кнопку на нужной карточке.")
        if shown.get("version") is not None and int(shown["version"]) != pending.version:
            return TurnResult("Предложение обновилось после того, как вы его видели. Посмотрите новые цифры.", proposal=proposal_view(pending), data=memory)
        plan = json.loads(pending.actions_json or "{}")
        actions = plan.get("actions", [])
        if channel == "voice" and (plan.get("import") or any(a.get("action") not in VOICE_SAFE for a in actions)):
            return TurnResult("Такие изменения (оплаты, условия фабрики, перенос позиций) голосом не сохраняю — нажмите кнопку «Подтвердить» на карточке.",
                              proposal=proposal_view(pending), data=memory,
                              speech="Это изменение я сохраняю только по кнопке. Нажмите «Подтвердить» на карточке.")
        if actions and not plan.get("import"):        # imports re-check their own comparison on commit
            fresh = la.preview_actions(db, actions)
            stored = json.loads(pending.preview_json or "{}")
            if not fresh.ok or proposal_fingerprint(fresh) != json.dumps(
                    {"m": stored.get("messages", []), "d": [{k: str(v) for k, v in r.items()} for r in stored.get("diff", [])]},
                    sort_keys=True, ensure_ascii=False):
                if not fresh.ok:
                    pending.status, pending.decided_at = "failed", datetime.now(timezone.utc)
                    db.commit()
                    return TurnResult(f"Сохранить нельзя: {fresh.error}", data=memory)
                pending.preview_json = json.dumps({"messages": fresh.messages, "diff": fresh.diff}, ensure_ascii=False, default=str)
                pending.version += 1
                db.commit()
                return TurnResult("Данные изменились, пока предложение ждало. Вот новый расчёт — подтвердите ещё раз.",
                                  proposal=proposal_view(pending), data=memory, speech="Цифры изменились. Проверьте новый расчёт и подтвердите ещё раз.")
        try:
            result = commit_proposal(db, pending.public_id, actor="assistant (голос)" if channel == "voice" else "assistant")
        except (ValueError, la.ActionError) as exc:
            return TurnResult(f"Не сохранено: {exc}", proposal=proposal_view(pending), data=memory)
        view = proposal_view(pending)
        lines = result.get("messages", [])
        reply = "Сохранено.\n" + "\n".join(f"• {l}" for l in lines)
        client = {"navigate": result["navigate"]} if result.get("navigate") else None
        if result.get("follow_up"):
            client = (client or {}) | {"follow_up": result["follow_up"]}
        return TurnResult(reply, proposal=view, data=memory | {"last_committed": pending.public_id}, client=client,
                          speech="Сохранила." + (" Можно отменить фразой «отмени последнее изменение»." if json.loads(pending.result_json or "{}").get("undo") else ""))
    if REJECT.search(lowered):
        rest = REJECT.sub("", text, count=1).strip(" ,.!—-")
        pending.status, pending.decided_at = "cancelled", datetime.now(timezone.utc)
        db.commit()
        if rest and re.search(r"\d", rest):
            corrected = correct_markups(db, conversation, pending, rest, memory)
            if corrected is not None:
                return corrected
        if rest:
            return None if not re.search(r"\w{3,}", rest) else _redo(rest)
        return TurnResult("Хорошо, не сохраняю. Ничего не изменено.", proposal=proposal_view(pending), data=memory, speech="Хорошо, не сохраняю.")
    return None


def _redo(rest: str):
    """Signal to the caller: the proposal was cancelled, continue with the remaining words as a new request."""
    return {"continue_with": rest}


def correct_markups(db: Session, conversation: str, cancelled: AgentProposal, rest: str, memory: dict):
    """ "Нет, у AA оставь 25" → same proposal with AA's markup changed; the others stay as proposed."""
    from app.agent import TurnResult, _Pending, _store_proposal, run_tool
    plan = json.loads(cancelled.actions_json or "{}")
    actions = [a for a in plan.get("actions", []) if a.get("action") == "set_customer_markup"]
    if not actions:
        return None
    found = sel.customers_in(db, rest)
    number = re.search(r"(\d{1,2}(?:[.,]\d+)?)\s?%?", rest)
    if not isinstance(found, list) or len(found) != 1 or not number:
        return None
    code, value = found[0].code, number.group(1).replace(",", ".")
    changed = False
    for action in actions:
        if action["params"].get("customer", "").upper() == code.upper():
            action["params"]["markup"] = value
            changed = True
    if not changed:
        template = dict(actions[0]["params"])
        template.update(customer=code, markup=value)
        actions.append({"action": "set_customer_markup", "params": template})
    pending = _Pending()
    out = run_tool(db, "propose_changes", {"summary": "Наценки: " + ", ".join(f"{a['params']['customer']} {a['params']['markup']}%" for a in actions),
                                           "actions": actions}, pending)
    if "error" in out:
        return TurnResult(out["error"], data=memory)
    proposal = _store_proposal(db, rest, pending, conversation)
    lines = [f"• {l}" for l in out.get("will_do", [])]
    impact = [f"{r['label']}: {eur(r['before'])} → {eur(r['after'])}" for r in out.get("financial_impact", [])]
    reply = "Исправила предложение:\n" + "\n".join(lines) + ("\n\nВлияние на суммы:\n" + "\n".join(impact) if impact else "") + "\n\nСохранить?"
    return TurnResult(reply, proposal=proposal, data=memory, speech="Исправила. " + "; ".join(
        f"{a['params']['customer']} {a['params']['markup']}%" for a in actions) + ". Сохранить?")


def undo_last(db: Session, conversation: str, memory: dict):
    from app.agent import TurnResult, _Pending, _store_proposal, run_tool
    done = list(db.scalars(select(AgentProposal).where(AgentProposal.status == "committed")
                           .order_by(AgentProposal.decided_at.desc(), AgentProposal.id.desc()).limit(10)))
    target = next((p for p in done if p.conversation == conversation), done[0] if done else None)
    if target is None:
        return TurnResult("Нечего отменять: сохранённых изменений от ассистента нет.", data=memory)
    undo = json.loads(target.result_json or "{}").get("undo")
    if not undo:
        return TurnResult(f"Изменение «{target.summary}» нельзя отменить автоматически (оплаты и переносы отменяются вручную на странице «Фабрики» или в таблице).",
                          data=memory)
    pending = _Pending()
    out = run_tool(db, "propose_changes", {"summary": f"Отменить: {target.summary}", "actions": [{"action": "restore_state", "params": undo}]}, pending)
    if "error" in out:
        return TurnResult(out["error"], data=memory)
    proposal = _store_proposal(db, "отмена", pending, conversation)
    impact = [f"{r['label']}: {eur(r['before'])} → {eur(r['after'])}" for r in out.get("financial_impact", [])]
    return TurnResult(f"Верну как было до «{target.summary}»:\n" + ("\n".join(impact) or "Суммы не изменятся.") + "\n\nСохранить?",
                      proposal=proposal, data=memory, speech="Подготовила отмену. Сохранить?")


# --- Data checks ----------------------------------------------------------------------------

def fill_meaning(colour: str | None) -> str | None:
    if not colour or not re.fullmatch(r"#?[0-9A-Fa-f]{6}", colour.strip()):
        return None
    value = colour.strip().lstrip("#")
    r, g, b = (int(value[i:i + 2], 16) for i in (0, 2, 4))
    if g > r + 20 and g > b + 10:
        return "зелёный: обычно «готово / подтверждено»"
    if r > 200 and g > 180 and b < 150:
        return "жёлтый: обычно «в работе / уточнить»"
    if r > g + 40 and r > b + 40:
        return "красный: обычно «проблема / отмена»"
    if b > r + 20 and b > g:
        return "синий: отметка поставщика"
    if abs(r - g) < 12 and abs(g - b) < 12 and r < 230:
        return "серый: часто «снято / неактуально» — уточните у поставщика"
    return None


def audit_findings(db: Session, season, brands: list[str] | None = None) -> list[str]:
    from app.factory_ledger import season_ledger
    findings: list[str] = []
    ledger = season_ledger(db, season)
    for view in ledger.orders:
        order = view.order
        if brands and order.brand.name not in brands:
            continue
        if view.source_difference not in (None, ZERO) and view.source_total is not None:
            findings.append(f"{order.brand.name}: в файле фабрики итог {eur(view.source_total)}, по строкам приложения {eur(view.factory_total)} "
                            f"(разница {eur(view.source_difference)}) — проверьте строки без суммы, отменённые и формулы итога.")
        for warning in view.warnings:
            findings.append(f"{order.brand.name}: {warning}")
        if view.unallocated:
            findings.append(f"{order.brand.name}: {view.unallocated} поз. без клиента — откройте «Уточнить».")
        seen: dict[tuple, OrderItem] = {}
        for item in order.items:
            if item.status == "Cancelled":
                continue
            where = f"лист «{item.source_sheet}», строка {item.source_row}" if item.source_row else f"#{item.id}"
            if item.source_total is not None and item.wholesale_unit_price is not None:
                expected = money(Decimal(item.wholesale_unit_price) * item.quantity)
                group_total = money(sum((Decimal(i.wholesale_unit_price or 0) * i.quantity for i in order.items
                                         if i.allocation_group_id and i.allocation_group_id == item.allocation_group_id), ZERO))
                if money(item.source_total) not in (expected, group_total):
                    findings.append(f"{order.brand.name}, {where}: в файле сумма строки {eur(item.source_total)}, а цена × кол-во = {eur(group_total or expected)}.")
            key = (item.article, item.size, item.customer_id)
            if item.article and key in seen and seen[key].allocation_group_id != item.allocation_group_id:
                other = seen[key]
                findings.append(f"{order.brand.name}: возможный дубль — {item.article} размер {item.size} у "
                                f"{item.customer.code if item.customer else '—'} в строках {other.source_row} и {item.source_row}.")
            seen.setdefault(key, item)
            text = item.source_allocation_description or ""
            sizes = set(re.findall(r"\b(3\d|4\d|5\d)(?:[.,]5)?\b", text))
            if item.size and sizes and item.customer and re.sub(r"[.,]5$", "", item.size) not in sizes:
                findings.append(f"{order.brand.name}, {where}: размер {item.size} не встречается в распределении «{text}».")
    return list(dict.fromkeys(findings))


def photo_similarity(first: str | None, second: str | None) -> float | None:
    """Average-hash similarity of two product photos (0..1). An estimate, labelled as such to the user."""
    from app.customer_documents import _image_file
    from app.config import ROOT_DIR
    from PIL import Image
    hashes = []
    for path in (first, second):
        file = _image_file(path, ROOT_DIR) if path else None
        if file is None:
            return None
        with Image.open(file) as image:
            small = image.convert("L").resize((16, 16))
            pixels = list(small.getdata())
        mean = sum(pixels) / len(pixels)
        hashes.append([p > mean for p in pixels])
    same = sum(a == b for a, b in zip(*hashes))
    return same / len(hashes[0])
