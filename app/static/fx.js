/* Factory workflow screens: preview → confirm, inline grid editing with undo, uploads, assistant chat. */
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const eur = (v) => `€ ${Number(v).toLocaleString('ru-RU', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  const post = (url, body) => fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' }, body: JSON.stringify(body) })
    .then(async (r) => ({ status: r.status, data: await r.json().catch(() => ({})) }));
  const season = () => ($('[data-season]')?.dataset.season) || new URLSearchParams(location.search).get('season') || undefined;
  const store = { get(k) { try { return JSON.parse(sessionStorage.getItem(k) || 'null'); } catch { return null; } }, set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } } };

  /* ---------- Preview, then commit (same code path as the assistant) ---------- */
  function diffTable(diff) {
    if (!diff || !diff.length) return '<p class="muted">Суммы не изменятся.</p>';
    return `<table class="table fx-diff"><thead><tr><th></th><th class="num">Было</th><th class="num">Станет</th><th class="num">Разница</th></tr></thead><tbody>${
      diff.map((r) => `<tr><td>${esc(r.label)}</td><td class="num">${eur(r.before)}</td><td class="num">${eur(r.after)}</td><td class="num"><strong>${Number(r.delta) > 0 ? '+' : ''}${eur(r.delta)}</strong></td></tr>`).join('')}</tbody></table>`;
  }
  async function previewAndCommit(actions, reason, undo) {
    const dialog = $('#fx-preview');
    const result = await post('/api/actions/preview', { actions });
    if (!result.data.ok) { alert(result.data.error || 'Не получилось'); return false; }
    if (!dialog) return commit(actions, reason, undo);
    $('[data-preview-body]', dialog).innerHTML = `<ul>${result.data.messages.map((m) => `<li>${esc(m)}</li>`).join('')}</ul>${diffTable(result.data.diff)}`;
    dialog.showModal();
    return new Promise((resolve) => {
      $('[data-preview-confirm]', dialog).onclick = async () => { dialog.close(); resolve(await commit(actions, reason, undo)); };
      $('[data-preview-cancel]', dialog).onclick = () => { dialog.close(); resolve(false); };
    });
  }
  async function commit(actions, reason, undo) {
    const result = await post('/api/actions/commit', { actions, reason });
    if (!result.data.ok) { alert(result.data.error || 'Не сохранено'); return false; }
    if (undo) { const stack = store.get('fx-undo') || []; stack.push(undo); store.set('fx-undo', stack.slice(-30)); }
    store.set('fx-scroll', [location.pathname + location.search, window.scrollY]);
    location.reload();
    return true;
  }
  const saved = store.get('fx-scroll');
  if (saved && saved[0] === location.pathname + location.search) { window.scrollTo(0, saved[1]); store.set('fx-scroll', null); }

  /* ---------- Orders grid ---------- */
  const grid = $('#fx-grid');
  if (grid) {
    const actionFor = (field, id, value, cell) => {
      const item_id = Number(id);
      if (field === 'customer') return { action: 'move_item', params: { item_id, customer: value } };
      if (field === 'markup') return { action: 'set_item_markup', params: { item_id, markup: value } };
      if (field === 'price') return { action: 'set_manual_price', params: { item_id, unit_price: value || null } };
      if (field === 'status') {
        const params = { item_ids: [item_id], status: value };
        if (value === 'Cancelled') { const reason = prompt('Причина отмены?'); if (!reason) return null; params.reason = reason; }
        return { action: 'set_status', params };
      }
      return { action: 'update_item', params: { item_id, [field === 'quantity' ? 'quantity' : field]: value } };
    };
    const currentValue = (cell) => {
      const f = cell.dataset.edit;
      if (f === 'customer') return $('.chip', cell)?.textContent.split(' ')[0].replace('кому?', '') || '';
      if (f === 'status') return cell.dataset.status || '';
      return cell.textContent.replace('✎', '').replace('добавить', '').trim().replace(/\s/g, f === 'markup' || f === 'price' || f === 'quantity' ? '' : ' ');
    };
    const open = (cell) => {
      if (cell.querySelector('input,select')) return;
      const field = cell.dataset.edit;
      const before = currentValue(cell);
      let input;
      if (cell.dataset.options) {
        input = document.createElement('select');
        input.innerHTML = cell.dataset.options.split(',').map((o) => { const [value, label] = o.split('|'); return `<option value="${esc(value)}" ${value === before ? 'selected' : ''}>${esc(label || value)}</option>`; }).join('');
      } else {
        input = document.createElement('input');
        input.value = before.replace(',', '.');
        input.size = 8;
        if (['quantity', 'markup', 'price'].includes(field)) input.inputMode = 'decimal';
      }
      input.className = 'cell-input';
      const original = cell.innerHTML;
      cell.innerHTML = '';
      cell.appendChild(input);
      input.focus();
      let done = false;
      const finish = async (save) => {
        if (done) return; done = true;
        const value = input.value.trim();
        if (!save || value === before) { cell.innerHTML = original; return; }
        const action = actionFor(field, cell.dataset.item, value, cell);
        if (!action) { cell.innerHTML = original; return; }
        const undo = before === '' && field !== 'price' ? null : actionFor(field, cell.dataset.item, before, cell);
        const money = ['customer', 'markup', 'price', 'quantity', 'status'].includes(field);
        const ok = money ? await previewAndCommit([action], 'Изменение в таблице заказов', undo) : await commit([action], 'Изменение в таблице заказов', undo);
        if (!ok) cell.innerHTML = original;
      };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); });
      input.addEventListener('blur', () => finish(true));
      if (input.tagName === 'SELECT') input.addEventListener('change', () => finish(true));
    };
    $$('[data-edit]', grid).forEach((cell) => {
      cell.addEventListener('click', () => open(cell));
      cell.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target === cell) open(cell); });
    });
    document.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) {
        const stack = store.get('fx-undo') || [];
        const last = stack.pop();
        if (!last) return;
        store.set('fx-undo', stack);
        e.preventDefault();
        commit([last], 'Отмена последнего изменения');
      }
    });
    const bar = $('#fx-bulk');
    const selected = () => $$('[data-select]:checked', grid).map((c) => Number(c.value));
    const refresh = () => {
      const n = selected().length; bar.hidden = !n; $('[data-bulk-count]', bar).textContent = n;
      store.set('fx-board-selection', { ids: selected(), season: season(), at: Date.now() });
    };
    $$('[data-select]', grid).forEach((c) => c.addEventListener('change', refresh));
    $('[data-select-all]', grid)?.addEventListener('change', (e) => { $$('[data-select]', grid).forEach((c) => { c.checked = e.target.checked; }); refresh(); });
    $('[data-bulk-clear]', bar)?.addEventListener('click', () => { $$('[data-select]', grid).forEach((c) => { c.checked = false; }); refresh(); });
    $('[data-bulk-apply]', bar)?.addEventListener('click', () => {
      const ids = selected(); const actions = [];
      const status = $('[data-bulk-status]', bar).value; const customer = $('[data-bulk-customer]', bar).value; const markup = $('[data-bulk-markup]', bar).value.trim();
      if (status) { const params = { item_ids: ids, status }; if (status === 'Cancelled') { const reason = prompt('Причина отмены?'); if (!reason) return; params.reason = reason; } actions.push({ action: 'set_status', params }); }
      if (customer) ids.forEach((item_id) => actions.push({ action: 'move_item', params: { item_id, customer } }));
      if (markup) ids.forEach((item_id) => actions.push({ action: 'set_item_markup', params: { item_id, markup } }));
      if (!actions.length) { alert('Выберите, что изменить'); return; }
      previewAndCommit(actions, `Массовое изменение (${ids.length} поз.)`);
    });
  }

  /* ---------- Factories, pricing ---------- */
  $$('[data-terms]').forEach((b) => b.addEventListener('click', () => {
    const form = $('#fx-terms'); form.hidden = false; form.order_id.value = b.dataset.terms;
    $('[data-terms-brand]', form).textContent = b.dataset.brand; form.rate1.focus();
  }));
  $('#fx-terms')?.addEventListener('submit', (e) => {
    e.preventDefault(); const f = e.target;
    const portions = [{ rate: f.rate1.value, share: f.share1.value || '100' }];
    if (f.rate2.value) portions.push({ rate: f.rate2.value, share: f.share2.value || '50' });
    previewAndCommit([{ action: 'set_factory_rate', params: { order_id: Number(f.order_id.value), portions, deposit_rate: f.deposit.value } }], 'Условия фабрики');
  });
  $('#fx-pay')?.addEventListener('submit', (e) => {
    e.preventDefault(); const f = e.target;
    previewAndCommit([{ action: 'record_payment', params: { payer: f.payer.value, amount: f.amount.value, to: f.to.value, confirmed: f.confirmed.value === 'true',
      date: f.date.value || undefined, reference: f.reference.value || undefined, season: f.dataset.season } }], 'Оплата');
  });
  $$('[data-confirm-payment]').forEach((b) => b.addEventListener('click', () => previewAndCommit([{ action: 'confirm_payment', params: { payment_id: Number(b.dataset.confirmPayment) } }], 'Оплата подтверждена')));
  $('#fx-markup')?.addEventListener('submit', (e) => {
    e.preventDefault(); const f = e.target; const params = { customer: f.customer.value, markup: f.markup.value };
    if (f.scope.value === 'season') params.season = f.dataset.season;
    if (f.scope.value.startsWith('brand:')) { params.season = f.dataset.season; params.brand = f.scope.value.slice(6); }
    previewAndCommit([{ action: 'set_customer_markup', params }], `Наценка ${params.customer}`);
  });
  $$('[data-set-owner]').forEach((b) => b.addEventListener('click', () => previewAndCommit([{ action: 'set_owner', params: { customer: b.dataset.setOwner } }], 'Владелец')));

  /* ---------- Upload with progress ---------- */
  const upload = $('#fx-upload');
  if (upload) {
    const zone = $('[data-drop]', upload); const input = $('#fx-files');
    ['dragenter', 'dragover'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.add('is-over'); }));
    ['dragleave', 'drop'].forEach((t) => zone.addEventListener(t, (e) => { e.preventDefault(); zone.classList.remove('is-over'); }));
    zone.addEventListener('drop', (e) => { input.files = e.dataTransfer.files; upload.requestSubmit(); });
    input.addEventListener('change', () => { if (input.files.length) upload.requestSubmit(); });
    upload.addEventListener('submit', (e) => {
      e.preventDefault();
      if (!input.files.length) { input.click(); return; }
      const big = Array.from(input.files).find((f) => f.size > 1024 ** 3);
      if (big) { alert(`«${big.name}» больше 1 ГБ`); return; }
      const box = $('#fx-upload-progress'); box.hidden = false;
      const xhr = new XMLHttpRequest();
      xhr.open('POST', upload.action); xhr.setRequestHeader('Accept', 'application/json');
      xhr.upload.onprogress = (ev) => { if (ev.lengthComputable) { $('span', box).style.width = `${(ev.loaded / ev.total) * 100}%`; $('p', box).textContent = `Загружено ${Math.round(ev.loaded / 1048576)} из ${Math.round(ev.total / 1048576)} МБ`; } };
      xhr.onload = () => {
        if (xhr.status >= 400) { $('p', box).textContent = (JSON.parse(xhr.responseText || '{}').detail) || 'Ошибка загрузки'; return; }
        const tokens = JSON.parse(xhr.responseText).tokens || [];
        location.href = tokens.length === 1 ? `/factory-import/${tokens[0]}` : '/factory-import';
      };
      xhr.send(new FormData(upload));
    });
  }
  const poll = $('[data-poll]');
  if (poll) {
    const tick = async () => {
      const status = await fetch(`/factory-import/${poll.dataset.poll}/status`).then((r) => r.json());
      $('span', poll).style.width = `${Math.max(10, (status.progress || 0) * 100)}%`;
      if (!['queued', 'analysing'].includes(status.state)) location.reload(); else setTimeout(tick, 1500);
    };
    setTimeout(tick, 1200);
  }

  /* The assistant page (chat, voice, result panel) lives in assistant.js. */
})();
