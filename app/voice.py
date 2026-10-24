"""Voice for the assistant: ElevenLabs Scribe v2 Realtime (speech → text) and streamed TTS (text → speech).

Architecture: browser microphone → 16 kHz PCM → ElevenLabs realtime WebSocket, authorised by a
server-issued *single-use* token (15 min, consumed on first use) → transcript → the same
``agent.handle`` as typed chat → reply → ``to_speech`` (short, plain Russian, no code/JSON/formulas)
→ ``POST /api/voice/speech`` → ``GET /api/voice/speech/{id}`` streams MP3 from ElevenLabs.

The permanent key never reaches the browser: it lives in ``secret_store`` (AES-GCM under
data/private, excluded from backups) or in ``ELEVENLABS_API_KEY``; only its last four
characters are shown. Microphone audio goes from the browser straight to ElevenLabs and is
never stored here; an uploaded voice recording is transcribed in memory and discarded.
Spoken amounts are always copied from the reply the calculation engine produced.
"""
from __future__ import annotations

import os
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterator
from urllib.parse import quote, urlencode

import httpx
from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import secret_store

REF = "voice-settings"
API = "https://api.elevenlabs.io"
REALTIME_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
REALTIME_MODEL = "scribe_v2_realtime"
BATCH_MODEL = "scribe_v2"
TTS_MODELS = {
    "eleven_v4_turbo": "Eleven v4 Turbo — самый живой голос для диалога (рекомендуется)",
    "eleven_flash_v2_5": "Eleven Flash v2.5 — быстрее и дешевле",
    "eleven_multilingual_v2": "Eleven Multilingual v2 — ровный, для длинных ответов",
}
DEFAULT_MODEL = "eleven_v4_turbo"
FALLBACK_MODEL = "eleven_flash_v2_5"
# ElevenLabs default voices (available to every account; all speak Russian with the multilingual models).
VOICES = {
    "EXAVITQu4vr4xnSDxMaL": "Sarah — спокойный, деловой (жен.)",
    "Xb7hH8MSUJpSbSDYk0k2": "Alice — уверенный, чёткий (жен.)",
    "cgSgspJ2msm6clMCkdW9": "Jessica — тёплый, живой (жен.)",
    "XrExE9yKIg1WjnnlVkGX": "Matilda — мягкий (жен.)",
    "21m00Tcm4TlvDq8ikWAM": "Rachel — нейтральный (жен.)",
    "JBFqnCBsd6RMkjVDRZzb": "George — тёплый (муж.)",
    "onwK4e9ZLuTAKqWW03F9": "Daniel — строгий (муж.)",
}
DEFAULT_VOICE = "EXAVITQu4vr4xnSDxMaL"
MAX_SPEECH_CHARS = 600
MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class VoiceError(ValueError):
    """Operator-readable problem with ElevenLabs (no secrets in the message)."""


# --- Settings ---------------------------------------------------------------------------

def stored() -> dict:
    try:
        return secret_store.load(REF)
    except Exception:       # unreadable store: behave as "not configured", never crash pages
        return {}


@dataclass
class VoiceSettings:
    api_key: str
    voice_id: str
    model: str
    language: str
    speak_replies: bool
    review_before_send: bool
    source: str | None
    saved_at: str | None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def hint(self) -> str | None:
        return ("…" + self.api_key[-4:]) if len(self.api_key) > 8 else None


def current() -> VoiceSettings:
    data = stored()
    env_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    key = data.get("api_key") or env_key
    return VoiceSettings(
        api_key=key,
        voice_id=data.get("voice_id") or os.environ.get("ELEVENLABS_VOICE_ID") or DEFAULT_VOICE,
        model=data.get("model") or os.environ.get("ELEVENLABS_TTS_MODEL") or DEFAULT_MODEL,
        language=data.get("language") or "ru",
        speak_replies=data.get("speak_replies", True),
        review_before_send=data.get("review_before_send", False),
        source="app" if data.get("api_key") else ("env" if env_key else None),
        saved_at=data.get("saved_at"))


def clean_key(value: str) -> str:
    """Pasted keys often carry a stray leading '-' or spaces."""
    return "".join(value.split()).lstrip("-–—").strip("\"'")


