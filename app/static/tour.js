/* Guided tour: highlights one part of the app at a time and explains it in plain words.
   Start with the Guide button, Overview's "Take the tour", or any URL with ?tour=1.
   Progress survives page changes (sessionStorage); Esc or × ends it. Nothing is changed by the tour. */
(() => {
  const KEY = 'alina-tour-step';
  const store = {
    get() { try { return sessionStorage.getItem(KEY); } catch { return null; } },
    set(value) { try { value === null ? sessionStorage.removeItem(KEY) : sessionStorage.setItem(KEY, value); } catch { /* private mode: tour still works on this page */ } },
  };
  const STEPS = [
    { path: '/', target: ['.shell-nav', '.tabbar'], title: 'Four places for everything',
      text: 'Overview shows the season at a glance. Workspace holds all goods and money. Inbox is where new orders arrive. Customers holds each client with their balance and statements.' },
    { path: '/', target: ['.figures', '.page-header'], title: 'The season in four numbers',
      text: 'Order value, what customers still owe, what you owe the factory, and goods that are Ready but not shipped. Click any number to see the rows behind it.' },
    { path: '/', target: ['#next-actions', '.overview-grid'], title: 'What to do next',
      text: 'The app lists what needs attention, such as goods without a customer or payments to record. Each item links straight to the place where you fix it.' },
    { path: '/', target: ['.shell-tools'], title: 'Search, AI Help, Add, Settings',
      text: 'Search finds any customer, product or file (Ctrl K). AI Help answers questions about the business. Add starts a new order, payment or season. The gear opens Settings.' },
    { path: '/worksheet', target: ['.view-switch'], title: 'Workspace views',
      text: 'Products is the main list. Brands shows each season table. Ready & shipping covers batches, shipments and costs. Money covers payments. Tables holds the tables you describe yourself.' },
    { path: '/worksheet', target: ['.ws-states', '.legend'], title: 'Red, yellow, green',
      text: 'Red means not done, yellow in process, green done. Every product has one of these colours, so you can see the state of the season at a glance.' },
    { path: '/worksheet', target: ['.product-row [data-edit-key]', '.wb-table'], title: 'Type straight into cells',
      text: 'Click a cell such as colour, size or quantity and type, just like Excel. Enter saves and moves down, Tab moves right, Esc cancels. Every change is recorded in the history.' },
    { path: '/worksheet', target: ['.select-all', '#select-all'], title: 'Change many rows at once',
      text: 'Tick rows (or Select shown), then choose a colour or a shipment group in the bar that appears and press Apply.' },
    { path: '/worksheet', target: ['.workspace-bar .menu', '.workspace-bar'], title: 'Excel in and out',
      text: 'Download everything as Excel, edit it, and upload it back: you see every change before it is saved. Upload a supplier master workbook here too. "Check the last upload" lists every problem in it, colour-coded.' },
    { path: '/tables', target: ['.describe-panel', '.page-header'], title: 'Describe a table',
      text: 'Write what you need, for example "New season FW27 with brands Atelier Nord and Lumen, 30% markup" or "a table of suppliers with name, phone, email". You get a draft first; nothing is created until you press Create.' },
    { path: '/inbox', target: ['#compose', '.page-header'], title: 'New orders arrive here',
      text: 'Type or dictate an order, or drop photos, PDFs and spreadsheets. AI drafts the rows and asks about anything missing. You correct and save once; saving twice never creates duplicates.' },
    { path: '/customers', target: ['#customer-list', '.filter-bar', '.page-header'], title: 'Customers and balances',
      text: 'Each customer shows what they ordered, paid and still owe. Open one for their goods, shipments and payments, and to download a private PDF statement or Excel file for them.' },
    { path: '/settings', target: ['.settings-groups', '.page-header'], title: 'Settings',
      text: 'Pricing rules per brand and season, Google Drive and Gmail, the AI assistant, backups and restore, and the full history of changes.' },
    { path: '/settings/ai', target: ['form[action="/settings/ai"]', '.page-header'], title: 'Connect Claude',
      text: 'Paste a Claude API key and choose Sonnet 5.5 (best quality) or Haiku 5.5 (fastest). The key is stored encrypted and never shown again. Without it everything still works by hand.' },
    { path: '/', target: [], title: 'You are ready',
      text: 'Start with Add, or describe an order in Inbox. You can replay this guide any time with the Guide button at the top.' },
  ];

  let box = null;
  let spot = null;
  let target = null;
  let index = -1;

  const visible = el => el && el.offsetWidth > 0 && el.offsetHeight > 0;
  const findTarget = step => {
    for (const selector of step.target) {
      const el = document.querySelector(selector);
      if (visible(el)) return el;
    }
    return null;
  };

  function place() {
    if (!box) return;
    const narrow = window.innerWidth < 720;
    box.classList.toggle('docked', narrow || !target);
    if (target) {
      const r = target.getBoundingClientRect();
      Object.assign(spot.style, { display: 'block', top: `${r.top - 6}px`, left: `${r.left - 6}px`, width: `${r.width + 12}px`, height: `${r.height + 12}px` });
      if (!narrow) {
        const w = box.offsetWidth;
        const h = box.offsetHeight;
        const below = r.bottom + 14;
        const top = below + h < window.innerHeight - 12 ? below : Math.max(12, r.top - h - 14);
        box.style.top = `${top}px`;
        box.style.left = `${Math.min(Math.max(16, r.left), window.innerWidth - w - 16)}px`;
      }
    } else {
      Object.assign(spot.style, { display: 'block', top: '50%', left: '50%', width: '0px', height: '0px' });
    }
    if (narrow || !target) { box.style.top = ''; box.style.left = ''; }
  }

  function close() {
    store.set(null);
    box?.remove();
    spot?.remove();
    box = spot = target = null;
    index = -1;
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
    document.removeEventListener('keydown', onKey, true);
  }

  function go(next) {
    if (next < 0) return;
    if (next >= STEPS.length) { close(); return; }
    store.set(String(next));
    if (location.pathname !== STEPS[next].path) { location.href = STEPS[next].path; return; }
    show(next);
  }

  function onKey(event) {
    if (!box) return;
    if (event.key === 'Escape') { event.preventDefault(); close(); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); go(index + 1); }
    else if (event.key === 'ArrowLeft') { event.preventDefault(); go(index - 1); }
  }

  function button(label, className, handler) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = className;
    b.textContent = label;
    b.addEventListener('click', handler);
    return b;
  }

  function show(i) {
    const step = STEPS[i];
    index = i;
    if (!box) {
      spot = document.createElement('div');
      spot.className = 'tour-spot';
      spot.setAttribute('aria-hidden', 'true');
      box = document.createElement('div');
      box.className = 'tour-box';
      box.setAttribute('role', 'dialog');
      box.setAttribute('aria-live', 'polite');
      box.setAttribute('aria-labelledby', 'tour-title');
      document.body.append(spot, box);
      window.addEventListener('resize', place);
      window.addEventListener('scroll', place, true);
      document.addEventListener('keydown', onKey, true);
    }
    target = findTarget(step);
    const head = document.createElement('div');
    head.className = 'tour-head';
    const count = document.createElement('span');
    count.className = 'tour-count';
    count.textContent = `${i + 1} / ${STEPS.length}`;
    head.append(count, button('×', 'icon-btn tour-close', close));
    head.lastChild.setAttribute('aria-label', 'End the guide');
    const title = document.createElement('h2');
    title.id = 'tour-title';
    title.textContent = step.title;
    const text = document.createElement('p');
    text.textContent = step.text;
    const bar = document.createElement('div');
    bar.className = 'tour-progress';
    bar.style.setProperty('--done', `${((i + 1) / STEPS.length) * 100}%`);
    const actions = document.createElement('div');
    actions.className = 'tour-actions';
    if (i > 0) actions.append(button('Back', 'btn quiet', () => go(i - 1)));
    const next = button(i === STEPS.length - 1 ? 'Finish' : 'Next', 'btn primary', () => go(i + 1));
    actions.append(next);
    box.replaceChildren(head, title, text, bar, actions);
    if (target) target.scrollIntoView({ block: 'center' });
    requestAnimationFrame(() => { place(); next.focus({ preventScroll: true }); });
  }

  const start = () => go(0);
  document.addEventListener('click', event => {
    const trigger = event.target.closest('[data-tour-start]');
    if (trigger) { event.preventDefault(); trigger.closest('details')?.removeAttribute('open'); start(); }
  });

  const saved = store.get();
  const params = new URLSearchParams(location.search);
  if (params.get('tour') === '1') {
    start();
  } else if (saved !== null) {
    const i = Number(saved);
    // Resume only on the page the tour sent us to; any other navigation ends the tour quietly.
    if (STEPS[i] && STEPS[i].path === location.pathname) show(i); else store.set(null);
  }
})();
