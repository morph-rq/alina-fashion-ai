"""Exact, structured selection of allocations (OrderItem rows) for the assistant, voice and exports.

A selection is a set of explicit filters over verified records — season, brand/collection,
customer identities (codes and aliases, never substrings of free text), item ids, statuses,
sizes, colours, categories, prices, markups, source sheet/rows. ``resolve`` turns filters into
the exact list of OrderItem rows and a human summary ("Выбрано: SS27 → Dior cruise → IDS → 3 позиции").

``parse`` reads a Russian/English request into filters deterministically and *merges* it with the
previous selection of the conversation, so "Покажи IDS" after "Открой Dior Cruise" keeps the brand.
When a brand or customer is ambiguous it returns one short question instead of guessing.

Each OrderItem is one customer allocation with a persistent id; ``allocation_group_id`` links the
allocations that came from one product row of the factory file.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import ledger_actions as la
from app.calculations import ZERO, item_amounts, money
from app.models import Customer, Order, OrderItem, Season

READY_STATES = {"Ready", "Assigned to shipment", "In transit", "Arrived", "Delivered"}
NOT_READY_STATES = {"Ordered", "Confirmed", "Production"}

# Category ← keywords in the product name (factory names are mostly English; Russian added for typed names).
CATEGORIES = {
    "dress": ("Платья", ("dress", "gown", "плать")),
    "coat": ("Пальто", ("coat", "trench", "parka", "пальто", "тренч")),
    "jacket": ("Жакеты", ("jacket", "blazer", "bomber", "жакет", "пиджак", "куртк")),
    "suit": ("Костюмы", ("suit", "костюм")),
    "skirt": ("Юбки", ("skirt", "юбк")),
    "trousers": ("Брюки", ("trouser", "pant", "jean", "short", "брюк", "штан", "джинс", "шорт")),
    "top": ("Рубашки и топы", ("shirt", "blouse", " top", "t-shirt", "tee", "polo", "bra", "рубаш", "блуз", "топ", "футболк")),
    "knit": ("Трикотаж", ("sweater", "cardigan", "pullover", "jumper", "knit", "свитер", "кардиган", "джемпер")),
    "shoes": ("Обувь", ("shoe", "boot", "sneaker", "pump", "sandal", "loafer", "mule", "ballerina", "slingback", "espadrille",
                        "обув", "туфл", "ботин", "сапог", "кроссов", "босонож", "лофер", "мюли", "балетк")),
    "bag": ("Сумки", ("bag", "tote", "clutch", "pouch", "сумк", "клатч")),
    "accessory": ("Аксессуары", ("scarf", "belt", "hat", "cap", "glove", "shawl", "stole", "шарф", "ремен", "ремн", "шапк", "перчат", "палантин")),
}
# Spoken category words → category key (stems; plural/case forms all match).
CATEGORY_WORDS = {"плать": "dress", "пальт": "coat", "жакет": "jacket", "пиджак": "jacket", "куртк": "jacket",
                  "костюм": "suit", "юбк": "skirt", "брюк": "trousers", "штан": "trousers", "джинс": "trousers",
                  "рубаш": "top", "блуз": "top", "топ": "top", "футболк": "top", "свитер": "knit", "кардиган": "knit",
                  "трикотаж": "knit", "обув": "shoes", "туфл": "shoes", "ботин": "shoes", "сапог": "shoes",
                  "кроссов": "shoes", "сумк": "bag", "клатч": "bag", "шарф": "accessory", "аксессуар": "accessory",
                  "ремен": "accessory", "ремн": "accessory",
                  "dress": "dress", "coat": "coat", "jacket": "jacket", "suit": "suit", "skirt": "skirt",
                  "trouser": "trousers", "pants": "trousers", "shirt": "top", "knit": "knit", "shoe": "shoes", "bag": "bag"}
COLORS = {"black": ("черн", "black", "noir", "nero"), "white": ("бел", "white", "blanc", "bianco", "ivory", "ecru"),
          "beige": ("беж", "beige", "sand", "camel"), "blue": ("син", "голуб", "blue", "navy", "bleu"),
          "red": ("красн", "red", "rouge"), "green": ("зелен", "зелён", "green", "vert"), "grey": ("сер", "grey", "gray", "gris"),
          "brown": ("корич", "brown", "marron", "chocolate"), "pink": ("розов", "pink", "rose"),
          "yellow": ("желт", "жёлт", "yellow"), "gold": ("золот", "gold"), "silver": ("серебр", "silver")}
STATUS_WORDS = [(r"\bне\s*готов|ещё не готов|еще не готов|not ready", "not_ready"), (r"готов|\bready\b", "ready"),
                (r"отмен|cancel", "cancelled"), (r"в производств", "Production"), (r"отгруж|в пути|in transit", "shipped"),
                (r"доставлен|delivered", "Delivered")]


@dataclass
class Filters:
    season: str | None = None
    brands: list[str] = field(default_factory=list)          # exact Brand.name values (brand or collection = order)
    customers: list[str] = field(default_factory=list)       # Customer.code values
    exclude_customers: list[str] = field(default_factory=list)
    item_ids: list[int] = field(default_factory=list)
    readiness: str | None = None                              # ready | not_ready | cancelled | shipped | <status>
    categories: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    article: str | None = None
    price_min: str | None = None                              # customer unit price, EUR
    price_max: str | None = None
    markup: str | None = None                                 # customer markup in percent ("35")
    sheet: str | None = None
    rows: list[int] = field(default_factory=list)
    unpaid: bool = False
    include_cancelled: bool = False

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], False)}

    @classmethod
    def from_dict(cls, data: dict | None) -> Filters:
        names = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in (data or {}).items() if k in names})


@dataclass
class Selection:
    filters: Filters
    season: Season | None
    items: list[OrderItem]
    summary: str
    question: dict | None = None

    @property
    def customer_codes(self) -> list[str]:
        return sorted({i.customer.code for i in self.items if i.customer})


def category_of(name: str | None) -> str | None:
    text = f" {(name or '').casefold()} "
    for key, (_, words) in CATEGORIES.items():
        if any(w in text for w in words):
            return key
    return None


def color_of(value: str | None) -> set[str]:
    text = (value or "").casefold()
    return {key for key, words in COLORS.items() if any(w in text for w in words)}


def _size_key(value: str) -> str:
    return str(value).strip().upper().replace(",", ".").removesuffix(".0")


def unit_price(item: OrderItem) -> Decimal:
    amounts = item_amounts(item)
    return money(amounts.final / item.quantity) if item.quantity else amounts.final


def resolve(db: Session, filters: Filters, screen: dict | None = None) -> Selection:
    try:
        season = la.find_season(db, filters.season)
    except la.ActionError:
        season = None
    query = select(OrderItem).join(Order)
    if season is not None:
        query = query.where(Order.season_id == season.id)
    if filters.item_ids:
        query = query.where(OrderItem.id.in_([int(i) for i in filters.item_ids]))
    items = list(db.scalars(query.order_by(Order.id, OrderItem.source_row, OrderItem.id)))
    brands = {b.casefold() for b in filters.brands}
    codes = {c.upper() for c in filters.customers}
    excluded = {c.upper() for c in filters.exclude_customers}
    sizes = {_size_key(s) for s in filters.sizes}
    unpaid_customers = _unpaid_customers(db, season) if filters.unpaid and season else set()
    out = []
    for item in items:
        code = item.customer.code.upper() if item.customer else None
        if brands and item.order.brand.name.casefold() not in brands:
            continue
        if codes and code not in codes:
            continue
        if excluded and code in excluded:
            continue
        status = item.status
        wanted = filters.readiness
        if status == "Cancelled" and wanted != "cancelled" and not filters.include_cancelled:
            continue
        if wanted == "ready" and status not in READY_STATES:
            continue
        if wanted == "not_ready" and status not in NOT_READY_STATES:
            continue
        if wanted == "cancelled" and status != "Cancelled":
            continue
        if wanted == "shipped" and status not in READY_STATES - {"Ready"}:
            continue
        if wanted and wanted not in {"ready", "not_ready", "cancelled", "shipped"} and status != wanted:
            continue
        if filters.categories and category_of(item.name) not in filters.categories:
            continue
        if sizes and _size_key(item.size or "") not in sizes:
            continue
        if filters.colors and not color_of(item.color) & set(filters.colors):
            continue
        if filters.article and filters.article.casefold() not in (item.article or "").casefold():
            continue
        if filters.sheet and (item.source_sheet or "").casefold() != filters.sheet.casefold():
            continue
        if filters.rows and item.source_row not in set(filters.rows):
            continue
        if filters.markup is not None and (item.customer is None or item.customer.is_owner
                                           or Decimal(item.markup_percentage or 0) * 100 != Decimal(filters.markup)):
            continue
        if filters.price_min is not None and unit_price(item) < Decimal(filters.price_min):
            continue
        if filters.price_max is not None and unit_price(item) > Decimal(filters.price_max):
            continue
        if filters.unpaid and code not in unpaid_customers:
            continue
        out.append(item)
    return Selection(filters, season, out, describe(filters, season, out))


def _unpaid_customers(db: Session, season: Season) -> set[str]:
    from app.factory_ledger import season_ledger
    ledger = season_ledger(db, season)
    return {code.upper() for code, c in ledger.settlement.customers.items()
            if not c.owner and c.confirmed_to_factory + c.confirmed_to_owner < c.total}


def describe(filters: Filters, season: Season | None, items: list[OrderItem]) -> str:
    parts = [season.name if season else "все сезоны"]
    parts.append(", ".join(filters.brands) if filters.brands else "все бренды")
    if filters.customers:
        parts.append(", ".join(filters.customers))
    elif filters.exclude_customers:
        parts.append("все клиенты, кроме " + ", ".join(filters.exclude_customers))
    extra = []
    labels = {"ready": "готовые", "not_ready": "не готовые", "cancelled": "отменённые", "shipped": "отгруженные"}
    if filters.readiness:
        extra.append(labels.get(filters.readiness, la.STATUS_RU.get(filters.readiness, filters.readiness)))
    if filters.categories:
        extra.append(", ".join(CATEGORIES[c][0].lower() for c in filters.categories if c in CATEGORIES))
    if filters.sizes:
        extra.append("размер " + ", ".join(filters.sizes))
    if filters.colors:
        extra.append("цвет " + ", ".join(filters.colors))
    if filters.article:
        extra.append(f"артикул {filters.article}")
    if filters.price_min or filters.price_max:
        extra.append(f"цена {filters.price_min or '0'}–{filters.price_max or '…'} €")
    if filters.markup:
        extra.append(f"наценка {filters.markup}%")
    if filters.sheet or filters.rows:
        extra.append(f"лист «{filters.sheet or '—'}»" + (f" строки {', '.join(map(str, filters.rows))}" if filters.rows else ""))
    if filters.item_ids:
        extra.append(f"выделено {len(filters.item_ids)}")
    if filters.unpaid:
        extra.append("с неоплаченным остатком")
    if extra:
        parts.append(", ".join(extra))
    count = len(items)
    word = "позиция" if count % 10 == 1 and count % 100 != 11 else "позиции" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "позиций"
    return "Выбрано: " + " → ".join(parts) + f" → {count} {word}"


def selection_table(selection: Selection) -> dict:
    """Rows and verified totals for the result panel (customer prices from the pricing engine)."""
    rows, base, total = [], ZERO, ZERO
    for item in selection.items:
        amounts = item_amounts(item)
        base += amounts.base
        total += amounts.final
        rows.append({"id": item.id, "group": item.allocation_group_id, "brand": item.order.brand.name,
                     "customer": item.customer.code if item.customer else None, "name": item.name or "",
                     "article": item.article or "", "color": item.color or "", "size": item.size or "",
                     "quantity": item.quantity, "base": str(amounts.base), "unit": str(unit_price(item)),
                     "total": str(amounts.final), "markup": str(Decimal(item.markup_percentage or 0) * 100),
                     "status": la.STATUS_RU.get(item.status, item.status), "status_code": item.status,
                     "image": item.image_path, "sheet": item.source_sheet, "row": item.source_row})
    return {"summary": selection.summary, "filters": selection.filters.to_dict(),
            "season": selection.season.name if selection.season else None, "rows": rows,
            "customers": selection.customer_codes, "base_total": str(money(base)), "customer_total": str(money(total))}


# --- Natural language → filters ---------------------------------------------------------

def _translit(word: str) -> str:
    from app.factory_import import TRANSLIT
    return "".join(TRANSLIT.get(ch, ch) for ch in word.upper()).casefold()


def _close(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or min(len(a), len(b)) < 4:
        return False
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1] <= 1


def _latin_words(text: str) -> list[str]:
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text)
    return [w.casefold() if re.fullmatch(r"[A-Za-z0-9]+", w) else _translit(w) for w in words]


def match_brands(text: str, season: Season | None, db: Session) -> tuple[list[str], dict | None]:
    """Brands/collections named in the text. The longest exact name wins ("Dior cruise" over "Dior");
    a bare "Dior" with only several Dior collections and no plain "Dior" is a question, not a guess."""
    names = sorted({o.brand.name for o in (season.orders if season else db.scalars(select(Order)))}, key=len, reverse=True)
    words = _latin_words(text)
    found: list[str] = []
    used: set[int] = set()
    for name in names:
        tokens = _latin_words(name)
        if not tokens:
            continue
        for start in range(len(words) - len(tokens) + 1):
            span = range(start, start + len(tokens))
            if used & set(span):
                continue
            if all(_close(words[start + k], tokens[k]) or words[start + k].startswith(tokens[k]) and len(tokens[k]) >= 4
                   for k in range(len(tokens))):
                found.append(name)
                used |= set(span)
                break
    if found:
        # "Dior" also matched while a longer "Dior cruise" was named at the same place → keep the longest.
        return found, None
    # A family word ("Dior") that only prefixes several collections → ask.
    families: dict[str, list[str]] = {}
    for name in names:
        head = _latin_words(name)[0] if _latin_words(name) else ""
        if len(head) >= 3 and any(_close(w, head) for w in words):
            families.setdefault(head, []).append(name)
    for head, options in families.items():
        if len(options) == 1:
            return options, None
        return [], {"question": "Какой именно заказ?", "options": sorted(options)}
    return [], None


def _number(value: str) -> str | None:
    try:
        return str(Decimal(value.replace(" ", "").replace(",", ".")))
    except InvalidOperation:
        return None


def parse(db: Session, text: str, previous: Filters | None = None, screen: dict | None = None) -> tuple[Filters, dict | None, set[str]]:
    """Filters for ``text``, merged onto ``previous``. Returns (filters, question, changed-keys)."""
    screen = screen or {}
    lowered = text.casefold()
    base = Filters.from_dict(previous.to_dict()) if previous else Filters()
    changed: set[str] = set()
    if screen.get("fresh") or previous is None:
        # The owner opened another page since the last message: "здесь / это / ей" now mean what is on screen.
        if screen.get("season"):
            base.season = screen["season"]
        if screen.get("brand") or screen.get("fresh"):
            base.brands = [screen["brand"]] if screen.get("brand") else ([] if screen.get("fresh") else base.brands)
        if screen.get("customer") or screen.get("fresh"):
            base.customers = [screen["customer"]] if screen.get("customer") and screen["customer"] != "—" else []
            base.exclude_customers = []
        if screen.get("fresh"):
            base.item_ids, base.readiness = [], ({"Ready": "ready", "Cancelled": "cancelled"}.get(screen.get("status") or "") or None)
    season_match = re.search(r"\b(SS|FW|AW|PF|RS)\s?(\d{2})\b", text, re.I)
    if season_match:
        base.season = season_match.group(1).upper() + season_match.group(2)
        changed.add("season")
    elif not base.season:
        base.season = screen.get("season")
    try:
        season = la.find_season(db, base.season)
    except la.ActionError:
        season = None
    if season is not None:
        base.season = season.name
    question = None

    brands, brand_question = match_brands(text, season, db)
    if brand_question:
        question = brand_question
    if brands:
        if base.brands != brands:
            base.item_ids = []
        base.brands, changed = brands, changed | {"brands"}
    elif re.search(r"(все|всем|по всем)\s+бренд", lowered):
        base.brands, changed = [], changed | {"brands"}

    found = customers_in(db, text)
    if isinstance(found, dict):
        question = question or found
        found = []
    if re.search(r"(всех|всем|остальн|другим|другие)\s+(остальн\w*\s+)?клиент|для остальных|остальным", lowered):
        if re.search(r"остальн|друг", lowered) and (previous and previous.customers):
            base.exclude_customers = list(previous.customers)
        else:
            base.exclude_customers = []
        base.customers = []
        changed.add("customers")
    elif found:
        base.customers, base.exclude_customers = [c.code for c in found], []
        if not _mentions_selected(lowered):
            base.item_ids = []
        changed.add("customers")

    if not found and question is None:
        named = re.search(r"\b(?:для|у|клиент\w*)\s+([A-Za-zА-Яа-яЁё]{2,})", text)
        word = named.group(1) if named else ""
        if word and word.casefold() not in SCOPE_WORDS and _translit(word) not in {w for b in brands for w in _latin_words(b)} \
                and not match_brands(word, season, db)[0]:
            codes = [c.code for c in db.scalars(select(Customer).where(Customer.is_owner.is_(False)).order_by(Customer.code))]
            question = {"question": f"Клиент «{word}» не найден. Для кого?", "options": codes[:10]}
    if _mentions_selected(lowered) and screen.get("selected_item_ids"):
        base.item_ids = [int(i) for i in screen["selected_item_ids"]][:500]
        changed.add("item_ids")
    for pattern, value in STATUS_WORDS:
        if re.search(pattern, lowered):
            if value == "ready" and re.search(r"(отметь|пометь)\s.*готов", lowered):
                break      # "отметь готовыми" is an edit, not a filter
            base.readiness = value
            changed.add("readiness")
            break
    if re.search(r"все (вещи|позиции|товары)|всё подряд|без фильтр", lowered):
        base.readiness, base.categories, base.sizes, base.colors = None, [], [], []
        changed.add("reset")
    cats = []
    for word in re.findall(r"[a-zа-яё]+", lowered):
        for stem, key in CATEGORY_WORDS.items():
            if word.startswith(stem) and key not in cats:
                cats.append(key)
    if cats:
        base.categories = cats
        changed.add("categories")
    sizes = re.findall(r"размер\w*\s+((?:\d{2}(?:[.,]5)?|XXS|XS|S|M|L|XL|XXL|UNI)(?:\s*(?:,|и|или)\s*(?:\d{2}(?:[.,]5)?|XXS|XS|S|M|L|XL|XXL))*)", text, re.I)
    sizes += re.findall(r"\b(\d{2}(?:[.,]5)?)(?:-?(?:го|й|ого))?\s+размер", text, re.I)
    if sizes:
        values = []
        for chunk in sizes:
            values += re.findall(r"\d{2}(?:[.,]5)?|XXS|XS|XXL|XL|UNI|\b[SML]\b", chunk, re.I)
        base.sizes = list(dict.fromkeys(v.upper().replace(",", ".") for v in values))
        changed.add("sizes")
    colors = [key for key, words in COLORS.items()
              if any(re.search(rf"\b{w}\w{{0,3}}?(ый|ий|ой|ая|яя|ое|ее|ые|ие|ых|их|ого|его|ую|юю|ым|ыми)\b", lowered) if re.match(r"[а-яё]", w)
                     else re.search(rf"\b{w}\b", lowered) for w in words)]
    if colors:
        base.colors = colors
        changed.add("colors")
    article = re.search(r"артикул\w*\s+([A-Z0-9][A-Z0-9-]{3,})", text, re.I)
    if article:
        base.article = article.group(1).upper()
        changed.add("article")
    low = re.search(r"(?:дороже|от|больше|свыше)\s+(\d[\d\s]*[.,]?\d*)\s*(?:€|евро|eur)", lowered)
    high = re.search(r"(?:дешевле|до|меньше|не дороже)\s+(\d[\d\s]*[.,]?\d*)\s*(?:€|евро|eur)", lowered)
    if low:
        base.price_min = _number(low.group(1))
        changed.add("price")
    if high:
        base.price_max = _number(high.group(1))
        changed.add("price")
    sheet = re.search(r"(?:лист|вкладк\w*)\s+[«\"']?([^«»\"',.]+?)[»\"']?(?:\s+строк|\s*$|,)", text, re.I)
    if sheet:
        base.sheet = sheet.group(1).strip()
        changed.add("sheet")
    rows = re.search(r"строк\w*\s+((?:\d+\s*(?:,|и|-|–)\s*)*\d+)", lowered)
    if rows and not re.search(r"(эту|эти|выделенн)\s+строк", lowered):
        values: list[int] = []
        for part in re.split(r"\s*(?:,|и)\s*", rows.group(1)):
            if re.fullmatch(r"\d+\s*[-–]\s*\d+", part):
                a, b = (int(x) for x in re.split(r"[-–]", part))
                values += list(range(a, min(b, a + 500) + 1))
            elif part.strip().isdigit():
                values.append(int(part))
        base.rows = values
        changed.add("rows")
    if re.search(r"неоплач|не оплач|не заплат|долж", lowered) and re.search(r"вещ|позици|товар|клиент", lowered):
        base.unpaid = True
        changed.add("unpaid")
    return base, question, changed


SCOPE_WORDS = {"всех", "всем", "остальных", "остальным", "каждого", "каждому", "клиентов", "клиента", "фабрики", "фабрике", "себя",
               "меня", "нас", "них", "неё", "нее", "ней", "него", "него", "этого", "этой", "этих", "всего", "сезона", "бренда",
               "бренду", "заказа", "готовых", "выделенных", "выбранных", "платьев", "вещей", "позиций", "товаров", "размера",
               "файла", "excel", "pdf", "всё", "все", "той", "того", "тех", "одного", "одной", "ready", "the"}


def _mentions_selected(lowered: str) -> bool:
    return bool(re.search(r"выделенн|выбранн|отмеченн|\bэт(у|и|от|а|о|ой)\b|вот (это|эту|эти|этот)", lowered))


IGNORED_WORDS = {"для", "ей", "им", "мне", "excel", "pdf", "и", "на", "по", "сделай", "поставь", "а", "только", "все", "всё",
                 "покажи", "открой", "размер", "размера", "цвет", "по", "из", "у", "в", "со", "с", "the", "and", "for", "то",
                 "да", "нет", "сохрани", "это", "эти", "вещи", "вещей", "файл", "таблицу", "таблица", "процентов", "процента"}


def customers_in(db: Session, text: str) -> list[Customer] | dict:
    """Customers named in the text, through verified codes/aliases. Brand words never count as customers."""
    brand_words = {w for o in db.scalars(select(Order)) for w in _latin_words(o.brand.name)}
    found, seen = [], set()
    for word in re.findall(r"[A-Za-zА-Яа-яЁё]{2,}", text):
        if word.casefold() in IGNORED_WORDS or _translit(word) in brand_words or word.casefold() in brand_words:
            continue
        try:
            customer = la.find_customer(db, word)
        except la.ActionError as exc:
            if "нескольким" in str(exc):
                options = re.findall(r"[A-ZА-Я0-9]{2,}", str(exc).split(":", 1)[-1])
                return {"question": f"Кого вы имеете в виду — {', '.join(options)}?", "options": options}
            continue
        if customer.id not in seen:
            seen.add(customer.id)
            found.append(customer)
    return found