def key_problem(key: str) -> str | None:
    if not key:
        return "Вставьте ключ ElevenLabs."
    if re.fullmatch(r"[0-9a-f]{64}", key):
        return ("Это ID ключа, а не сам ключ. Сам ключ начинается с «sk_» и показывается один раз при создании "
                "(elevenlabs.io › Developers › API Keys › Create / Rotate).")
    if not key.startswith("sk_"):
        return "Ключ ElevenLabs начинается с «sk_». Скопируйте его ещё раз."
    return None


# --- ElevenLabs client ------------------------------------------------------------------

def _problem(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail") or {}
    except ValueError:
        detail = {}
    status = detail.get("status") if isinstance(detail, dict) else None
    message = detail.get("message", "") if isinstance(detail, dict) else str(detail)
    if status == "api_key_id_used_as_api_key":
        return key_problem("0" * 64) or ""
    if status == "missing_permissions":
        permission = re.search(r"permission (\w+)", message)
        return f"У ключа нет разрешения {permission.group(1) if permission else ''}. Включите его в настройках ключа на elevenlabs.io."
    if response.status_code in (401, 403):
        return "ElevenLabs не принял ключ. Проверьте ключ в Настройках › Голос."
    if response.status_code == 429 or status in ("quota_exceeded", "too_many_concurrent_requests"):
        return "Лимит ElevenLabs исчерпан или слишком много запросов. Попробуйте через минуту или проверьте тариф."
    if response.status_code in (400, 404, 422) and "model" in message.casefold():
        return f"Модель недоступна для этого ключа: {message[:160]}"
    if response.status_code in (400, 404, 422) and "voice" in message.casefold():
        return "Этот голос недоступен. Выберите другой в Настройках › Голос."
    return f"ElevenLabs ответил {response.status_code}. {message[:160]}".strip()


class ElevenLabs:
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, base: str = API):
        if not api_key:
            raise VoiceError("Голос не подключён: добавьте ключ ElevenLabs в Настройках › Голос.")
        self.client = httpx.Client(base_url=base, transport=transport, timeout=httpx.Timeout(30, connect=10),
                                   headers={"xi-api-key": api_key})

    def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = self.client.request(method, url, **kwargs)
        except httpx.TimeoutException:
            raise VoiceError("ElevenLabs не ответил вовремя. Проверьте интернет и попробуйте ещё раз.") from None
        except httpx.HTTPError:
            raise VoiceError("Нет связи с ElevenLabs. Проверьте интернет.") from None
        if response.status_code >= 400:
            raise VoiceError(_problem(response))
        return response

    def realtime_token(self) -> str:
        return self._call("POST", "/v1/single-use-token/realtime_scribe").json()["token"]

    def synthesize(self, text: str, voice_id: str, model: str, language: str = "ru",
                   output_format: str = "mp3_44100_128") -> Iterator[bytes]:
        """Streams MP3. Falls back to Flash v2.5 if the chosen model is not available to the key."""
        body = {"text": text, "model_id": model, "language_code": language,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75, "speed": 1.0}}
        url = f"/v1/text-to-speech/{quote(voice_id, safe='')}/stream?output_format={output_format}"
        try:
            request = self.client.build_request("POST", url, json=body)
            response = self.client.send(request, stream=True)
        except httpx.HTTPError:
            raise VoiceError("Нет связи с ElevenLabs. Проверьте интернет.") from None
        if response.status_code >= 400:
            response.read()
            problem = _problem(response)
            response.close()
            if model != FALLBACK_MODEL and response.status_code in (400, 404, 422) and "Модель" in problem:
                yield from self.synthesize(text, voice_id, FALLBACK_MODEL, language, output_format)
                return
            raise VoiceError(problem)
        try:
            yield from response.iter_bytes(4096)
        finally:
            response.close()

    def transcribe(self, data: bytes, filename: str, language: str = "ru", keyterms: list[str] | None = None) -> dict:
        form = {"model_id": BATCH_MODEL, "language_code": language, "tag_audio_events": "false"}
        files = [("file", (filename or "audio", data))]
        for term in (keyterms or [])[:50]:
            files.append(("keyterms", (None, term)))
        return self._call("POST", "/v1/speech-to-text", data=form, files=files).json()

    def voices(self) -> list[dict] | None:
        """The account's voices, or None when the key may not list them (restricted keys)."""
        try:
            data = self._call("GET", "/v2/voices", params={"page_size": 100}).json()
        except VoiceError:
            return None
        return [{"id": v["voice_id"], "name": v.get("name", ""), "category": v.get("category")} for v in data.get("voices", [])]

    def usage(self) -> dict | None:
        try:
            data = self._call("GET", "/v1/user/subscription").json()
        except VoiceError:
            return None
        return {"used": data.get("character_count"), "limit": data.get("character_limit"), "tier": data.get("tier")}

    def check(self, voice_id: str, model: str) -> dict:
        """Real test: a realtime transcription token and a one-word speech sample."""
        self.realtime_token()
        audio = b"".join(self.synthesize("Готово.", voice_id, model))
        if len(audio) < 500:
            raise VoiceError("ElevenLabs вернул пустой звук.")
        return {"stt": True, "tts": True, "bytes": len(audio)}


