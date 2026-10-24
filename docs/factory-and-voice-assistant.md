# Factory orders and the voice assistant

Russian-first workflow for factory Excel/PDF files, exact pricing, customer documents, and an assistant
you can speak to. Screens: **Заказы** `/board`, **Фабрики** `/factories`, **Клиенты** `/pricing`,
**Документы** `/documents`, **Ассистент** `/assistant`, **Настройки › Голос** `/settings/voice`.

## Money rules (app/settlement.py, app/factory_ledger.py)

- Percentages are stored as fractions (0.30). All money is `Decimal`, rounded per policy: `cent` (half-up)
  or `euro_up` (`ROUNDUP(x, 0)`, as in some supplier files).
- **Factory price** = Σ portion base × (1 + factory rate). Portions are either a section (items whose
  `pricing_section` matches, e.g. RTW 23 % / bags 25 %) or a share of the remaining base
  ("half at 25 %, half at 30 %": 1000 → 1275). Shares are split to the cent (largest remainder).
- **Customer price** = base × (1 + customer markup), independent of the factory rate. A manual unit price
  wins over every rule. Rule precedence: order › brand + season › season › brand › default. The owner's own
  items are always at factory price.
- **Deposit**: factory deposit = factory total × deposit rate; each customer's deposit = their total × rate;
  the owner pays the difference (negative = customers overfund). Planned and confirmed payments are kept
  apart and are never counted twice.
- Acceptance example: AA 20 / IDS 20 / SS 30 / owner 30, factory 30 %, customers 35 %, deposit 50 %
  → factory deposit 130, customers 65, external deposits 47.25, owner net 17.75.

## Changing data safely (app/ledger_actions.py)

Every change, from a grid cell, a form or the assistant, is a typed action. `preview_actions` applies
it in a throw-away transaction and returns "было / станет"; `commit_actions` applies the same code in one
transaction, writes the audit log and a settlement snapshot (History compares any two dates). The assistant
has no tool that commits; only "Подтвердить" (or an allowed spoken "да", below) does.

## Selecting exactly what was asked (app/selection.py)

Each `OrderItem` is one customer allocation with a permanent id; `allocation_group_id` links the allocations
of one factory row. Filters: season, brand/collection, customers (codes and aliases only, never similar
names), exclude customers, item ids (rows ticked in the result panel or on the board), status (ready /
not ready / cancelled / shipped / exact status), category (from product-name keywords), size, colour,
article, price range, markup, source sheet and rows, unpaid. Every answer states the scope:
"Выбрано: SS27 → Dior cruise → IDS → 3 позиции".

- "Dior cruise" beats "Dior". A bare "Dior" picks the plain "Dior" order; if only collections exist
  (DIOR main, Dior pre, Dior cruise… as in the real SS27 file) the assistant asks which one.
- An unknown name ("для Кристины") is a question, not "everyone".
- Follow-ups keep the context: "Открой Dior Cruise" → "Покажи IDS" → "Поставь ей 35%" → "А сколько теперь
  предоплата?" → "Сделай PDF" → "И Excel тоже" → "Теперь сделай это для AA, но с 30%".

## Documents (app/customer_documents.py, app/selective_export.py)

- **Customer** (one Excel + PDF per customer): only their selected lines, photos, article, colour, size,
  quantity, customer prices and deposit. If the selection is the customer's whole order, payments and balance
  are included; for a partial selection ("только готовые", three ticked rows) only the totals of those lines.
  Each file is scanned (all XML parts, PDF text and metadata) for other customers' codes, names, aliases and
  factory wording, and is refused if anything leaks.
- **Internal** report: every selected allocation with customer codes, base and customer prices, and the
  factory figures per brand. Marked "ВНУТРЕННИЙ ОТЧЁТ — не для клиентов".
- **Original with my prices**: the stored factory file, unchanged except two added columns
  ("Клиент · цена", "Клиент · сумма") on the selected rows. Photos, formulas, formatting and other sheets stay.
- Several files → "Скачать всё (ZIP)". Files are previews; nothing is sent to anyone.

## Voice (app/voice.py, app/static/copilot.js)

Mac microphone → browser (AudioWorklet, 16 kHz PCM) → ElevenLabs **Scribe v2 Realtime** over a WebSocket
authorised by a server-issued **single-use token** (15 min, used once) → transcript normalised
("ай ди эс" → IDS, "тридцать пять процентов" → 35 %, "сорокового размера" → 40, "диор круиз" → Dior cruise;
raw text kept in the conversation log) → the **same** `agent.handle` as typed chat → reply → short spoken
summary (`to_speech`: no code, JSON, formulas, links or tables; amounts copied from the calculation) →
`/api/voice/speech/{id}` streams MP3 from ElevenLabs TTS (**eleven_v4_turbo**, fallback **eleven_flash_v2_5**).

