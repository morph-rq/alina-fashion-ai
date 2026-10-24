/* Alina Fashion · shared interface behaviour (menus, drawers, search, AI Help, form safety, worksheet). */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
  const store = {
    get(area, key) { try { return window[area].getItem(key); } catch { return null; } },
    set(area, key, value) { try { window[area].setItem(key, value); } catch { /* storage unavailable */ } },
  };

  /* ---------- Menus (details.menu) ---------- */
  const menus = () => $$('details.menu');
  document.addEventListener('toggle', event => {
    const menu = event.target;
    if (!(menu instanceof HTMLDetailsElement) || !menu.classList.contains('menu') || !menu.open) return;
    menus().forEach(other => { if (other !== menu) other.open = false; });
    const first = $('.menu-panel a, .menu-panel button', menu);
    if (first && menu.dataset.focusFirst !== 'false') window.requestAnimationFrame(() => first.focus());
  }, true);
  document.addEventListener('click', event => {
    menus().forEach(menu => { if (menu.open && !menu.contains(event.target)) menu.open = false; });
  });
  document.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    const open = menus().find(menu => menu.open);
    if (open) { open.open = false; $('summary', open)?.focus(); }
  });

  /* ---------- Drawers and dialogs ---------- */
  const openDialog = (dialog, opener) => {
    if (!dialog || dialog.open) return;
    dialog._opener = opener || document.activeElement;
    /* data-set='{"field": "value"}' on the opener pre-fills fields, e.g. the Ready batch of a ledger row. */
    if (opener?.dataset?.set) {
      try {
        Object.entries(JSON.parse(opener.dataset.set)).forEach(([name, value]) => {
          const field = dialog.querySelector(`[name="${CSS.escape(name)}"]`);
          if (field) { field.value = value; field.dispatchEvent(new Event('change', { bubbles: true })); }
        });
      } catch { /* ignore malformed presets */ }
    }
    if (typeof dialog.showModal === 'function') dialog.showModal(); else dialog.setAttribute('open', '');
    const target = $('[autofocus]', dialog) || $('input:not([type=hidden]), select, textarea', dialog);
    target?.focus();
  };
  const closeDialog = dialog => {
    if (!dialog) return;
    if (typeof dialog.close === 'function') dialog.close(); else dialog.removeAttribute('open');
  };
  document.addEventListener('click', event => {
    const opener = event.target.closest('[data-open]');
    if (opener) {
      const dialog = document.getElementById(opener.dataset.open);
      if (dialog) {
        event.preventDefault();
        opener.closest('details.menu')?.removeAttribute('open');
        openDialog(dialog, opener);
      }
      return;
    }
    const closer = event.target.closest('[data-close]');
    if (closer) { closeDialog(closer.closest('dialog')); return; }
    if (event.target instanceof HTMLDialogElement && event.target.classList.contains('drawer')) {
      const rect = event.target.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) closeDialog(event.target);
    }
  });
  $$('dialog').forEach(dialog => dialog.addEventListener('close', () => dialog._opener?.focus?.()));
  const openFromHash = () => {
    const match = /^#do-([\w-]+)$/.exec(window.location.hash);
    if (match) openDialog(document.getElementById(match[1]));
  };
  openFromHash();
  window.addEventListener('hashchange', openFromHash);

  /* Scroll to a section requested by a redirect (?focus=…) when no #anchor was given. */
  const query = new URLSearchParams(window.location.search);
  const focusTarget = query.get('focus');
  if (focusTarget && !window.location.hash) document.getElementById(focusTarget)?.scrollIntoView({ block: 'start' });
  /* A one-time confirmation (?done=…) shows once: drop it from the address and fade the toast. */
  if (query.has('done')) {
    query.delete('done');
    window.history.replaceState(null, '', window.location.pathname + (query.toString() ? `?${query}` : '') + window.location.hash);
  }
  $$('.notice.toast').forEach(toast => window.setTimeout(() => { toast.hidden = true; }, 7000));

  /* ---------- Form safety ---------- */
  let submitting = false;
  const dirtyForms = new Set();
  /* event.target.form also covers fields placed outside their form with form="…" (brand table rows). */
  const markDirty = event => {
    const form = event.target?.form;
    if (form && form.hasAttribute('data-guard') && event.target.type !== 'hidden') dirtyForms.add(form);
  };
  document.addEventListener('input', markDirty);
  document.addEventListener('change', markDirty);
  document.addEventListener('submit', event => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { event.preventDefault(); return; }
    if (event.defaultPrevented) return;
    submitting = true;
    if (form.hasAttribute('data-once')) {
      window.setTimeout(() => $$('button[type=submit], button:not([type])', form).forEach(button => {
        button.disabled = true;
        button.dataset.label = button.dataset.label || button.textContent;
        if (!button.dataset.keepLabel) button.textContent = 'Saving…';
      }), 0);
    }
  });
  window.addEventListener('beforeunload', event => {
    if (submitting || !dirtyForms.size) return;
    event.preventDefault();
    event.returnValue = '';
  });
  /* Coming back with the browser's Back button (for example after a refused save) re-enables the form. */
  window.addEventListener('pageshow', () => {
    submitting = false;
    $$('form[data-once] button[data-label]').forEach(button => { button.disabled = false; button.textContent = button.dataset.label; });
  });

  /* File pickers: an English label and the chosen file names, whatever the browser's language. */
  $$('input[type=file]').forEach(input => {
    if (input.classList.contains('sr-only-input') || input.closest('.image-dropzone, .composer-action')) return;
    const wrap = document.createElement('span');
    wrap.className = 'file-pick';
    input.parentNode.insertBefore(wrap, input);
    const button = document.createElement('span');
    button.className = 'btn sm';
    button.setAttribute('aria-hidden', 'true');
    button.textContent = input.multiple ? 'Choose files' : 'Choose a file';
    const names = document.createElement('span');
    names.className = 'file-names';
    names.setAttribute('aria-live', 'polite');
    names.textContent = 'No file chosen';
    wrap.append(input, button, names);
    input.addEventListener('change', () => {
      const files = Array.from(input.files || []);
      names.textContent = files.length ? files.map(file => file.name).join(', ') : 'No file chosen';
    });
  });

  /* Client-side filtering of short lists (customers, brands). */
  $$('[data-filter-input]').forEach(input => {
    const list = document.getElementById(input.dataset.filterInput);
    const count = input.dataset.filterCount ? document.getElementById(input.dataset.filterCount) : null;
    const empty = input.dataset.filterEmpty ? document.getElementById(input.dataset.filterEmpty) : null;
    const rows = list ? $$('[data-search]', list) : [];
    input.addEventListener('input', () => {
      const query = input.value.trim().toLowerCase();
      let shown = 0;
      rows.forEach(row => { const match = !query || row.dataset.search.includes(query); row.hidden = !match; if (match) shown += 1; });
      if (count) count.textContent = String(shown);
      if (empty) empty.hidden = shown !== 0;
    });
  });

  /* ---------- Workspace memory: return to the last filtered worksheet ---------- */
  if (window.location.pathname === '/worksheet') {
    /* One-time confirmations (saved=…, updated=…) are not part of the remembered view. */
    const params = new URLSearchParams(window.location.search);
    const hadNotice = params.has('saved') || params.has('updated');
    params.delete('saved');
    params.delete('updated');
    const clean = window.location.pathname + (params.toString() ? `?${params}` : '');
    if (hadNotice) window.history.replaceState(null, '', clean + window.location.hash);
    store.set('sessionStorage', 'ws-last', clean);
  }
  const lastWorksheet = store.get('sessionStorage', 'ws-last');
  if (lastWorksheet && lastWorksheet.startsWith('/worksheet')) {
    $$('a[data-workspace-link]').forEach(link => { link.href = lastWorksheet; });
  }

  /* ---------- Worksheet ---------- */
  const filterForm = document.getElementById('workbook-filter');
  const filtersToggle = document.getElementById('filters-toggle');
  if (filterForm && filtersToggle) {
    /* On phones the filters fold behind one button; the search field stays visible. */
    filterForm.classList.add('has-toggle');
    filtersToggle.addEventListener('click', () => {
      const open = filterForm.classList.toggle('show-filters');
      filtersToggle.setAttribute('aria-expanded', String(open));
    });
  }
  if (filterForm) {
    /* Leave empty filters out of the address so links and bookmarks stay short. */
    filterForm.addEventListener('submit', () => $$('input, select', filterForm).forEach(field => { if (!field.value) field.disabled = true; }));
    window.addEventListener('pageshow', () => $$('input, select', filterForm).forEach(field => { field.disabled = false; }));
    $$('select', filterForm).forEach(select => select.addEventListener('change', () => filterForm.requestSubmit()));
    $$('[data-state]').forEach(button => button.addEventListener('click', () => {
      const field = document.getElementById('state-filter');
      field.value = field.value === button.dataset.state ? '' : button.dataset.state;
      filterForm.requestSubmit();
    }));
  }
  const columnsToggle = document.getElementById('toggle-columns');
  if (columnsToggle) {
    const apply = on => { document.body.classList.toggle('cols-all', on); columnsToggle.setAttribute('aria-pressed', String(on)); };
    apply(store.get('localStorage', 'ws-cols') === 'all');
    columnsToggle.addEventListener('click', () => {
      const on = !document.body.classList.contains('cols-all');
      apply(on);
      store.set('localStorage', 'ws-cols', on ? 'all' : 'short');
    });
  }
  const bulkForm = document.getElementById('bulk-form');
  if (bulkForm) {
    const checks = $$('input[name="item_ids"]', bulkForm);
    const all = document.getElementById('select-all');
    const bar = document.getElementById('bulk-bar');
    const label = document.getElementById('selection-count');
    const apply = document.getElementById('apply-selection');
    const update = () => {
      const n = checks.filter(check => check.checked).length;
      if (label) label.textContent = `${n} selected`;
      if (apply) apply.disabled = !n;
      if (bar) bar.hidden = !n;
      if (all) { all.checked = checks.length > 0 && n === checks.length; all.indeterminate = n > 0 && n < checks.length; }
    };
    all?.addEventListener('change', () => { checks.forEach(check => { check.checked = all.checked; }); update(); });
    checks.forEach(check => check.addEventListener('change', update));
    document.getElementById('clear-selection')?.addEventListener('click', () => { checks.forEach(check => { check.checked = false; }); update(); });
    update();
  }

  /* ---------- Global search ---------- */
  const searchDialog = document.getElementById('search-dialog');
  const searchInput = document.getElementById('global-search-query');
  const searchResults = document.getElementById('global-search-results');
  if (searchDialog && searchInput && searchResults) {
    let timer;
    let controller;
    const welcome = (title, detail = '') => {
      searchResults.replaceChildren();
      const box = document.createElement('div');
      box.className = 'search-welcome';
      const strong = document.createElement('strong');
      strong.textContent = title;
      const span = document.createElement('span');
      span.textContent = detail;
      box.append(strong, span);
      searchResults.append(box);
    };
    const openSearch = () => { openDialog(searchDialog); searchInput.select(); };
    $$('[data-search-open]').forEach(button => button.addEventListener('click', openSearch));
    document.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') { event.preventDefault(); openSearch(); }
    });
    searchDialog.addEventListener('close', () => controller?.abort());
    const run = async query => {
      controller?.abort();
      controller = new AbortController();
      welcome('Searching…');
      try {
        const response = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: controller.signal });
        const data = await response.json();
        searchResults.replaceChildren();
        if (!data.results?.length) { welcome('Nothing found', 'Try a name, phone number, SKU, brand, season or file name.'); return; }
        data.results.forEach(row => {
          const link = document.createElement('a');
          link.href = row.url;
          const badge = document.createElement('span');
          badge.className = 'search-result-type';
          badge.textContent = row.type;
          const text = document.createElement('span');
          const title = document.createElement('strong');
          title.textContent = row.title;
          const small = document.createElement('small');
          small.textContent = row.subtitle || '';
          text.append(title, small);
          const arrow = document.createElement('i');
          arrow.setAttribute('aria-hidden', 'true');
          arrow.textContent = '↗';
          link.append(badge, text, arrow);
          searchResults.append(link);
        });
      } catch (error) {
        if (error.name !== 'AbortError') welcome('Search is unavailable right now', 'Try again in a moment.');
      }
    };
    searchInput.addEventListener('input', () => {
      window.clearTimeout(timer);
      const query = searchInput.value.trim();
      if (query.length < 2) { welcome('Find anything', 'A customer, phone, SKU, product, brand, season or file.'); return; }
      timer = window.setTimeout(() => run(query), 180);
    });
    searchInput.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown') { event.preventDefault(); $('a', searchResults)?.focus(); }
    });
    searchResults.addEventListener('keydown', event => {
      const links = $$('a', searchResults);
      const index = links.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' && index > -1) { event.preventDefault(); links[Math.min(index + 1, links.length - 1)].focus(); }
      if (event.key === 'ArrowUp' && index > -1) { event.preventDefault(); (index === 0 ? searchInput : links[index - 1]).focus(); }
    });
  }

  /* ---------- AI Help (business helper) ---------- */
  const helpForm = document.getElementById('ai-help-form');
  const helpInput = document.getElementById('ai-help-question');
  const helpMessages = document.getElementById('ai-help-messages');
  if (helpForm && helpInput && helpMessages) {
    const currency = document.body.dataset.currency || '';
    const bubble = (text, kind = 'assistant') => {
      const node = document.createElement('div');
      node.className = `${kind}-bubble`;
      node.textContent = text;
      helpMessages.append(node);
      helpMessages.scrollTop = helpMessages.scrollHeight;
      return node;
    };
    const addLink = (href, label, download = false) => {
      const link = document.createElement('a');
      link.className = 'btn sm assistant-action';
      link.href = href;
      if (download) link.download = '';
      link.textContent = label;
      helpMessages.append(link);
    };
    const ask = async question => {
      bubble(question, 'user');
      const waiting = bubble('Thinking…');
      try {
        const response = await fetch('/api/assistant/help', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: question }) });
        const data = await response.json();
        waiting.textContent = data.answer || data.detail || 'I could not answer that.';
        if (data.profit && /profit|money|earn|margin/i.test(question)) {
          const box = document.createElement('div');
          box.className = 'assistant-profit';
          const label = document.createElement('span');
          label.textContent = 'Projected profit';
          const value = document.createElement('strong');
          value.textContent = `${Number(data.profit.projected_profit).toLocaleString()} ${currency}`;
          const note = document.createElement('small');
          note.textContent = data.profit.explanation;
          box.append(label, value, note);
          helpMessages.append(box);
        }
        if (data.suggested_page) addLink(data.suggested_page, data.suggested_page_label || 'Open page');
        if (data.download_url) addLink(data.download_url, data.download_label || 'Download Excel file', true);
        if (data.rule_proposal) {
          const proposal = data.rule_proposal;
          const box = document.createElement('form');
          box.className = 'assistant-proposal';
          box.method = 'post';
          box.action = '/assistant/apply-rule';
          ['rule_id', 'markup_percentage', 'initial_percentage', 'ready_percentage', 'transport_percentage', 'special_cost', 'explanation'].forEach(name => {
            if (proposal[name] === null || proposal[name] === undefined) return;
            const hidden = document.createElement('input');
            hidden.type = 'hidden';
            hidden.name = name;
            hidden.value = proposal[name];
            box.append(hidden);
          });
          const confirmation = document.createElement('input');
          confirmation.type = 'hidden';
          confirmation.name = 'confirmation';
          confirmation.value = 'APPLY_AI_RULE_PROPOSAL';
          const title = document.createElement('strong');
          title.textContent = 'Review this formula proposal';
          const detail = document.createElement('small');
          detail.textContent = proposal.explanation;
          const submit = document.createElement('button');
          submit.className = 'btn primary sm';
          submit.textContent = 'Apply this proposal';
          box.append(confirmation, title, detail, submit);
          helpMessages.append(box);
        }
        helpMessages.scrollTop = helpMessages.scrollHeight;
      } catch {
        waiting.textContent = 'AI Help is unavailable right now. Try again shortly.';
      }
    };
    helpForm.addEventListener('submit', event => {
      event.preventDefault();
      const question = helpInput.value.trim();
      if (!question) return;
      helpInput.value = '';
      ask(question);
    });
    helpInput.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); helpForm.requestSubmit(); }
    });
    $$('[data-ai-prompt]').forEach(button => button.addEventListener('click', () => ask(button.textContent)));
  }

  /* ---------- Spreadsheet cells: click or type to edit, Enter/Tab saves, Esc cancels ---------- */
  const sheetStatus = (() => {
    let box = document.getElementById('sheet-status');
    if (!box) { box = document.createElement('div'); box.id = 'sheet-status'; box.className = 'sheet-status'; box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite'); document.body.append(box); }
    let timer;
    return (message, error = false) => { box.textContent = message; box.classList.toggle('error', error); box.classList.add('show'); clearTimeout(timer); timer = setTimeout(() => box.classList.remove('show'), error ? 6000 : 1600); };
  })();
  const editableCells = row => $$('[data-edit-key]', row);
  const moveTo = (cell, direction) => {
    const row = cell.closest('[data-edit-url]');
    if (!row) return;
    let target;
    if (direction === 'right') { const cells = editableCells(row); target = cells[cells.indexOf(cell) + 1]; }
    else if (direction === 'down') {
      let next = row.nextElementSibling;
      while (next && !next.matches('[data-edit-url]')) next = next.nextElementSibling;
      target = next && next.querySelector(`[data-edit-key="${cell.dataset.editKey}"]`);
    }
    if (target) { target.focus(); startEdit(target); }
  };
  function startEdit(cell, firstKey) {
    if (cell.querySelector('input')) return;
    const row = cell.closest('[data-edit-url]');
    if (!row) return;
    const original = cell.dataset.value ?? cell.textContent.trim();
    const input = document.createElement('input');
    input.className = 'cell-input';
    input.value = firstKey ?? original;
    if (cell.dataset.type === 'date') input.placeholder = 'YYYY-MM-DD';
    if (['number', 'money'].includes(cell.dataset.type)) input.inputMode = 'decimal';
    input.setAttribute('aria-label', cell.getAttribute('aria-label') || 'Cell');
    cell.classList.add('editing');
    cell.replaceChildren(input);
    input.focus();
    if (firstKey === undefined) input.select();
    let done = false;
    const finish = async (move) => {
      if (done) return;
      done = true;
      const value = input.value.trim();
      cell.classList.remove('editing');
      if (value === original) { cell.textContent = original; if (move) moveTo(cell, move); else cell.focus(); return; }
      cell.textContent = value;
      cell.classList.add('saving');
      try {
        const response = await fetch(row.dataset.editUrl, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ row_id: row.dataset.rowId, key: cell.dataset.editKey, field: cell.dataset.editKey, value, revision: row.dataset.revision }) });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || data.detail || 'Not saved.');
        cell.textContent = data.value ?? value;
        cell.dataset.value = data.value ?? value;
        if (data.revision !== undefined) row.dataset.revision = data.revision;
        cell.classList.remove('cell-error');
        if (cell.textContent) cell.classList.remove('cell-missing');
        cell.removeAttribute('title');
        cell.classList.add('saved');
        setTimeout(() => cell.classList.remove('saved'), 900);
        sheetStatus('Saved');
      } catch (error) {
        cell.textContent = original;
        cell.classList.add('cell-error');
        cell.title = error.message;
        sheetStatus(error.message, true);
      } finally {
        cell.classList.remove('saving');
      }
      if (move) moveTo(cell, move); else cell.focus();
    };
    input.addEventListener('keydown', event => {
      if (event.key === 'Enter') { event.preventDefault(); finish('down'); }
      else if (event.key === 'Tab') { event.preventDefault(); finish(event.shiftKey ? null : 'right'); }
      else if (event.key === 'Escape') { done = true; cell.classList.remove('editing'); cell.textContent = original; cell.focus(); }
    });
    input.addEventListener('blur', () => finish(null));
  }
  document.addEventListener('click', event => {
    const cell = event.target.closest('[data-edit-key]');
    if (cell && !event.target.closest('a, button, input, select') && cell.closest('[data-edit-url]')) startEdit(cell);
  });
  document.addEventListener('keydown', event => {
    const cell = event.target.closest?.('[data-edit-key]');
    if (!cell || event.target !== cell) return;
    if (event.key === 'Enter' || event.key === 'F2') { event.preventDefault(); startEdit(cell); }
    else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) { event.preventDefault(); startEdit(cell, event.key); }
  });
})();