def client(settings: VoiceSettings | None = None, transport: httpx.BaseTransport | None = None) -> ElevenLabs:
    return ElevenLabs((settings or current()).api_key, transport)


# --- Transcript normalisation -----------------------------------------------------------

LETTER_NAMES = {"A": ["эй"], "B": ["би", "бэ"], "C": ["си", "цэ"], "D": ["ди", "дэ"], "E": ["и"], "F": ["эф"],
                "G": ["джи", "гэ"], "H": ["эйч", "аш"], "I": ["ай"], "J": ["джей"], "K": ["кей", "ка"], "L": ["эл", "эль"],
                "M": ["эм"], "N": ["эн"], "O": ["оу"], "P": ["пи", "пэ"], "Q": ["кью"], "R": ["ар", "эр"], "S": ["эс"],
                "T": ["ти", "тэ"], "U": ["ю"], "V": ["ви", "вэ"], "W": ["дабл ю"], "X": ["икс"], "Y": ["уай"], "Z": ["зед", "зет"]}
BRAND_WORDS = {"диор": "Dior", "круиз": "Cruise", "лоро": "Loro", "пиана": "Piana", "селин": "Celine", "шанель": "Chanel",
               "прада": "Prada", "миу": "Miu", "гуччи": "Gucci", "валентино": "Valentino", "боттега": "Bottega",
               "венета": "Veneta", "эрмес": "Hermes", "фенди": "Fendi", "баленсиага": "Balenciaga", "лоран": "Laurent",
               "брунелло": "Brunello", "кучинелли": "Cucinelli", "мара": "Mara", "сандер": "Sander", "лоэве": "Loewe",
               "лоеве": "Loewe", "барберри": "Burberry", "бёрберри": "Burberry", "алайя": "Alaia", "живанши": "Givenchy",
               "версаче": "Versace", "кензо": "Kenzo", "моклер": "Moncler", "монклер": "Moncler", "эскада": "Escada",
               "шиапарелли": "Schiaparelli", "ферражамо": "Ferragamo", "феррагамо": "Ferragamo", "тодс": "Tods"}
UNITS = {"ноль": 0, "один": 1, "одна": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
         "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
         "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
         "девятнадцать": 19}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
        "восемьдесят": 80, "девяносто": 90}
HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
            "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
# Ordinals used for sizes ("тридцать восьмой / восьмого размера").
ORDINAL_UNITS = {"шест": 6, "седьм": 7, "восьм": 8, "девят": 9, "перв": 1, "втор": 2, "трет": 3, "четвёрт": 4, "четверт": 4, "пят": 5}
ORDINAL_TENS = {"тридцат": 30, "сороков": 40, "пятидесят": 50, "двадцат": 20}