- **Push-to-talk**: hold the microphone (or tap, speak, tap). **Без рук**: explicit switch, red indicator,
  pauses send the phrase, "Стоп" or Esc ends it. Speaking over the assistant stops its voice (barge-in);
  its own voice is not re-transcribed.
- Spoken payments and anything with "оплатил/поступил" + amount always land in the text box for checking
  first; "Показывать распознанный текст перед отправкой" does this for everything.
- **Confirmation by voice** ("да, сохрани") confirms only the newest pending proposal of the same
  conversation, in the version shown; if the numbers changed meanwhile, the new figures are shown and asked
  again. Allowed by voice: markups, statuses, item edits, undo, documents. **Payments, credits, factory
  terms, moving items and owner changes need the button.**
- "Нет, у AA оставь 25" rebuilds the proposal with that change. "Сначала покажи расчёт" shows the figures.
  "Отмени последнее изменение" prepares an exact restore of the last saved change (markups, prices,
  statuses, factory terms) as a new proposal.
- Recordings are not stored: microphone audio goes from the browser to ElevenLabs; an uploaded voice note
  is transcribed in memory and discarded.

### Setting up voice on a Mac

1. Start the app (`bash scripts/run.sh`, or `PORT=8003 bash scripts/run.sh`) and open it at
   **http://127.0.0.1:PORT**: microphones need 127.0.0.1/localhost or https.
2. **Настройки › Голос** (`/settings/voice`): paste the ElevenLabs key (starts with `sk_`; the 64-character
   "key ID" does not work), choose a voice and model, press **Проверить и сохранить**. The test fetches a real
   transcription token and synthesises one word. Alternatively set `ELEVENLABS_API_KEY` (optional
   `ELEVENLABS_VOICE_ID`, `ELEVENLABS_TTS_MODEL`) in `.env`; a key saved in the app wins.
3. The key needs the **Text to Speech** and **Speech to Text** permissions. `voices_read` / `user_read` are
   optional (account voice list and character usage).
4. First use: the browser asks for the microphone. If it was refused: Safari › Settings for this website ›
   Microphone, or Chrome › the icon left of the address › Microphone; and macOS System Settings › Privacy &
   Security › Microphone › enable the browser.

The key is stored with `secret_store` (AES-GCM, `data/private`, file mode 600, excluded from backups and Git)
and never sent to the browser. Without a key everything else works; voice controls explain how to connect.

## Fashion AI bar (every page) — app/copilot.py, app/static/copilot.js, app/templates/_copilot.html

The assistant lives in a bar under the navigation on every page; the conversation opens in a dock on the
right (a bottom sheet on narrow screens). The page moves aside instead of being covered; the dock width can be
dragged. The conversation id is kept in the browser, so the dialogue continues across pages; ⟲ lists past
dialogues, ＋ starts a new one. ⌘J / Ctrl+J opens the dock, ⌘⇧M talks, Esc stops listening/speaking/thinking.

- **What it sees.** Each page publishes its state as `data-fx-ctx` (page, season, brand, customer, status,
  rows shown, totals, workbook + sheet, import under review); the bar adds ticked rows and the pending proposal.
  The chip in the bar shows it ("Заказы · SS27 · Dior cruise · выделено 1"); "Что сейчас открыто?" reads it back.
  When the page changed since the last message (`fresh`), "здесь / это / ей" refer to what is on screen.
- **It acts.** "Открой фабрики", "Открой Dior Cruise", "Покажи все заказы IDS" navigate (with the selection
  shown in the dock); "Открой загрузку файла" opens the file picker; "Измени AA процент на 30", "Посчитай
  предоплаты", "Сделай Excel и PDF", "А теперь то же самое для IDS", "Так же для SS" use the same services as
  the screens. Voice answers finish before the page changes.
