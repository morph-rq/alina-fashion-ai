/* Fashion AI — the command bar on every page and its dock (conversation + results).
 *
 * One assistant for the whole app: what you type, say or drop goes to /api/agent/* together with the
 * structured state of the open page (page, season, brand, customer, workbook/sheet, selected rows,
 * pending proposal). The conversation id lives in localStorage, so the dialogue continues across pages.
 * Voice: ElevenLabs Scribe v2 Realtime (single-use token from the server) in, streamed TTS out; the
 * permanent key never reaches the browser. Typing always works without voice. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const eur = (v) => `€ ${Number(v).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const media = (p) => (p && p.includes('data/images/') ? `/media/${p.split('data/images/')[1]}` : '');
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) })
    .then(async (r) => ({ status: r.status, data: await r.json().catch(() => ({})) }));
  const local = { get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : JSON.parse(v); } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } } };
  const session = { get(k) { try { return JSON.parse(sessionStorage.getItem(k) || 'null'); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } } };
  const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(8)), (b) => b.toString(16).padStart(2, '0')).join('');

  const bar = $('#copilot');
  if (!bar) return;
  const config = JSON.parse(bar.dataset.config || '{}');
  const voiceConfig = config.voice || {};
  const form = $('#copilot-form'); const input = $('#copilot-input'); const dock = $('#copilot-dock'); const list = $('#copilot-messages');
  const statusText = $('#copilot-status-text'); const note = $('#copilot-note'); const live = $('#copilot-live');
  const mic = $('#copilot-mic'); const stopBtn = $('#copilot-stop'); const expand = $('#copilot-expand');
  const handsFree = $('#copilot-handsfree'); const speakToggle = $('#copilot-speak'); const reviewToggle = $('#copilot-review');
  const volume = $('#copilot-volume'); const suggest = $('#copilot-suggest'); const fileInput = $('#copilot-file');
  const slot = $('#copilot-slot');            // the Ассистент page shows the dock inline
  let conversation = (slot && /^[a-f0-9]{16}$/.test(slot.dataset.conversation || '') && slot.dataset.conversation) || local.get('copilot-conversation', '') || '';
  if (!/^[a-f0-9]{16}$/.test(conversation)) conversation = newId();
  local.set('copilot-conversation', conversation);
  let busy = false; let loaded = false; let panelPick = new Set(); let abort = null;

  /* ---------- State indicator ---------- */
  const STATES = { idle: '', listening: 'Слушаю…', connecting: 'Подключаю микрофон…', thinking: 'Думаю…', working: 'Работаю…', speaking: 'Говорю…', reading: 'Читаю файл…' };
  function setState(state) {
    bar.dataset.state = state; statusText.textContent = STATES[state] || '';
    stopBtn.hidden = !['listening', 'connecting', 'thinking', 'working', 'speaking', 'reading'].includes(state) && !handsFree.checked;
  }
  const say = (message, tone = '') => { note.hidden = !message; note.textContent = message || ''; note.dataset.tone = tone; };

  /* ---------- What the assistant sees: structured page state ---------- */
  function boardPick() {
    const pick = session.get('fx-board-selection');
    return pick && pick.ids && pick.ids.length && Date.now() - pick.at < 4 * 3600e3 ? pick : null;
  }
  function screen() {
    const q = new URLSearchParams(location.search);
    const s = { area: document.body.dataset.area, path: location.pathname, title: ($('main h1')?.textContent || document.title).trim().slice(0, 120) };
    ['season', 'brand', 'customer', 'status', 'q'].forEach((k) => { if (q.get(k)) s[k] = q.get(k); });
    $$('[data-fx-ctx]').forEach((el) => { try { Object.assign(s, JSON.parse(el.dataset.fxCtx)); } catch { /* bad json */ } });
    const gridIds = $$('#fx-grid [data-select]:checked').map((c) => Number(c.value));
    const ids = panelPick.size ? [...panelPick] : gridIds.length ? gridIds : (boardPick()?.ids || []);
    if (ids.length) s.selected_item_ids = ids.slice(0, 500);
    const cell = document.activeElement?.closest?.('.wb-grid td');
    if (s.workbook && cell) { const row = cell.closest('tr'); s.workbook.cell = `${$$('th,td', row).indexOf(cell)}:${row?.querySelector('.wb-row-head')?.textContent.trim() || ''}`; }
    const card = $$('.fx-proposal[data-status="pending"]', list).pop();
    if (card) s.proposal = { id: card.dataset.proposal, version: Number(card.dataset.version || 1) };
    const key = JSON.stringify([s.path, s.season, s.brand, s.customer, s.status, s.workbook?.source, s.workbook?.sheet]);
    if (local.get('copilot-last-screen', '') !== key) s.fresh = true;
    s._key = key;
    return s;
  }
  function contextLabel(s) {
    const names = { overview: 'Обзор', workspace: 'Заказы', factories: 'Фабрики', customers: 'Клиенты', inbox: 'Документы', assistant: 'Ассистент', settings: 'Настройки' };
    const parts = [names[s.area] || s.title];
    if (s.workbook) parts.push(s.workbook.file, s.workbook.sheet);
    [s.season, s.brand, s.customer].forEach((v) => { if (v) parts.push(v); });
    if (s.selected_item_ids?.length) parts.push(`выделено ${s.selected_item_ids.length}`);
    return parts.filter(Boolean).join(' · ');
  }
  const refreshContext = () => { $('[data-ctx-text]', bar).textContent = contextLabel(screen()); };
  document.addEventListener('change', (e) => { if (e.target.matches('[data-select], [data-select-all], [data-pick]')) refreshContext(); });

  /* ---------- Dock ---------- */
  const narrow = () => window.matchMedia('(max-width: 1100px)').matches;
  function openDock(focus = false) {
    if (dock.hidden) { dock.hidden = false; document.body.classList.add('copilot-open'); expand.setAttribute('aria-expanded', 'true'); local.set('copilot-open', true); }
    $('.copilot-badge', expand).hidden = true;
    if (!loaded) loadConversation();
    if (focus) input.focus();
  }
  function closeDock() {
    if (slot) return;
    dock.hidden = true; document.body.classList.remove('copilot-open'); expand.setAttribute('aria-expanded', 'false'); local.set('copilot-open', false);
  }
  expand.addEventListener('click', () => (dock.hidden ? openDock(true) : closeDock()));
  $('[data-dock-close]', dock).addEventListener('click', closeDock);
  const width = local.get('copilot-width', 420);
  document.documentElement.style.setProperty('--dock-w', `${Math.min(Math.max(width, 340), 760)}px`);
  $('[data-resize]', dock).addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const move = (m) => { const w = Math.min(Math.max(window.innerWidth - m.clientX, 340), Math.min(760, window.innerWidth * 0.6)); document.documentElement.style.setProperty('--dock-w', `${w}px`); local.set('copilot-width', Math.round(w)); };
    const up = () => { window.removeEventListener('pointermove', move); window.removeEventListener('pointerup', up); };
    window.addEventListener('pointermove', move); window.addEventListener('pointerup', up);
  });
  if (slot) { slot.appendChild(dock); dock.classList.add('is-inline'); dock.hidden = false; loadConversation(); }
  else if (local.get('copilot-open', false) && !narrow()) openDock();

  /* ---------- Messages ---------- */
  const bubble = (cls, html) => { const div = document.createElement('div'); div.className = cls; div.innerHTML = html; list.appendChild(div); div.scrollIntoView({ block: 'end', behavior: 'smooth' }); return div; };
  const textHtml = (t) => esc(t).replace(/\n/g, '<br>');
  function diffTable(diff) {
    if (!diff || !diff.length) return '';
    return `<table class="table fx-diff"><thead><tr><th></th><th class="num">Было</th><th class="num">Станет</th></tr></thead><tbody>${
      diff.map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${eur(r.before)}</td><td class="num"><strong>${eur(r.after)}</strong></td></tr>`).join('')}</tbody></table>`;
  }
  function proposalCard(p) {
    const existing = $(`[data-proposal="${CSS.escape(p.id)}"]`, list);
    const docs = (p.result?.documents || []).map((d) => `<a href="${esc(d.url)}" download>${esc(String(d.format).toUpperCase())} ${esc(d.customer || '')} v${esc(d.version)}</a>`).join(' ');
    const say_ = p.button_only ? '<span class="muted fx-say">только кнопкой</span>' : '<span class="muted fx-say">или скажите «Да, сохрани»</span>';
    const html = `<strong>${esc(p.summary)}</strong><ul>${(p.will_do || []).map((l) => `<li>${esc(l)}</li>`).join('')}${(p.documents || []).map((d) => `<li>Excel + PDF для ${esc(d.customer)} (${esc(d.season)})</li>`).join('')}</ul>${diffTable(p.diff)}`
      + (p.status === 'pending' ? `<div class="btn-row"><button class="btn primary sm" data-commit>Подтвердить</button><button class="btn quiet sm" data-cancel>Отмена</button>${say_}</div>`
        : p.status === 'committed' ? `<p class="tone-ok">Сохранено. ${docs}</p>` : '<p class="muted">Отменено — ничего не изменено.</p>');
    if (p.status === 'pending') {
      $$('.fx-proposal[data-status="pending"]', list).forEach((old) => { if (old.dataset.proposal !== p.id) { old.dataset.status = 'cancelled'; const row = $('.btn-row', old); if (row) row.outerHTML = '<p class="muted">Заменено новым предложением.</p>'; } });
    }
    const card = existing || bubble('fx-proposal', '');
    card.innerHTML = html; card.dataset.proposal = p.id; card.dataset.version = p.version || 1; card.dataset.status = p.status;
    return card;
  }
  function question(q) {
    if (!q || !q.options?.length) return;
    bubble('copilot-options', q.options.map((o) => `<button class="btn sm" type="button" data-option>${esc(o)}</button>`).join(' '));
  }
  function renderMessage(m) {
    if (m.role === 'user') bubble('user-bubble', textHtml(m.text) + (m.raw && m.raw !== m.text ? `<small class="fx-raw">Распознано: «${esc(m.raw)}»</small>` : ''));
    else { bubble('assistant-bubble', textHtml(m.text)); if (m.proposal) proposalCard(m.proposal); }
  }
  async function loadConversation() {
    loaded = true;
    list.innerHTML = '';
    const r = await fetch(`/api/agent/conversations/${conversation}`).then((x) => x.json()).catch(() => ({ messages: [] }));
    if (!r.messages?.length) {
      bubble('assistant-bubble copilot-welcome', 'Здравствуйте! Я вижу, что открыто на экране, и могу всё сделать за вас: «Открой Dior Cruise», «Покажи все заказы IDS», «Измени AA процент на 30», «Посчитай предоплаты», «Сделай Excel и PDF». Перетащите сюда файл фабрики — я его прочитаю и сравню с заказами.');
    }
    (r.messages || []).forEach(renderMessage);
    const lastQ = [...(r.messages || [])].reverse().find((m) => m.role === 'assistant');
    if (lastQ?.question) question(lastQ.question);
    if (r.panel) renderPanel(r.panel);
    chips();
  }

  list.addEventListener('click', async (e) => {
    if (e.target.matches('[data-option]')) { send(e.target.textContent); return; }
    if (e.target.matches('[data-ask]')) { send(e.target.dataset.ask); return; }
    const pager = e.target.closest('[data-inspect-next]');
    if (pager) { send(pager.dataset.inspectNext); return; }
    const card = e.target.closest('[data-proposal]');
    if (!card) return;
    if (e.target.matches('[data-commit]')) {
      e.target.disabled = true; e.target.textContent = 'Сохраняю…'; setState('working');
      const r = await post(`/api/agent/proposals/${card.dataset.proposal}/commit`, {});
      setState('idle');
      const stale = r.data.already && r.data.already !== 'committed';
      card.dataset.status = r.data.ok && !stale ? 'committed' : card.dataset.status;
      $('.btn-row', card).outerHTML = stale ? '<p class="muted">Это предложение уже неактуально — ничего не изменено.</p>'
        : r.data.ok ? `<p class="tone-ok">Сохранено. ${(r.data.messages || []).map(esc).join(' ')} ${(r.data.documents || []).map((d) => `<a href="${esc(d.url)}" download>${esc(String(d.format).toUpperCase())} ${esc(d.customer || '')} v${esc(d.version)}</a>`).join(' ')}</p>`
          : `<p class="tone-danger">${esc(r.data.error)}</p>`;
      if (r.data.ok && r.data.documents?.length) renderPanel({ documents: r.data.documents.map((d) => ({ ...d, kind: 'customer' })), bundle: r.data.documents.length > 1 ? `/documents/bundle?ids=${r.data.documents.map((d) => d.id).join(',')}` : null });
      if (r.data.ok) afterCommit(r.data);
    }
    if (e.target.matches('[data-cancel]')) { await post(`/api/agent/proposals/${card.dataset.proposal}/cancel`, {}); card.dataset.status = 'cancelled'; $('.btn-row', card).outerHTML = '<p class="muted">Отменено — ничего не изменено.</p>'; }
  });
  function afterCommit(data) {
    if (data.follow_up) { session.set('copilot-follow-up', { text: data.follow_up, at: Date.now() }); }
    if (data.navigate && !slot) { location.href = data.navigate; return; }
    if (data.follow_up) { session.set('copilot-follow-up', null); send(data.follow_up, { note: 'Продолжаю: ' }); return; }
    if (!slot) setTimeout(() => location.reload(), 900);       // the page under the dock shows the new figures
  }

  /* ---------- Sending ---------- */
  async function send(message, { channel = 'text', raw = null, note: prefix = '' } = {}) {
    message = (message || '').trim();
    if (!message || busy) return;
    busy = true; openDock(); hideSuggest();
    voice.stopSpeaking();
    bubble('user-bubble', (prefix ? `<small class="muted">${esc(prefix)}</small>` : '') + textHtml(message) + (raw && raw !== message ? `<small class="fx-raw">Распознано: «${esc(raw)}»</small>` : ''));
    const wait = bubble('assistant-bubble is-thinking', '<span class="dots"><i></i><i></i><i></i></span>');
    setState('thinking');
    const s = screen();
    const aboutWindow = /посмотри на (это|этот|мой|окно)|в этом окне|это окно|который я открыла|на mac|на маке|что здесь написано/i.test(message);
    if (aboutWindow && share.stream) { busy = false; wait.remove(); setState('idle'); share.ask(message); return; }
    if (aboutWindow && !/fashion|заказ|страниц/i.test(message)) {
      busy = false; setState('idle');
      wait.className = 'assistant-bubble';
      wait.innerHTML = 'Другие окна Mac я не вижу, пока вы сами их не покажете: ⚙ в этом диалоге → «Показать окно Mac ассистенту…» (macOS спросит, какое окно). Всё, что открыто в Fashion, я вижу и так. Файл Excel или PDF можно просто перетащить сюда.';
      return;
    }
    let r;
    abort = new AbortController();
    try {
      const key = s._key; delete s._key;
      const resp = await fetch('/api/agent/message', { method: 'POST', signal: abort.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation, text: message, raw, channel, screen: s }) });
      r = { data: await resp.json().catch(() => ({ reply: 'Ошибка ответа.' })) };
      local.set('copilot-last-screen', key);
    } catch (err) {
      r = { data: { reply: err.name === 'AbortError' ? 'Остановлено.' : 'Нет связи с приложением. Проверьте, что оно запущено.' } };
    }
    abort = null; busy = false; setState('idle');
    wait.className = 'assistant-bubble'; wait.innerHTML = textHtml(r.data.reply || r.data.detail || 'Ошибка');
    question(r.data.question);
    if (r.data.proposal) proposalCard(r.data.proposal);
    if (r.data.panel) renderPanel(r.data.panel);
    if (dock.hidden || narrow()) { const b = $('.copilot-badge', expand); b.hidden = false; b.textContent = '1'; }
    const spoken = (channel === 'voice' || handsFree.checked) && speakToggle.checked && r.data.speech ? voice.speak(r.data.speech) : null;
    const client = r.data.client || {};
    if (client.open_upload) fileInput.click();
    if (client.follow_up) send(client.follow_up, { note: 'Продолжаю: ' });
    chips();
    if (client.navigate) {
      if (spoken) { say('Открываю, когда договорю… (Esc — сразу)'); await spoken; await voice.untilQuiet(15000); say(''); }
      navigate(client.navigate);
    }
  }
  function navigate(url) {
    if (slot) { window.open(url, '_self'); return; }
    if (location.pathname + location.search === url) return;
    session.set('copilot-nav', { at: Date.now() });
    location.href = url;          // the dock re-opens on the next page with the same conversation
  }
  form.addEventListener('submit', (e) => { e.preventDefault(); const raw = input.dataset.raw || null; const message = input.value; input.value = ''; delete input.dataset.raw; send(message, { channel: raw ? 'voice' : 'text', raw }); });
  stopBtn.addEventListener('click', stopAll);
  function stopAll() { handsFree.checked = false; voice.stopSpeaking(); voice.close(); abort?.abort(); setState('idle'); say(''); }

  /* ---------- Suggestions (contextual, under the bar) ---------- */
  function suggestions(s) {
    const by = {
      workspace: s.brand ? [`Сколько каждая должна заплатить?`, 'Проверь суммы — что не совпадает?', 'Сделай Excel и PDF для каждой', 'Что сейчас открыто?']
        : ['Открой Dior Cruise', 'Покажи все вещи, которые ещё не готовы', 'Посчитай предоплаты'],
      factories: ['Сколько мы должны фабрике сейчас?', 'Посчитай предоплаты', 'Проверь суммы — что не совпадает?'],
      customers: ['Посчитай предоплаты', 'Измени AA процент на 30', 'Покажи все заказы IDS'],
      inbox: ['Открой загрузку файла', 'Сделай Excel и PDF для IDS', 'Какие листы в файле?'],
      overview: ['Открой Dior Cruise', 'Посчитай предоплаты', 'Что изменилось за неделю?'],
    };
    const list_ = (s.workbook ? ['Какие листы в файле?', `Покажи строки 1–40 листа ${s.workbook.sheet || ''}`.trim(), 'Проверь суммы — что не совпадает?'] : by[s.area])
      || ['Что сейчас открыто?', 'Открой Dior Cruise', 'Открой загрузку файла'];
    if (s.selected_item_ids?.length) list_.unshift(s.selected_item_ids.length === 1 ? 'А это сколько стоит?' : 'Сделай из этого PDF');
    return list_.slice(0, 4);
  }
  function chips() {
    $('#copilot-chips').innerHTML = suggestions(screen()).map((t) => `<button type="button" class="chip-btn" data-ask="${esc(t)}">${esc(t)}</button>`).join('');
  }
  $('#copilot-chips').addEventListener('click', (e) => { if (e.target.matches('[data-ask]')) send(e.target.dataset.ask); });
  function showSuggest() {
    if (input.value.trim()) { hideSuggest(); return; }
    suggest.innerHTML = suggestions(screen()).map((t) => `<button type="button" role="option" data-ask="${esc(t)}">${esc(t)}</button>`).join('')
      + `<button type="button" role="option" class="muted" data-open-dock>Открыть диалог и историю →</button>`;
    suggest.hidden = false;
  }
  const hideSuggest = () => { suggest.hidden = true; };
  input.addEventListener('focus', showSuggest);
  input.addEventListener('input', showSuggest);
  input.addEventListener('blur', () => setTimeout(hideSuggest, 150));
  suggest.addEventListener('mousedown', (e) => e.preventDefault());
  suggest.addEventListener('click', (e) => { if (e.target.matches('[data-ask]')) send(e.target.dataset.ask); if (e.target.matches('[data-open-dock]')) { hideSuggest(); openDock(true); } });
  $('#copilot-ctx').addEventListener('click', () => send('Что сейчас открыто?'));
  bar.addEventListener('click', (e) => { if (e.target === bar || e.target.classList.contains('copilot-inner') || e.target === form) input.focus(); });

  /* ---------- Result panel blocks (inside the conversation) ---------- */
  function renderPanel(p) {
    const parts = [];
    if (p.attachment) {
      const a = p.attachment;
      parts.push(`<section class="fx-side-block"><h3>📎 ${esc(a.name)}</h3>${a.sheets ? `<table class="table compact"><thead><tr><th>Заказ</th><th class="num">Поз.</th><th class="num">Фото</th><th class="num">Неясно</th><th></th></tr></thead><tbody>${
        a.sheets.map((s) => `<tr><td>${esc(s.brand)}</td><td class="num">${s.items}</td><td class="num">${s.photos}</td><td class="num">${s.unclear || ''}</td><td>${s.exists ? '<span class="muted">уже есть</span>' : 'новый'}</td></tr>`).join('')}</tbody></table>` : ''}${
        a.review ? `<a class="btn sm quiet" href="${esc(a.review)}">Открыть проверку импорта</a>` : ''}</section>`);
    }
    if (p.revision) {
      const r = p.revision;
      const group = (title, rows, fmt) => (rows?.length ? `<details ${rows.length < 8 ? 'open' : ''}><summary>${esc(title)} · ${rows.length}</summary><ul class="plain-list">${rows.map(fmt).join('')}</ul></details>` : '');
      parts.push(`<section class="fx-side-block fx-revision"><h3>Сравнение с ${esc(r.season)}</h3>
        ${group('Новые заказы', r.new_orders, (o) => `<li>${esc(o.brand)} — ${o.items} поз., база ${eur(o.base)}</li>`)}
        ${group('Новые позиции', r.new, (o) => `<li>${esc(o.label)} · ${o.quantity} шт. <small class="muted">${esc(o.rows)}</small></li>`)}
        ${group('Изменились', r.changed, (o) => `<li>${esc(o.label)}: ${o.changes.map((c) => `${c.field === 'price' ? 'цена' : 'кол-во'} ${esc(c.before)} → <b>${esc(c.after)}</b>`).join(', ')}</li>`)}
        ${group('Отменены в файле', r.cancelled, (o) => `<li>${esc(o.label)} <small class="muted">${esc(o.rows)}</small></li>`)}
        ${group('Нет в новом файле', r.missing, (o) => `<li>${esc(o.label)} · ${o.quantity} шт.</li>`)}
        ${group('Дубли в файле', r.duplicates, (o) => `<li>${esc(o.label)} <small class="muted">${esc(o.rows.join(', '))}</small></li>`)}
        ${group('Без клиента / неясно', r.unresolved, (o) => `<li>${esc(o.sheet.trim())} стр. ${o.row}: ${esc(o.label)} <small class="muted">«${esc(o.text || '—')}»</small></li>`)}
        ${r.unknown_customers?.length ? `<p class="muted">Новые обозначения клиентов: ${r.unknown_customers.map(esc).join(', ')}</p>` : ''}
        ${(r.warnings || []).map((w) => `<p class="tone-warn">⚠ ${esc(w)}</p>`).join('')}
        <a class="btn sm quiet" href="${esc(r.review)}">Проверить каждую строку с фото</a></section>`);
    }
    if (p.figures?.length) {
      parts.push(`<section class="fx-side-block"><h3>Расчёт</h3><dl class="fx-figs">${p.figures.map((f) => `<div><dt>${esc(f.label)}</dt><dd>${eur(f.value)}</dd></div>`).join('')}</dl><p class="muted">Из расчёта приложения (те же формулы, что в таблицах).</p></section>`);
    }
    if (p.bars?.length) {
      const max = Math.max(...p.bars.map((b) => Number(b.value))) || 1;
      parts.push(`<section class="fx-side-block"><h3>Предоплата и оплачено</h3><div class="fx-bars">${p.bars.map((b) => `<div class="fx-bar-row"><span>${esc(b.label)}</span><span class="fx-bar"><i style="width:${(100 * Number(b.value) / max).toFixed(1)}%"></i><b style="width:${(100 * Math.min(Number(b.paid), Number(b.value)) / max).toFixed(1)}%"></b></span><span class="num">${eur(b.value)}</span></div>`).join('')}</div><p class="muted">Светлое — предоплата, тёмное — подтверждённая оплата.</p></section>`);
    }
    if (p.table) {
      const t = p.table; const numeric = new Set(t.numeric || []);
      parts.push(`<section class="fx-side-block"><h3>${esc(t.title)}</h3><div class="fx-table-wrap${t.grid ? ' is-grid' : ''}"><table class="table compact"><thead><tr>${t.columns.map((c, i) => `<th${numeric.has(i) ? ' class="num"' : ''}>${esc(c)}</th>`).join('')}</tr></thead><tbody>${
        t.rows.map((row) => `<tr>${row.map((v, i) => `<td${numeric.has(i) ? ' class="num"' : ''}>${esc(v)}</td>`).join('')}</tr>`).join('') || `<tr><td colspan="${t.columns.length}" class="muted">Пусто</td></tr>`}</tbody></table></div></section>`);
    }
    if (p.documents?.length) {
      parts.push(`<section class="fx-side-block"><h3>Файлы</h3><ul class="fx-files">${p.documents.map((d) => `<li><a class="btn sm" href="${esc(d.url)}" download>${esc(String(d.format).toUpperCase())}</a> <span>${esc(d.kind === 'internal' ? 'Внутренний отчёт' : d.kind === 'original' ? 'Оригинал с ценами' : d.customer)}${d.version ? ` · v${esc(d.version)}` : ''}</span>${d.format === 'pdf' ? ` <a class="muted" href="${esc(d.url.replace('/download', '/view'))}" target="_blank" rel="noopener">просмотр</a>` : ` <a class="muted" href="${esc(d.url.replace('/download', '/view'))}" target="_blank" rel="noopener">просмотр</a>`}</li>`).join('')}</ul>${p.bundle ? `<a class="btn sm primary" href="${esc(p.bundle)}" download>Скачать всё (ZIP)</a>` : ''}<p class="muted">Предпросмотр — клиентам ничего не отправлено. Все версии — в <a href="/documents">Документах</a>.</p></section>`);
    }
    if (p.item) {
      parts.push(`<section class="fx-side-block"><h3>Позиция</h3>${media(p.item.image) ? `<img class="fx-item-photo" src="${esc(media(p.item.image))}" alt="">` : ''}<ul class="plain-list">${p.item.facts.map((f) => `<li>${esc(f)}</li>`).join('')}</ul></section>`);
    }
    if (p.photos) {
      parts.push(`<section class="fx-side-block"><h3>Сравнение фото</h3><div class="fx-photo-pair">${p.photos.map((x) => `<figure>${media(x.image) ? `<img src="${esc(media(x.image))}" alt="">` : '—'}<figcaption>#${esc(x.id)} ${esc(x.name)}</figcaption></figure>`).join('')}</div></section>`);
    }
    if (p.findings?.length) {
      parts.push(`<section class="fx-side-block"><h3>Проверка</h3><ul class="plain-list">${p.findings.map((f) => `<li>${esc(f)}</li>`).join('')}</ul></section>`);
    }
    if (p.selection) {
      const s = p.selection; panelPick = new Set();
      parts.push(`<section class="fx-side-block"><h3>${esc(s.summary)}</h3>
        <p class="muted">По ценам клиентов ${eur(s.customer_total)} · база ${eur(s.base_total)}. Отметьте строки — тогда «эти» и «выделенные» будут о них.</p>
        <div class="fx-sel">${s.rows.map((r) => `<label class="fx-sel-row"><input type="checkbox" data-pick value="${esc(r.id)}">
          ${media(r.image) ? `<img src="${esc(media(r.image))}" alt="" loading="lazy">` : '<span class="fx-noimg"></span>'}
          <span class="fx-sel-main"><strong>${esc(r.name || r.article)}</strong><small>${esc([r.brand, r.article, r.color].filter(Boolean).join(' · '))}</small></span>
          <span class="fx-sel-meta"><b>${esc(r.customer || '—')}</b><small>р. ${esc(r.size || '—')} · ${esc(r.quantity)} шт.</small></span>
          <span class="fx-sel-sum">${eur(r.total)}<small>${esc(r.status)}</small></span></label>`).join('') || '<p class="muted">Ничего не найдено.</p>'}</div></section>`);
    }
    if (!parts.length) return;
    const box = bubble('copilot-result', parts.join(''));
    $$('[data-pick]', box).forEach((c) => c.addEventListener('change', () => { c.checked ? panelPick.add(Number(c.value)) : panelPick.delete(Number(c.value)); showPick(); }));
    showPick();
  }
  function showPick() {
    const box = $('#copilot-pick'); const pick = boardPick();
    const n = panelPick.size || ($$('#fx-grid [data-select]:checked').length) || pick?.ids.length || 0;
    box.hidden = !n;
    if (n) $('span', box).textContent = panelPick.size ? `Отмечено в результатах: ${n} — «эти» и «выделенные» о них.` : `Выделено в таблице: ${n} поз.`;
    refreshContext();
  }
  $('[data-clear-pick]', dock).addEventListener('click', () => { panelPick.clear(); session.set('fx-board-selection', null); $$('[data-pick]', list).forEach((c) => { c.checked = false; }); showPick(); });

  /* ---------- History ---------- */
  const history_ = $('[data-history]', dock);
  history_.addEventListener('toggle', async () => {
    if (!history_.open) return;
    const r = await fetch('/api/agent/conversations').then((x) => x.json()).catch(() => ({ conversations: [] }));
    $('[data-history-list]', history_).innerHTML = (r.conversations || []).map((c) => `<button type="button" data-conv="${esc(c.id)}" class="${c.id === conversation ? 'is-current' : ''}"><span>${esc(c.title)}</span><small>${c.when ? new Date(c.when).toLocaleString('ru-RU', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : ''} · ${c.messages} сообщ.</small></button>`).join('') || '<p class="muted">Пока нет диалогов.</p>';
  });
  $('[data-history-list]', history_).addEventListener('click', (e) => {
    const b = e.target.closest('[data-conv]'); if (!b) return;
    conversation = b.dataset.conv; local.set('copilot-conversation', conversation); history_.open = false; loadConversation();
  });
  $('[data-new-chat]', dock).addEventListener('click', () => { conversation = newId(); local.set('copilot-conversation', conversation); local.set('copilot-last-screen', ''); loadConversation(); input.focus(); });

  /* ---------- Hints (grounded, at most one shown, dismissals remembered) ---------- */
  (async () => {
    const r = await post('/api/agent/hints', { screen: screen() }).catch(() => ({ data: {} }));
    const dismissed = local.get('copilot-hints-off', []);
    const hint = (r.data.hints || []).find((h) => !dismissed.includes(h.id));
    if (!hint) return;
    const box = $('#copilot-hint'); const inDock = $('#copilot-dock-hint');
    [box, inDock].forEach((el) => { $('[data-hint-text]', el).textContent = hint.text; $('[data-hint-ask]', el).textContent = hint.ask;
      $('[data-hint-ask]', el).onclick = () => { box.hidden = true; inDock.hidden = true; send(hint.ask); }; });
    inDock.hidden = false;
    const seen = session.get('copilot-hints-seen') || [];
    if (!seen.includes(hint.id)) {        // shown once per visit under the bar, then it stays only in the dialog
      box.hidden = false; session.set('copilot-hints-seen', [...seen, hint.id]);
      setTimeout(() => { box.hidden = true; }, 9000);
    }
    $('[data-hint-close]', box).onclick = () => { box.hidden = true; inDock.hidden = true; local.set('copilot-hints-off', [...dismissed, hint.id].slice(-60)); };
  })();

  /* ---------- Files: attach button, drag & drop anywhere ---------- */
  $('#copilot-attach').addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => { if (fileInput.files.length) attach([...fileInput.files]); fileInput.value = ''; });
  const veil = $('#drop-veil'); let depth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes('Files');
  const ownZone = (e) => e.target.closest?.('[data-drop], .image-dropzone, input[type=file]');
  window.addEventListener('dragenter', (e) => { if (!hasFiles(e) || ownZone(e)) return; depth += 1; veil.hidden = false; bar.classList.add('is-drop'); });
  window.addEventListener('dragleave', () => { depth = Math.max(0, depth - 1); if (!depth) { veil.hidden = true; bar.classList.remove('is-drop'); } });
  window.addEventListener('dragover', (e) => { if (hasFiles(e) && !ownZone(e)) e.preventDefault(); });
  window.addEventListener('drop', (e) => {
    depth = 0; veil.hidden = true; bar.classList.remove('is-drop');
    if (!hasFiles(e) || ownZone(e)) return;
    e.preventDefault();
    attach([...e.dataTransfer.files]);
  });
  async function attach(files) {
    const ok = files.filter((f) => /\.(xlsx|xlsm|xls|pdf)$/i.test(f.name));
    if (!ok.length) { say('Поддерживаются Excel (.xlsx, .xlsm, .xls) и PDF.', 'warn'); return; }
    openDock();
    const s = screen();
    for (const file of ok) {
      const card = bubble('user-bubble copilot-file', `📎 <b>${esc(file.name)}</b> <small>${(file.size / 1048576).toFixed(1)} МБ</small><span class="copilot-progress"><i></i></span><small class="muted" data-file-state>Загружаю…</small>`);
      setState('reading');
      const body = new FormData(); body.append('files', file); body.append('conversation', conversation); body.append('season', s.season || '');
      const tokens = await new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/agent/attach');
        xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) $('.copilot-progress i', card).style.width = `${(100 * ev.loaded / ev.total).toFixed(0)}%`; };
        xhr.onload = () => { try { const d = JSON.parse(xhr.responseText); resolve(xhr.status === 200 ? d.tokens : (say(d.detail || 'Не удалось загрузить.', 'danger'), [])); } catch { resolve([]); } };
        xhr.onerror = () => { say('Нет связи с приложением.', 'danger'); resolve([]); };
        xhr.send(body);
      });
      if (!tokens?.length) { $('[data-file-state]', card).textContent = 'Не загружено'; setState('idle'); continue; }
      $('[data-file-state]', card).textContent = 'Читаю: листы, фото, клиентов, итоги…';
      const token = tokens[0];
      for (let i = 0; i < 600; i += 1) {
        const st = await fetch(`/factory-import/${token}/status`).then((x) => x.json()).catch(() => ({}));
        if (!['queued', 'analysing'].includes(st.state)) break;
        $('[data-file-state]', card).textContent = st.message || 'Читаю…';
        await new Promise((r) => setTimeout(r, 700));
      }
      $('[data-file-state]', card).textContent = 'Прочитано';
      $('.copilot-progress', card).remove();
      const r = await post('/api/agent/attachment', { conversation, token });
      setState('idle');
      bubble('assistant-bubble', textHtml(r.data.reply || r.data.detail || 'Ошибка'));
      question(r.data.question);
      if (r.data.panel) renderPanel(r.data.panel);
      if (speakToggle.checked && (handsFree.checked || voice.active) && r.data.speech) voice.speak(r.data.speech);
    }
  }

  /* ---------- Optional: show a Mac window (explicit, visible, stoppable) ---------- */
  const share = {
    stream: null, video: null,
    async start() {
      if (!navigator.mediaDevices?.getDisplayMedia) { say('Этот браузер не умеет показывать окна. Используйте Chrome или Safari 17+.', 'warn'); return; }
      try {
        this.stream = await navigator.mediaDevices.getDisplayMedia({ video: { displaySurface: 'window' }, audio: false });
      } catch (err) {
        say(err.name === 'NotAllowedError' ? 'Показ окна отменён (или macOS не разрешила запись экрана: Системные настройки › Конфиденциальность › Запись экрана).' : 'Не удалось начать показ окна.', 'warn');
        return;
      }
      this.video = document.createElement('video'); this.video.muted = true; this.video.srcObject = this.stream; await this.video.play().catch(() => {});
      const track = this.stream.getVideoTracks()[0];
      const label = /\s/.test(track.label || '') || (track.label || '').length < 40 ? track.label : '';
      $('[data-share-label]', bar).textContent = `Показывается ассистенту: ${label || 'выбранное окно'}`;
      $('#copilot-share').hidden = false; bar.classList.add('is-sharing');
      track.addEventListener('ended', () => this.stop());
    },
    stop() { this.stream?.getTracks().forEach((t) => t.stop()); this.stream = null; this.video = null; $('#copilot-share').hidden = true; bar.classList.remove('is-sharing'); },
    frame() {
      const v = this.video; if (!v || !v.videoWidth) return null;
      const scale = Math.min(1, 1600 / v.videoWidth); const c = document.createElement('canvas');
      c.width = Math.round(v.videoWidth * scale); c.height = Math.round(v.videoHeight * scale);
      c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
      return c.toDataURL('image/jpeg', 0.85);
    },
    async ask(text) {
      const image = this.frame();
      if (!image) { say('Нет изображения окна — начните показ ещё раз.', 'warn'); return; }
      openDock();
      bubble('user-bubble', `🖥 ${textHtml(text)}<img class="copilot-shot" src="${image}" alt="Снимок показанного окна">`);
      setState('thinking');
      const r = await post('/api/agent/screen-frame', { conversation, text, image });
      setState('idle');
      bubble('assistant-bubble', textHtml(r.data.reply || r.data.detail || 'Ошибка'));
      if (speakToggle.checked && r.data.speech && voice.active) voice.speak(r.data.speech);
    },
  };
  $('[data-share-start]', dock).addEventListener('click', () => { $$('.dock-menu', dock).forEach((d) => { d.open = false; }); share.start(); });
  $('[data-share-stop]', bar).addEventListener('click', () => share.stop());
  $('[data-share-frame]', bar).addEventListener('click', () => { const t = input.value.trim() || 'Что на этом экране?'; input.value = ''; share.ask(t); });

  /* ---------- Voice (ElevenLabs Scribe v2 Realtime + streamed TTS) ---------- */
  speakToggle.checked = local.get('fx-speak', voiceConfig.speak_replies !== false);
  speakToggle.addEventListener('change', () => local.set('fx-speak', speakToggle.checked));
  reviewToggle.checked = local.get('fx-review', !!voiceConfig.review_before_send);
  reviewToggle.addEventListener('change', () => local.set('fx-review', reviewToggle.checked));
  volume.value = local.get('fx-volume', 1);
  const WORKLET = `class Pcm16 extends AudioWorkletProcessor {
    constructor() { super(); this.ratio = sampleRate / 16000; this.pos = 0; this.out = new Int16Array(1600); this.n = 0; this.sum = 0; }
    process(inputs) {
      const ch = inputs[0] && inputs[0][0];
      if (!ch) return true;
      for (let i = 0; i < ch.length; i++) {
        this.pos += 1;
        if (this.pos >= this.ratio) { this.pos -= this.ratio; const s = Math.max(-1, Math.min(1, ch[i])); this.out[this.n++] = s < 0 ? s * 0x8000 : s * 0x7fff; this.sum += s * s;
          if (this.n === this.out.length) { this.port.postMessage({ pcm: this.out.buffer.slice(0), level: Math.sqrt(this.sum / this.n) }); this.n = 0; this.sum = 0; } }
      }
      return true;
    }
  }
  registerProcessor('pcm16', Pcm16);`;
  const liveFinal = $('.copilot-live-final', live); const livePartial = $('.copilot-live-partial', live);

  const voice = {
    ws: null, stream: null, ctx: null, node: null, active: false, mode: null, retries: 0, finalText: '', stopping: false,
    audio: new Audio(), speakingText: '', pressedAt: 0, backlog: [], finished: true,
    supported() { return !!(navigator.mediaDevices?.getUserMedia && window.WebSocket && (window.AudioWorkletNode || window.AudioContext)); },
    async start(mode) {
      if (!voiceConfig.configured) { say('Голос не подключён: Настройки › Голос — вставьте ключ ElevenLabs. Пока можно писать.', 'warn'); return; }
      if (!window.isSecureContext) { say('Микрофон работает только на https или http://127.0.0.1 / localhost.', 'warn'); return; }
      if (!this.supported()) { say('Этот браузер не даёт доступ к микрофону. Используйте свежий Safari или Chrome.', 'warn'); return; }
      if (this.active) return;
      this.stopSpeaking();
      this.mode = mode; this.active = true; this.stopping = false; this.finalText = ''; this.retries = 0; this.backlog = []; this.finished = false;
      setState('connecting'); say('');
      live.hidden = false; liveFinal.textContent = ''; livePartial.textContent = 'Слушаю…';
      try { await this.openMic(); await this.connect(); } catch (err) { this.fail(err); }
    },
    async openMic() {
      const deviceId = local.get('fx-input', '');
      try {
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      } catch (err) {
        if (err.name === 'NotAllowedError' || err.name === 'SecurityError') throw new Error('mic-denied');
        if (err.name === 'NotFoundError' || err.name === 'OverconstrainedError') { local.set('fx-input', ''); throw new Error('mic-missing'); }
        throw err;
      }
      fillDevices();
      this.ctx = new (window.AudioContext || window.webkitAudioContext)();
      if (this.ctx.state === 'suspended') await this.ctx.resume();
      const source = this.ctx.createMediaStreamSource(this.stream);
      const url = URL.createObjectURL(new Blob([WORKLET], { type: 'application/javascript' }));
      await this.ctx.audioWorklet.addModule(url);
      URL.revokeObjectURL(url);
      this.node = new AudioWorkletNode(this.ctx, 'pcm16');
      this.node.port.onmessage = (e) => this.chunk(e.data);
      source.connect(this.node);
    },
    async connect() {
      const r = await post(`/api/voice/token?hands_free=${this.mode === 'handsfree'}`, {});
      if (r.status !== 200) throw new Error(r.data.error || 'Не удалось подключиться к ElevenLabs.');
      await new Promise((resolve, reject) => {
        const ws = new WebSocket(r.data.url);
        this.ws = ws;
        const timer = setTimeout(() => reject(new Error('ElevenLabs не ответил. Проверьте интернет.')), 8000);
        ws.onmessage = (e) => {
          const m = JSON.parse(e.data);
          if (m.message_type === 'session_started') {
            clearTimeout(timer); this.retries = 0; setState(this.stopping ? 'thinking' : 'listening');
            this.backlog.splice(0).forEach((frame) => ws.send(frame));
            if (this.stopping) ws.send(JSON.stringify({ message_type: 'input_audio_chunk', audio_base_64: '', commit: true, sample_rate: 16000 }));
            resolve(); return;
          }
          this.message(m);
        };
        ws.onerror = () => { clearTimeout(timer); };
        ws.onclose = (e) => { clearTimeout(timer); if (this.ws !== ws) return; this.ws = null; if (this.active && !this.stopping) this.reconnect(e); else reject(new Error('closed')); };
      });
    },
    async reconnect() {
      if (this.retries >= 3) { this.fail(new Error('Связь с распознаванием прервалась. Попробуйте ещё раз.')); return; }
      this.retries += 1; setState('connecting'); say(`Переподключаюсь… (${this.retries})`);
      await new Promise((r) => setTimeout(r, 500 * this.retries ** 2));
      try { await this.connect(); say(''); } catch (err) { this.fail(err); }
    },
    chunk({ pcm, level }) {
      $$('.copilot-wave i', mic).forEach((b, i) => { b.style.transform = `scaleY(${Math.min(1, 0.15 + level * (6 + i * 1.5))})`; });
      const bytes = new Uint8Array(pcm); let bin = '';
      for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      const frame = JSON.stringify({ message_type: 'input_audio_chunk', audio_base_64: btoa(bin), commit: false, sample_rate: 16000 });
      if (this.ws && this.ws.readyState === 1 && !this.backlog.length) this.ws.send(frame);
      else if (this.backlog.length < 100) this.backlog.push(frame);
    },
    message(m) {
      const type = m.message_type;
      if (type === 'partial_transcript') {
        livePartial.textContent = m.text ? ` ${m.text}` : '';
        if (m.text && m.text.trim().length > 2 && this.isSpeaking() && !this.echo(m.text)) this.stopSpeaking();
      } else if (type === 'committed_transcript' || type === 'committed_transcript_with_timestamps') {
        const heard = (m.text || '').trim();
        livePartial.textContent = '';
        if (!heard || this.echo(heard)) return;
        if (this.mode === 'handsfree') { this.deliver(heard); return; }
        this.finalText = `${this.finalText} ${heard}`.trim();
        liveFinal.textContent = this.finalText;
        if (this.stopping) this.finish();
      } else if (['auth_error', 'quota_exceeded', 'rate_limited', 'resource_exhausted', 'session_time_limit_exceeded', 'invalid_request', 'input_error', 'transcriber_error', 'error'].includes(type)) {
        const text = { auth_error: 'ElevenLabs не принял ключ — проверьте Настройки › Голос.', quota_exceeded: 'Лимит ElevenLabs исчерпан.', rate_limited: 'Слишком много запросов к ElevenLabs. Подождите минуту.',
          session_time_limit_exceeded: 'Сеанс распознавания закончился — нажмите микрофон ещё раз.' }[type] || `Ошибка распознавания: ${m.error || type}`;
        if (type === 'session_time_limit_exceeded' && this.mode === 'handsfree') { this.reconnect(); return; }
        this.fail(new Error(text));
      } else if (type === 'insufficient_audio_activity' && this.stopping) {
        this.finish();
      }
    },
    echo(heard) {
      if (!this.speakingText) return false;
      const norm = (s) => s.toLowerCase().replace(/[^a-zа-яё0-9]+/gi, ' ').trim();
      const h = norm(heard); return h.length > 3 && norm(this.speakingText).includes(h);
    },
    release() {
      if (!this.active || this.mode === 'handsfree' || this.stopping) return;
      this.stopping = true; setState('thinking');
      if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify({ message_type: 'input_audio_chunk', audio_base_64: '', commit: true, sample_rate: 16000 }));
      this.stopMic();
      this.finishTimer = setTimeout(() => this.finish(), 2500);
    },
    finish() {
      clearTimeout(this.finishTimer);
      if (this.finished) return;
      this.finished = true;
      const heard = (this.finalText || livePartial.textContent || '').replace('Слушаю…', '').trim();
      this.finalText = '';
      this.close();
      if (heard) this.deliver(heard); else say('Не расслышала. Попробуйте ещё раз, ближе к микрофону.');
    },
    async deliver(heard) {
      const normalized = await post('/api/voice/normalize', { text: heard }).then((r) => r.data.text || heard).catch(() => heard);
      const review = reviewToggle.checked || /\d/.test(normalized) && /(оплатил|поступил|перевел|перевёл|заплатил)/i.test(normalized);
      if (review && this.mode !== 'handsfree') {
        input.value = normalized; input.dataset.raw = heard; input.focus();
        say('Проверьте распознанный текст в строке и нажмите Enter (можно исправить).');
        return;
      }
      live.hidden = this.mode !== 'handsfree'; liveFinal.textContent = '';
      send(normalized, { channel: 'voice', raw: heard });
    },
    stopMic() {
      try { this.node?.disconnect(); } catch { /* already gone */ }
      this.stream?.getTracks().forEach((t) => t.stop());
      this.ctx?.close().catch(() => {});
      this.node = this.stream = this.ctx = null;
      $$('.copilot-wave i', mic).forEach((b) => { b.style.transform = ''; });
    },
    close() {
      this.active = false; this.stopping = true;
      this.stopMic();
      const ws = this.ws; this.ws = null;
      try { ws?.close(); } catch { /* closed */ }
      if (!busy) setState(this.isSpeaking() ? 'speaking' : 'idle');
      if (!handsFree.checked) live.hidden = true;
      mic.classList.remove('is-live');
    },
    fail(err) {
      const messages = { 'mic-denied': 'Нет доступа к микрофону. Safari › Настройки для этого сайта › Микрофон; Chrome — значок слева от адреса › Микрофон. На Mac также: Системные настройки › Конфиденциальность и безопасность › Микрофон › включите браузер.',
        'mic-missing': 'Микрофон не найден. Подключите его или выберите другой в настройках голоса (⚙ в диалоге).', closed: 'Соединение закрыто.' };
      say(messages[err.message] || err.message || 'Голос недоступен. Можно продолжать текстом.', 'danger');
      handsFree.checked = false;
      this.close();
    },
    isSpeaking() { return !this.audio.paused && !this.audio.ended; },
    untilQuiet(max) {          // resolves when the spoken answer ends, is stopped, or after ``max`` ms
      return new Promise((resolve) => {
        if (!this.isSpeaking()) { resolve(); return; }
        const done = () => { clearTimeout(timer); ['ended', 'pause', 'error'].forEach((t) => this.audio.removeEventListener(t, done)); resolve(); };
        const timer = setTimeout(done, max);
        ['ended', 'pause', 'error'].forEach((t) => this.audio.addEventListener(t, done));
      });
    },
    async speak(spoken) {
      this.stopSpeaking();
      if (!voiceConfig.configured || !spoken) return;
      const r = await post('/api/voice/speech', { text: spoken });
      if (!r.data.id) { say(r.data.error || 'Не удалось озвучить ответ.', 'warn'); return; }
      this.speakingText = r.data.text;
      this.audio.src = `/api/voice/speech/${r.data.id}`;
      this.audio.volume = Number(volume.value);
      const sink = local.get('fx-output', '');
      if (sink && this.audio.setSinkId) { try { await this.audio.setSinkId(sink); } catch { /* device gone */ } }
      try { await this.audio.play(); } catch (err) { if (err.name === 'NotAllowedError') say('Браузер не дал включить звук — нажмите на страницу и повторите.', 'warn'); }
    },
    stopSpeaking() {
      if (!this.audio.paused) this.audio.pause();
      this.audio.removeAttribute('src'); this.audio.load();
      this.speakingText = '';
      if (bar.dataset.state === 'speaking') setState(this.active ? 'listening' : 'idle');
    },
  };
  voice.audio.addEventListener('playing', () => { if (!voice.active) setState('speaking'); });
  ['ended', 'pause', 'error'].forEach((t) => voice.audio.addEventListener(t, () => { voice.speakingText = t === 'ended' ? '' : voice.speakingText; if (bar.dataset.state === 'speaking') setState(voice.active ? 'listening' : 'idle'); }));
  volume.addEventListener('input', () => { voice.audio.volume = Number(volume.value); local.set('fx-volume', Number(volume.value)); });

  mic.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    if (handsFree.checked) { if (voice.active) { handsFree.checked = false; voice.close(); } else voice.start('handsfree'); return; }
    if (voice.active) { voice.release(); return; }
    voice.pressedAt = Date.now(); mic.classList.add('is-live'); voice.start('push');
  });
  mic.addEventListener('pointerup', () => { if (!handsFree.checked && voice.active && Date.now() - voice.pressedAt > 450) voice.release(); });
  mic.addEventListener('pointerleave', () => { if (!handsFree.checked && voice.active && voice.pressedAt && Date.now() - voice.pressedAt > 450 && !voice.stopping) voice.release(); });
  mic.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); if (voice.active) voice.release(); else voice.start(handsFree.checked ? 'handsfree' : 'push'); } });
  handsFree.addEventListener('change', () => {
    if (handsFree.checked) { voice.close(); voice.start('handsfree'); say('Режим «без рук»: говорите, паузы отправляют фразу. «Стоп» или Esc — выключить.'); }
    else { voice.close(); say(''); }
  });
  window.addEventListener('pagehide', () => { voice.close(); share.stop(); });
  async function fillDevices() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    const devices = await navigator.mediaDevices.enumerateDevices();
    const fill = (select, kind, key) => {
      const current = local.get(key, '');
      select.innerHTML = '<option value="">По умолчанию</option>' + devices.filter((d) => d.kind === kind && d.deviceId && d.deviceId !== 'default')
        .map((d) => `<option value="${esc(d.deviceId)}" ${d.deviceId === current ? 'selected' : ''}>${esc(d.label || 'Устройство')}</option>`).join('');
    };
    fill($('#copilot-input-device'), 'audioinput', 'fx-input');
    if ('setSinkId' in HTMLMediaElement.prototype) fill($('#copilot-output-device'), 'audiooutput', 'fx-output');
    else $('#copilot-output-device').closest('label').hidden = true;
  }
  $('#copilot-input-device').addEventListener('change', (e) => local.set('fx-input', e.target.value));
  $('#copilot-output-device').addEventListener('change', (e) => local.set('fx-output', e.target.value));
  $$('.dock-menu', dock)[1].addEventListener('toggle', fillDevices, { once: true });
  $('#copilot-audio-file').addEventListener('change', async (e) => {
    const file = e.target.files[0]; if (!file) return;
    say('Расшифровываю запись…');
    const body = new FormData(); body.append('file', file);
    const r = await fetch('/api/voice/transcribe', { method: 'POST', body }).then((x) => x.json()).catch(() => ({ error: 'Нет связи.' }));
    e.target.value = '';
    if (r.error) { say(r.error, 'danger'); return; }
    input.value = r.text; input.dataset.raw = r.raw; input.focus();
    say('Запись расшифрована (и не сохранена). Проверьте текст и нажмите Enter.');
  });
  if (!voiceConfig.configured) mic.title = 'Голос не подключён — Настройки › Голос. Пока можно писать.';

  /* ---------- Keyboard ---------- */
  document.addEventListener('keydown', (e) => {
    const mod = e.metaKey || e.ctrlKey;
    if (mod && e.key.toLowerCase() === 'j') { e.preventDefault(); if (dock.hidden) openDock(true); else if (document.activeElement === input) closeDock(); else input.focus(); }
    if (mod && e.shiftKey && e.key.toLowerCase() === 'm') { e.preventDefault(); if (voice.active) voice.release(); else voice.start(handsFree.checked ? 'handsfree' : 'push'); }
    if (e.key === 'Escape') {
      if (voice.active || voice.isSpeaking() || busy) { stopAll(); return; }
      if (!suggest.hidden) { hideSuggest(); return; }
      if (document.activeElement === input) input.blur();
      else if (!dock.hidden && !document.querySelector('dialog[open]')) closeDock();
    }
  });

  refreshContext();
  chips();
  const pendingFollow = session.get('copilot-follow-up');
  if (pendingFollow && Date.now() - pendingFollow.at < 60e3) { session.set('copilot-follow-up', null); openDock(); setTimeout(() => send(pendingFollow.text, { note: 'Продолжаю: ' }), 400); }
  else if (session.get('copilot-nav') && Date.now() - session.get('copilot-nav').at < 15e3) { session.set('copilot-nav', null); openDock(); }
})();