def _words_to_numbers(text: str) -> str:
    tokens = re.split(r"(\s+)", text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        word = tokens[i].casefold().strip(",.")
        if word in UNITS or word in TENS or word in HUNDREDS or word in ("тысяча", "тысячи", "тысяч"):
            total, current, j, consumed = 0, 0, i, []
            while j < len(tokens):
                w = tokens[j].casefold().strip(",.")
                if not w.strip():
                    j += 1
                    continue
                if w in HUNDREDS:
                    current += HUNDREDS[w]
                elif w in TENS:
                    current += TENS[w]
                elif w in UNITS:
                    current += UNITS[w]
                elif w in ("тысяча", "тысячи", "тысяч"):
                    total += (current or 1) * 1000
                    current = 0
                else:
                    break
                consumed.append(j)
                j += 1
            value = total + current
            trailing = tokens[consumed[-1]][len(tokens[consumed[-1]].rstrip(",.")):]
            out.append(str(value) + trailing)
            i = consumed[-1] + 1
            continue
        out.append(tokens[i])
        i += 1
    result = "".join(out)
    # "тридцать восьмого размера" → "30 восьмого" → 38; "сорокового" → 40
    result = re.sub(r"(\d+)\s+(шест|седьм|восьм|девят|перв|втор|трет|четв[её]рт|пят)(ой|ого|ый|ом|ая)\b",
                    lambda m: str(int(m.group(1)) + next(v for k, v in ORDINAL_UNITS.items() if m.group(2).casefold().startswith(k)))
                    if int(m.group(1)) % 10 == 0 else m.group(0), result, flags=re.I)
    result = re.sub(r"\b(сороков|пятидесят|тридцат)(ой|ого|ый|ом)\b", lambda m: str(next(v for k, v in ORDINAL_TENS.items() if m.group(1).casefold().startswith(k))), result, flags=re.I)
    return result


def _spoken_code_pattern(code: str) -> str | None:
    if not re.fullmatch(r"[A-Z]{2,4}", code):
        return None
    parts = ["(?:" + "|".join(LETTER_NAMES[ch]) + ")" for ch in code]
    return r"(?<![\w-])" + r"[\s-]?".join(parts) + r"(?![\w-])"


def normalize_transcript(text: str, codes: list[str], brands: list[str]) -> str:
    """Spoken forms → the names the app uses. Raw text is kept separately for the audit trail.

    «ай ди эс» → IDS, «эс эс» → SS, «тридцать пять процентов» → 35%, «диор круиз» → Dior Cruise,
    «сорокового размера» → 40 размера. Numbers already written as digits are left exactly as heard."""
    result = _words_to_numbers(text)
    result = re.sub(r"(\d+(?:[.,]\d+)?)\s*(?:процент\w*|%)", r"\1%", result, flags=re.I)
    # "процент на 30", "наценку 35", "наценка в 30" → "30%" (the number after the word is the percentage)
    result = re.sub(r"\b(процент\w*|наценк\w*|markup)\s+(?:на\s+|в\s+|до\s+)?(\d{1,3}(?:[.,]\d+)?)(?![\d%.,])", r"\1 \2%", result, flags=re.I)
    result = re.sub(r"(\d+)\s+с\s+половиной\s*%", lambda m: f"{m.group(1)}.5%", result, flags=re.I)
    for code in sorted(codes, key=len, reverse=True):
        pattern = _spoken_code_pattern(code.upper())
        if pattern:
            result = re.sub(pattern, code.upper(), result, flags=re.I)
    known = {b.casefold(): b for brand in brands for b in brand.split()}
    def brand_word(match: re.Match) -> str:
        word = match.group(0)
        low = word.casefold()
        for stem, latin in BRAND_WORDS.items():
            if low == stem or (low.startswith(stem) and len(low) - len(stem) <= 2):
                return known.get(latin.casefold(), latin)
        return word
    result = re.sub(r"[А-Яа-яЁё]{3,}", brand_word, result)
    return re.sub(r"\s{2,}", " ", result).strip()


KEYTERM_MAX = 20


def keyterms(db: Session) -> list[str]:
    """Words Scribe should expect: customer codes and aliases, brand names, business terms."""
    from app.models import Brand, Customer, CustomerAlias
    terms = [c.code for c in db.scalars(select(Customer))]
    terms += [a.alias for a in db.scalars(select(CustomerAlias))]
    terms += [b.name for b in db.scalars(select(Brand))]
    terms += ["Cruise", "предоплата", "наценка", "Excel", "PDF"]
    clean = []
    for term in terms:
        term = re.sub(r"\s+", " ", term or "").strip()
        # Scribe accepts key terms of at most 20 characters: a longer name ("Brunello Cucinelli women")
        # is given word by word instead.
        for part in ([term] if len(term) <= KEYTERM_MAX else [w for w in term.split(" ") if len(w) >= 3]):
            part = part[:KEYTERM_MAX]
            if len(part) >= 2 and part.casefold() not in {t.casefold() for t in clean}:
                clean.append(part)
    return clean[:100]


# --- What gets spoken -------------------------------------------------------------------

def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n)
    if n % 10 == 1 and n % 100 != 11:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def _amount_words(raw: str) -> str:
    text = raw.replace(" ", " ").replace(" ", " ").strip()
    compact = text.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", compact):
        compact = compact.replace(",", "")
    compact = compact.replace(",", ".")
    try:
        value = Decimal(compact)
    except InvalidOperation:
        return raw
    euros = int(value)
    cents = int((value - euros) * 100)
    spoken = f"{euros:,}".replace(",", " ") + " " + _plural(euros, "евро", "евро", "евро")
    if cents:
        spoken += f" {cents} " + _plural(cents, "цент", "цента", "центов")
    return spoken