- **Files.** Drop Excel/PDF anywhere (or 📎): it is stored and read like Загрузка, then the assistant says
  what it found and asks "добавить к существующему сезону или создать новый?". "Добавь к существующему и
  проверь изменения" compares with the ledger (app/revision.py): new orders, new / changed (price, quantity)
  / cancelled / missing allocations, duplicates, unclear rows, unknown customer codes, factory-total
  mismatches. The merge is one proposal and is saved only by the button (never by voice); changed rows go
  through `ledger_actions` (audited), new ones through `commit_import` restricted to the new units; agreed
  factory terms are not touched. A request that continues after the import ("…раздели, поставь IDS 35%,
  сделай Excel и PDF для каждой") resumes automatically after the confirmation. A PDF is compared with the
  orders by article number.
- **Huge workbooks, exactly** (app/workbook_inspect.py). The first look at a workbook streams every sheet
  once into a SQLite cell index (`data/cache/inspect/`; values and formulas, photo anchors per row, images
  never loaded). Then "Какие листы в файле?", "Покажи строки 20–40 листа Dior cruise", "дальше", "Найди
  711R07 в файле" return exact cell values (no rounding or summarising), 200 rows per page. The real 45 MB
  SS27 file indexes in about 1 s. The AI-model tool `inspect_workbook` uses the same functions.
- **Verifier.** Each row's own "=price*qty" formula wins over misaligned headers (fixed Celine Pre in SS27 and
  VALENTINO in the SS24 report); a factory-total mismatch names the rows the factory left without an amount.
  Grounded hints (rows without a customer, unconfirmed deposits, outdated customer documents, total
  mismatches) appear once under the bar and stay in the dock; × hides one for good.
- **Previews.** `/documents/{id}/view` shows a PDF inline and an Excel file as a table with its embedded photos.
- **Other Mac windows (optional).** ⚙ in the dock → "Показать окно Mac ассистенту…" uses the browser's
  screen-share picker (macOS asks which window). A red line shows while it is shared; "Прекратить показ" ends
  it. A still frame is sent only when you ask about the window; it is not stored. Reading it needs a
  vision-capable Claude key in Настройки › ИИ — without one the assistant says so instead of guessing.

## Uploading files (what goes where)

| File | Where | What happens |
|---|---|---|
| **Factory order file** (one workbook, a sheet per brand, photos, prices, "AA 40, Alina 38" allocations) — e.g. `ORDER__SS27_Alina__Factroy!!!_ (2).xlsx` | Drag onto the Fashion AI bar on any page, or 📎 in the bar / in the dialog | Read in a few seconds (sheets, photos, customers, totals). Into an empty app it creates the season, the orders and the customers (ALINA as owner; «владелица не ALINA» changes that; Cyrillic/Latin spellings of one name become one customer). Saved only after **Подтвердить**. |
| **A newer version from the factory** (same season) | Same: drag onto the bar | Compared with what is saved: new / changed / cancelled / missing rows, duplicates, rows without a customer. Say «Добавь к существующему, но сначала покажи изменения»; nothing is written before the button. The identical file again is recognised immediately («тот же файл, что уже загружен»). |
| **Factory PDF** (proforma, invoice) | Drag onto the bar | Article codes are read and matched with the orders. |
| **Customer reports from another season** (e.g. the SS24 «Отчет_ ALINA SS24 …» files) | Drag onto the bar | Read and shown; the assistant asks before creating that season. This layout is not a factory order: expect total mismatches and unclear rows to review in «Проверка импорта». |
| Any workbook, to look inside | Drag onto the bar, then «Какие листы в файле?», «Покажи строки 1–40 листа …» | Exact values and formulas, with the row's photos. |

The import review page with every row and photo is `/factory-import/<token>` («Открой проверку импорта»).

## Replies, edits and visuals

- Replies render Markdown (tables, lists, bold). Under each reply: the steps the assistant actually took (from its tool calls), and cards built from those results — figures, deposit bars, rows with photos, files with a strip of the Excel's photos.
- A change card shows Было / Станет / Разница; the percentage can be changed in the card (− / + / type) and «Пересчитать» sends the same correction as saying «Нет, AA 30%».
- Answers always appear at the bottom; a message sent while the assistant works waits in line.
- With a Claude key, navigation, markup changes, corrections, file follow-ups and the deposit table still use the exact rule handlers (≈0.2 s); everything else goes to Claude. If Claude fails, the rules answer and say so.
- Rows coloured in the factory file keep that colour on the board (name/article cell or a whole coloured row; size-grid shading is ignored).

## Data checks the assistant can run

"Проверь суммы — что не совпадает?": file total vs the app's total per order, rows whose line total in the
file differs from price × quantity, possible duplicates (same article, size and customer on different rows),
rows without a customer, sizes that are not in the row's allocation text. "Почему здесь зелёное?" reports the
row's fill colour from the factory file and its usual meaning as an *interpretation* (SS24 reports use green
#92D050, yellow #FFFF00, light green #C5E0B4; SS27 uses grey #C0C0C0), separately from the status recorded
in the app. "Сравни фото" gives an image-similarity estimate, labelled as an estimate.