def _percent_words(match: re.Match) -> str:
    number = match.group(1).replace(",", ".")
    if "." in number and not number.endswith(".0"):
        return number.replace(".", ",") + " процента"
    n = int(float(number))
    return f"{n} " + _plural(n, "процент", "процента", "процентов")


def spoken_code(code: str) -> str:
    return "-".join(LETTER_NAMES[ch][0] for ch in code) if re.fullmatch(r"[A-Z]{2,4}", code) else code


def to_speech(text: str, codes: list[str] | None = None, limit: int = 320) -> str:
    """A short, speakable version of a reply: no code, JSON, formulas, links or tables; amounts in words
    the voice reads naturally. Long replies keep the first sentences and point to the screen."""
    if not text:
        return ""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`[^`]*`", " ", t)
    t = re.sub(r"\{[^{}]*\}|\[[^\[\]]*\]\([^)]*\)", " ", t)        # JSON-ish blobs and markdown links
    t = re.sub(r"https?://\S+|(?<![\w])/[a-z][\w/.-]*(?:\?\S*)?", " ", t)
    t = re.sub(r"=[A-Z]+\([^)]*\)|=[A-Z]+\d+[*/+-][A-Z0-9.]+", " ", t)   # spreadsheet formulas
    t = re.sub(r"[*_#>|•⚠✎→]+", " ", t)
    lines = [l.strip(" -:") for l in t.splitlines() if l.strip(" -:")]
    t = ". ".join(lines)
    amount = r"(\d(?:[\d\u00a0\u202f .,]*\d)?)"
    t = re.sub(r"€\s?" + amount, lambda m: _amount_words(m.group(1)), t)
    t = re.sub(r"(?<![\w.,])" + amount + r"\s?(?:€|EUR|евро)", lambda m: _amount_words(m.group(1)), t)
    t = re.sub(r"(\d+(?:[.,]\d+)?)\s?%", _percent_words, t)
    for code in sorted(codes or [], key=len, reverse=True):
        if re.fullmatch(r"[A-Z]{2,4}", code):
            t = re.sub(rf"(?<![A-Za-z]){code}(?![A-Za-z])", spoken_code(code), t)
    t = re.sub(r"\s+", " ", t).replace(" .", ".").replace("..", ".").strip(" .")
    if len(t) <= limit:
        return t + "." if t and t[-1] not in ".!?" else t
    sentences = re.split(r"(?<=[.!?])\s+", t)
    short = ""
    for sentence in sentences:
        if len(short) + len(sentence) > limit - 40:
            break
        short += sentence + " "
    short = short.strip() or t[: limit - 40].rsplit(" ", 1)[0]
    return short.rstrip(".") + ". Подробности на экране."


# --- Speech requests (short-lived, in memory) ---------------------------------------------

class _SpeechQueue:
    """Text waiting to be streamed (id → text) and a small cache of finished audio, so a repeated phrase
    ("Готово.") is not paid for twice. Nothing is written to disk."""

    def __init__(self, ttl: int = 120, cache_size: int = 24):
        self.ttl, self.cache_size = ttl, cache_size
        self.pending: dict[str, tuple[float, str, str, str]] = {}
        self.cache: OrderedDict[tuple[str, str, str], bytes] = OrderedDict()

    def add(self, text: str, voice_id: str, model: str) -> str:
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] < self.ttl}
        token = secrets.token_urlsafe(12)
        self.pending[token] = (now, text, voice_id, model)
        return token

    def take(self, token: str) -> tuple[str, str, str] | None:
        entry = self.pending.pop(token, None)
        if not entry or time.monotonic() - entry[0] > self.ttl:
            return None
        return entry[1], entry[2], entry[3]

    def cached(self, key: tuple[str, str, str]) -> bytes | None:
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None

    def store(self, key: tuple[str, str, str], audio: bytes) -> None:
        if len(key[0]) <= 80:
            self.cache[key] = audio
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)


SPEECH = _SpeechQueue()


class _RateLimit:
    def __init__(self, per_minute: int):
        self.per_minute, self.calls = per_minute, []

    def allow(self) -> bool:
        now = time.monotonic()
        self.calls = [t for t in self.calls if now - t < 60]
        if len(self.calls) >= self.per_minute:
            return False
        self.calls.append(now)
        return True


TOKENS = _RateLimit(40)


def realtime_url(token: str, language: str, terms: list[str], hands_free: bool) -> str:
    params = [("model_id", REALTIME_MODEL), ("token", token), ("audio_format", "pcm_16000"),
              ("language_code", language), ("commit_strategy", "vad" if hands_free else "manual")]
    if hands_free:
        params += [("vad_silence_threshold_secs", "1.1"), ("vad_threshold", "0.45"), ("min_speech_duration_ms", "200")]
    params += [("keyterms", t) for t in terms[:50]]
    return REALTIME_URL + "?" + urlencode(params)


# --- Routes -------------------------------------------------------------------------------

def status() -> dict:
    s = current()
    return {"configured": s.configured, "source": s.source, "hint": s.hint, "voice_id": s.voice_id,
            "voice_name": VOICES.get(s.voice_id, s.voice_id).split(" —")[0], "model": s.model, "language": s.language,
            "speak_replies": s.speak_replies, "review_before_send": s.review_before_send, "saved_at": s.saved_at}


def install_voice(app, templates, context, get_db):
    @app.get("/settings/voice")
    def voice_page(request: Request):
        s = current()
        voices = dict(VOICES)
        account = None
        if s.configured and request.query_params.get("list") == "1":
            try:
                account = client(s).voices()
            except VoiceError:
                account = None
            for v in account or []:
                voices.setdefault(v["id"], f"{v['name']} — ваш голос")
        if s.voice_id not in voices:
            voices[s.voice_id] = f"{s.voice_id} — свой голос"
        return templates.TemplateResponse(request, "settings_voice.html", context(
            request, voice=status(), voices=voices, models=TTS_MODELS, account_voices=account,
            message=request.query_params.get("message"), error=request.query_params.get("error")))

    @app.post("/settings/voice")
    def voice_save(action: str = Form("save"), api_key: str = Form(""), voice_id: str = Form(DEFAULT_VOICE),
                   custom_voice_id: str = Form(""), model: str = Form(DEFAULT_MODEL), language: str = Form("ru"),
                   speak_replies: str = Form(""), review_before_send: str = Form("")):
        def back(**params):
            return RedirectResponse("/settings/voice?" + urlencode(params), 303)

        data = dict(stored())
        if action == "remove":
            secret_store.delete(REF)
            return back(message="Ключ ElevenLabs удалён из приложения. Голос выключен (ключ из .env, если есть, снова используется).")
        if model not in TTS_MODELS:
            return back(error="Выберите модель из списка.")
        chosen_voice = custom_voice_id.strip() or voice_id
        if not re.fullmatch(r"[A-Za-z0-9]{10,40}", chosen_voice):
            return back(error="ID голоса выглядит неверно (латинские буквы и цифры).")
        key = clean_key(api_key) if api_key.strip() else data.get("api_key") or os.environ.get("ELEVENLABS_API_KEY", "").strip()
        if api_key.strip() and key_problem(key):
            return back(error=key_problem(key))
        if not key:
            return back(error="Вставьте ключ ElevenLabs.")
        settings = VoiceSettings(key, chosen_voice, model, language if language in ("ru", "en") else "ru",
                                 bool(speak_replies), bool(review_before_send), "app", None)
        if action == "test":
            try:
                client(settings).check(settings.voice_id, settings.model)
            except VoiceError as exc:
                return back(error=f"Проверка не прошла: {exc}")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if api_key.strip() or data.get("api_key"):
            data["api_key"] = key
        data.update(voice_id=settings.voice_id, model=settings.model, language=settings.language,
                    speak_replies=settings.speak_replies, review_before_send=settings.review_before_send, saved_at=stamp)
        secret_store.save(REF, data)
        return back(message=("Связь работает: распознавание и голос отвечают. " if action == "test" else "") + "Сохранено.")

    @app.get("/api/voice/config")
    def voice_config():
        return JSONResponse(status() | {"models": TTS_MODELS})

    @app.post("/api/voice/token")
    def voice_token(request: Request, hands_free: bool = False, db: Session = Depends(get_db)):
        s = current()
        if not s.configured:
            return JSONResponse({"error": "Голос не подключён. Откройте Настройки › Голос и вставьте ключ ElevenLabs.",
                                 "setup": "/settings/voice"}, status_code=409)
        if not TOKENS.allow():
            return JSONResponse({"error": "Слишком часто. Подождите минуту."}, status_code=429)
        try:
            token = client(s).realtime_token()
        except VoiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"url": realtime_url(token, s.language, keyterms(db), hands_free), "sample_rate": 16000,
                             "expires_in": 900})

    @app.post("/api/voice/normalize")
    async def voice_normalize(request: Request, db: Session = Depends(get_db)):
        body = await request.json()
        raw = str(body.get("text") or "")[:4000]
        from app.models import Brand, Customer
        return JSONResponse({"raw": raw, "text": normalize_transcript(
            raw, [c.code for c in db.scalars(select(Customer))], [b.name for b in db.scalars(select(Brand))])})

    @app.post("/api/voice/speech")
    async def voice_speech(request: Request, db: Session = Depends(get_db)):
        s = current()
        if not s.configured:
            return JSONResponse({"error": "Голос не подключён."}, status_code=409)
        body = await request.json()
        from app.models import Customer
        text = to_speech(str(body.get("text") or "")[:4000], [c.code for c in db.scalars(select(Customer))])
        if not text:
            return JSONResponse({"error": "Нечего произносить."}, status_code=422)
        return JSONResponse({"id": SPEECH.add(text[:MAX_SPEECH_CHARS], s.voice_id, s.model), "text": text})

    @app.get("/api/voice/speech/{token}")
    def voice_stream(token: str):
        entry = SPEECH.take(token)
        if entry is None:
            raise HTTPException(404, "Фраза устарела")
        text, voice_id, model = entry
        key = (text, voice_id, model)
        hit = SPEECH.cached(key)
        if hit is not None:
            return StreamingResponse(iter([hit]), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})
        s = current()
        try:
            stream = client(s).synthesize(text, voice_id, model, s.language)
            first = next(stream)
        except (VoiceError, StopIteration) as exc:
            return JSONResponse({"error": str(exc) or "Пустой ответ ElevenLabs."}, status_code=502)

        def body():
            chunks = [first]
            yield first
            for chunk in stream:
                chunks.append(chunk)
                yield chunk
            SPEECH.store(key, b"".join(chunks))
        return StreamingResponse(body(), media_type="audio/mpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/voice/transcribe")
    def voice_transcribe(file: UploadFile = File(...), db: Session = Depends(get_db)):
        s = current()
        if not s.configured:
            return JSONResponse({"error": "Голос не подключён."}, status_code=409)
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "Запись больше 25 МБ."}, status_code=413)
        try:
            result = client(s).transcribe(data, file.filename or "audio", s.language, keyterms(db))
        except VoiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        finally:
            del data      # recordings are not kept
        from app.models import Brand, Customer
        raw = result.get("text", "")
        return JSONResponse({"raw": raw, "text": normalize_transcript(
            raw, [c.code for c in db.scalars(select(Customer))], [b.name for b in db.scalars(select(Brand))])})

    @app.get("/api/voice/usage")
    def voice_usage():
        s = current()
        if not s.configured:
            return JSONResponse({"configured": False})
        try:
            usage = client(s).usage()
        except VoiceError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"configured": True, "usage": usage,
                             "note": None if usage else "Ключ не может читать расход (нет разрешения user_read)."})
