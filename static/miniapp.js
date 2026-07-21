(() => {
  'use strict';

  const tg = window.Telegram?.WebApp;
  const initData = tg?.initData || '';
  const state = {
    section: 'dashboard',
    loaded: new Set(),
    offerStatus: 'all',
    offerQuery: '',
    offerPage: 1,
    offerPages: 1,
    selectionMode: false,
    selectedOffers: new Set(),
    currentOffer: null,
    currentChat: null,
    currentChatInfo: null,
    chats: [],
    chatQuery: '',
    chatLabel: 'all',
    dashboardPeriod: '30d',
    dashboardStart: '',
    dashboardEnd: '',
    dashboardProduct: '',
    attention: {count: 0, items: [], summary: {}},
    recent: [],
    searchTimer: null,
    currentOrder: null,
    replacementPreview: null,
    cases: [],
    caseStatus: 'all',
    knowledge: [],
    };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const esc = (value) => String(value ?? '—').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
  const money = (value, currency = 'RUB') => {
    const number = Number(value || 0);
    try { return new Intl.NumberFormat('ru-RU', {style:'currency', currency: currency || 'RUB', maximumFractionDigits: 2}).format(number); }
    catch { return `${number.toLocaleString('ru-RU')} ${currency || '₽'}`; }
  };
  const formatDate = (value) => {
    if (!value) return '—';
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString('ru-RU', {dateStyle:'short', timeStyle:'short'});
  };
  const getAny = (obj, keys, fallback = '') => {
    for (const key of keys) if (obj && obj[key] !== undefined && obj[key] !== null && obj[key] !== '') return obj[key];
    return fallback;
  };

  function toast(message, error = false) {
    const node = $('#toast');
    node.textContent = message;
    node.style.background = error ? 'var(--danger)' : 'var(--text)';
    node.style.color = error ? '#fff' : 'var(--bg)';
    node.classList.remove('hidden');
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.add('hidden'), 3200);
    if (tg?.HapticFeedback) error ? tg.HapticFeedback.notificationOccurred('error') : tg.HapticFeedback.notificationOccurred('success');
  }

  async function api(path, options = {}) {
    if (!initData) throw new Error('Панель необходимо открыть кнопкой из Telegram-бота');
    const response = await fetch(path, {
      ...options,
      headers: {
        'Authorization': `tma ${initData}`,
        'Content-Type': 'application/json',
        ...(options.headers || {}),
      },
    });
    let payload;
    try { payload = await response.json(); } catch { payload = {ok:false, error:`HTTP ${response.status}`}; }
    if (!response.ok || !payload.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    return payload;
  }

  function confirmAction(message) {
    return new Promise(resolve => {
      if (tg?.showConfirm) tg.showConfirm(message, resolve);
      else resolve(window.confirm(message));
    });
  }

  function openDialog(dialog) {
    dialog.showModal();
    tg?.HapticFeedback?.impactOccurred('light');
  }
  function closeDialog(dialog) { if (dialog.open) dialog.close(); }

  function applyPreferences() {
    const compact = localStorage.getItem('ggselCompactMode') === '1';
    const hideBalance = localStorage.getItem('ggselHideBalance') === '1';
    document.body.classList.toggle('compact-mode', compact);
    document.body.classList.toggle('hide-balance', hideBalance);
    const compactToggle = $('#compactModeToggle');
    const hideToggle = $('#hideBalanceToggle');
    if (compactToggle) compactToggle.checked = compact;
    if (hideToggle) hideToggle.checked = hideBalance;
  }

  function statusLabel(status) {
    return ({active:'Активен', paused:'Приостановлен', archived:'В архиве', unknown:'Неизвестно'})[status] || status || 'Неизвестно';
  }

  async function switchSection(name) {
    state.section = name;
    $$('.tab').forEach(tab => tab.classList.toggle('active', tab.dataset.section === name));
    $$('.section').forEach(section => section.classList.toggle('active', section.id === `section-${name}`));
    if (!state.loaded.has(name)) {
      state.loaded.add(name);
      try {
        if (name === 'dashboard') await loadDashboard();
        if (name === 'offers') await loadOffers();
        if (name === 'orders') await loadSales();
        if (name === 'finance') await loadFinance();
        if (name === 'chats') await loadChats();
        if (name === 'reviews') await loadReviews();
        if (name === 'categories') await loadCategories();
        if (name === 'audit') await loadAudit();
        if (name === 'operations') await loadOperations();
        if (name === 'inventory') await loadInventoryTools();
        if (name === 'workspace') await loadWorkspace();
        if (name === 'health') await loadHealth();
        if (name === 'cases') await loadCases();
        if (name === 'supply') await loadSupply();
        if (name === 'knowledge') await loadKnowledge();
        if (name === 'quality') await loadQuality();
        if (name === 'more') return;
      } catch (error) { toast(error.message, true); }
    }
    if (name === 'dashboard' && state.loaded.has(name)) Promise.allSettled([loadAttention(false), loadRecent()]);
    window.scrollTo({top:0, behavior:'smooth'});
  }

  async function loadIdentity() {
    const {data} = await api('/app/api/me');
    $('#ownerName').textContent = data.first_name ? `${data.first_name} · ID ${data.seller_id || '—'}` : '';
  }

  function attentionItemMarkup(item) {
    const icon = item.kind === 'low_stock' ? '!' : item.kind === 'chat' ? '✉' : '↻';
    return `<button class="attention-item plain-button severity-${esc(item.severity || 'info')}" data-attention-action="${esc(item.action || '')}" data-entity-id="${esc(item.entity_id || '')}">
      <span class="attention-icon">${icon}</span><div><strong>${esc(item.title)}</strong><span class="muted">${esc(item.subtitle || '')}</span></div><span class="support-arrow">→</span>
    </button>`;
  }

  async function loadAttention(open = false) {
    const {data} = await api('/app/api/attention');
    state.attention = data || {count:0, items:[], summary:{}};
    const count = Number(state.attention.count || 0);
    const badge = $('#attentionBadge');
    badge.textContent = count > 99 ? '99+' : String(count);
    badge.classList.toggle('hidden', count <= 0);
    const markup = state.attention.items?.length ? state.attention.items.map(attentionItemMarkup).join('') : '<div class="empty">Всё спокойно — срочных действий нет</div>';
    $('#dashboardAttention').innerHTML = markup;
    $('#attentionList').innerHTML = markup;
    const summary = state.attention.summary || {};
    $('#attentionSummary').innerHTML = [
      ['Остатки', summary.low_stock || 0], ['Новые чаты', summary.new_chats || 0], ['Замены', summary.replacement || 0], ['Операции', summary.operations || 0],
    ].map(([label,value]) => `<div class="metric-card compact-card"><span class="label">${esc(label)}</span><span class="value">${Number(value).toLocaleString('ru-RU')}</span></div>`).join('');
    if (open) openDialog($('#attentionDialog'));
  }

  async function loadRecent() {
    const {data} = await api('/app/api/recent?limit=8');
    state.recent = data || [];
    $('#dashboardRecent').innerHTML = state.recent.length ? state.recent.map(item => {
      const label = item.type === 'offer' ? 'Товар' : item.type === 'order' ? 'Заказ' : 'Чат';
      return `<button class="list-item plain-button recent-item" data-recent-type="${esc(item.type)}" data-recent-id="${esc(item.id)}"><div><strong>${esc(item.title || `${label} ${item.id}`)}</strong><span class="muted">${label} · ${formatDate(item.viewed_at)}</span></div><span class="support-arrow">→</span></button>`;
    }).join('') : '<div class="empty">Откройте товар, заказ или чат — они появятся здесь</div>';
  }

  const SEARCH_GROUPS = [
    ['offers', 'Товары', '▦'],
    ['orders', 'Заказы', '#'],
    ['chats', 'Клиенты и переписки', '✉'],
    ['inventory', 'Ключи и содержимое', '⌁'],
    ['suppliers', 'Поставщики', '◫'],
    ['batches', 'Партии', '▤'],
    ['cases', 'Проблемные обращения', '⚑'],
    ['knowledge', 'База знаний', '◎'],
    ['notes', 'Заметки', '✎'],
    ['errors', 'Ошибки API', '!'],
  ];

  function renderSearchGroup(title, items, icon = '⌕') {
    if (!items?.length) return '';
    return `<section class="search-group"><div class="search-group-heading"><h3><span>${esc(icon)}</span>${esc(title)}</h3><span class="search-count">${items.length}</span></div>${items.map(item => `<button class="search-result plain-button" data-search-type="${esc(item.type)}" data-search-id="${esc(item.id)}" data-search-invoice="${esc(item.invoice_id || '')}" data-search-offer="${esc(item.offer_id || '')}" data-search-conversation="${esc(item.conversation_id || '')}" data-search-query="${esc(item.title || '')}"><div class="search-result-main"><div class="search-result-title"><strong>${esc(item.title)}</strong>${item.badge ? `<span class="badge search-badge">${esc(item.badge)}</span>` : ''}</div><span class="muted">${esc(item.subtitle || '')}</span></div><span class="support-arrow">→</span></button>`).join('')}</section>`;
  }

  function readRecentSearches() {
    try { const value=JSON.parse(localStorage.getItem('ggselRecentSearches') || '[]'); return Array.isArray(value) ? value : []; }
    catch { return []; }
  }

  function rememberSearch(query) {
    const clean = String(query || '').trim();
    if (clean.length < 2) return;
    const old = readRecentSearches();
    localStorage.setItem('ggselRecentSearches', JSON.stringify([clean, ...old.filter(item => item !== clean)].slice(0, 6)));
  }

  function renderRecentSearches() {
    const recent = readRecentSearches();
    const node = $('#globalSearchRecent');
    if (!node) return;
    node.innerHTML = recent.length ? `<span class="search-example-label">Недавние:</span>${recent.map(value => `<button type="button" class="search-example" data-search-example="${esc(value)}">${esc(value)}</button>`).join('')}` : '';
  }

  function renderSearchMeta(meta = {}) {
    const node = $('#globalSearchMeta');
    if (!node) return;
    const filters = (meta.filters || []).map(item => `<span class="search-filter-chip"><b>${esc(item.label)}:</b> ${esc(item.value)}</span>`).join('');
    const terms = (meta.terms || []).map(term => `<span class="search-filter-chip">${esc(term)}</span>`).join('');
    const unknown = (meta.unknown || []).length ? `<div class="notice warning-notice compact-notice">Неизвестные команды восприняты как обычный текст: ${esc(meta.unknown.join(', '))}</div>` : '';
    const warnings = (meta.warnings || []).map(value => `<div class="notice warning-notice compact-notice">${esc(value)}</div>`).join('');
    node.innerHTML = `<div class="search-summary"><b>${Number(meta.total || 0).toLocaleString('ru-RU')}</b><span class="muted">результатов показано${meta.total >= (meta.limit || 12) ? ' · в каждой группе отобраны самые релевантные' : ''}</span></div>${filters || terms ? `<div class="search-active-filters">${terms}${filters}</div>` : ''}${unknown}${warnings}`;
  }

  async function runGlobalSearch(query) {
    const clean = String(query || '').trim();
    if (clean.length < 2) {
      $('#globalSearchMeta').innerHTML = '';
      $('#globalSearchResults').innerHTML = '<div class="empty">Введите минимум 2 символа или выберите пример команды</div>';
      return;
    }
    $('#globalSearchMeta').innerHTML = '<div class="search-summary"><span class="muted">Разбираю запрос…</span></div>';
    $('#globalSearchResults').innerHTML = '<div class="empty">Ищу по кабинету…</div>';
    try {
      const {data} = await api(`/app/api/search?q=${encodeURIComponent(clean)}&limit=14`);
      renderSearchMeta(data.meta || {});
      const html = SEARCH_GROUPS.map(([key,title,icon]) => renderSearchGroup(title, data[key], icon)).join('');
      $('#globalSearchResults').innerHTML = html || '<div class="empty">Ничего не найдено. Попробуйте убрать часть фильтров.</div>';
      rememberSearch(clean);
      renderRecentSearches();
    } catch (error) {
      $('#globalSearchMeta').innerHTML = '';
      $('#globalSearchResults').innerHTML = `<div class="notice">${esc(error.message)}</div>`;
    }
  }

  function applySearchExample(value) {
    const input = $('#globalSearchInput');
    input.value = value;
    input.focus();
    runGlobalSearch(value);
  }

  function openGlobalSearch() {
    renderRecentSearches();
    openDialog($('#globalSearchDialog'));
    setTimeout(() => $('#globalSearchInput').focus(), 80);
  }


  async function toggleFavorite(id, favorite) {
    try {
      await api(`/app/api/offers/${id}/favorite`, {method:'PUT', body:JSON.stringify({favorite})});
      toast(favorite ? 'Добавлено в избранное' : 'Удалено из избранного');
      if ($('#offerDialog').open) await showOffer(id);
      if (state.section === 'offers') await loadOffers();
    } catch (error) { toast(error.message, true); }
  }

  async function handleNavigationItem(type, id, invoice = '', query = '', offer = '', conversation = '') {
    closeDialog($('#globalSearchDialog'));
    closeDialog($('#attentionDialog'));
    if (type === 'offer') { await switchSection('offers'); await showOffer(id); return; }
    if (type === 'order' || type === 'order_note') { await switchSection('orders'); $('#invoiceInput').value = id || invoice; await findOrder(id || invoice); return; }
    if (type === 'chat' || type === 'customer_note') { await switchSection('chats'); await openChat(conversation || id); return; }
    if (type === 'inventory') {
      if (invoice) { await switchSection('orders'); $('#invoiceInput').value = invoice; await findOrder(invoice); }
      else if (offer) { await switchSection('offers'); await showOffer(offer); }
      else { await switchSection('inventory'); }
      return;
    }
    if (type === 'supplier' || type === 'batch') { await switchSection('supply'); setTimeout(() => document.querySelector('#section-supply')?.scrollIntoView({behavior:'smooth'}), 80); return; }
    if (type === 'case') {
      await switchSection('cases');
      $('#caseSearch').value = invoice || query || id;
      await loadCases();
      return;
    }
    if (type === 'knowledge') {
      await switchSection('knowledge');
      $('#knowledgeSearch').value = query || id;
      await loadKnowledge(query || id);
      return;
    }
    if (type === 'error') { await switchSection('health'); return; }
  }


  function localDateValue(value) {
    const date = value instanceof Date ? value : new Date(value);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000);
    return local.toISOString().slice(0, 10);
  }

  function dashboardRange(period) {
    const end = new Date();
    const start = new Date(end);
    if (period === 'today') return [localDateValue(end), localDateValue(end)];
    if (period === 'yesterday') {
      start.setDate(start.getDate() - 1);
      return [localDateValue(start), localDateValue(start)];
    }
    if (period === '7d') start.setDate(start.getDate() - 6);
    else if (period === '30d') start.setDate(start.getDate() - 29);
    else if (period === '6m') { start.setMonth(start.getMonth() - 6); start.setDate(start.getDate() + 1); }
    return [localDateValue(start), localDateValue(end)];
  }

  function formatPeriodLabel(start, end) {
    if (!start || !end) return '';
    const a = new Date(`${start}T00:00:00`);
    const b = new Date(`${end}T00:00:00`);
    const options = {day:'2-digit', month:'short'};
    if (start === end) return a.toLocaleDateString('ru-RU', {day:'2-digit', month:'long', year:'numeric'});
    return `${a.toLocaleDateString('ru-RU', options)} — ${b.toLocaleDateString('ru-RU', {...options, year:'numeric'})}`;
  }

  function compactMoney(value, currency = 'USD') {
    const number = Number(value || 0);
    const compact = new Intl.NumberFormat('ru-RU', {notation:'compact', maximumFractionDigits:1}).format(number);
    return currency === 'USD' ? `$${compact}` : `${compact} ₽`;
  }

  function trendMarkup(value, suffix = '%') {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '<span class="metric-trend neutral">нет базы</span>';
    const number = Number(value);
    const cls = number > 0 ? 'up' : number < 0 ? 'down' : 'neutral';
    const arrow = number > 0 ? '↑' : number < 0 ? '↓' : '→';
    return `<span class="metric-trend ${cls}">${arrow} ${Math.abs(number).toLocaleString('ru-RU', {maximumFractionDigits:2})}${suffix}</span>`;
  }

  function renderVerticalChart(container, series, mode = 'money') {
    if (!series?.length) { container.innerHTML = '<div class="empty">Нет данных за период</div>'; return; }
    const max = Math.max(1, ...series.map(row => mode === 'orders' ? Number(row.count || 0) : Number(row.gross || 0)));
    container.innerHTML = `<div class="bar-chart-scroll"><div class="bar-chart-grid">${series.map(row => {
      const gross = Number(row.gross || 0);
      const received = Number(row.received || 0);
      const count = Number(row.count || 0);
      const mainHeight = Math.max(mode === 'orders' && count > 0 ? 5 : gross > 0 ? 5 : 0, (mode === 'orders' ? count : gross) / max * 100);
      const secondaryHeight = gross > 0 ? Math.min(mainHeight, received / max * 100) : 0;
      const title = mode === 'orders'
        ? `${row.label}: ${count} заказов`
        : `${row.label}: продажи ${money(gross,'USD')}, к зачислению ${money(received,'USD')}`;
      return `<div class="bar-column" title="${esc(title)}">
        <div class="bar-value">${mode === 'orders' ? esc(count) : compactMoney(gross, 'USD')}</div>
        <div class="bar-well">${mode === 'orders'
          ? `<i class="bar-single" style="height:${mainHeight}%"></i>`
          : `<i class="bar-gross" style="height:${mainHeight}%"></i><i class="bar-received" style="height:${secondaryHeight}%"></i>`}
        </div>
        <span>${esc(row.label)}</span>
      </div>`;
    }).join('')}</div></div>`;
  }

  function populateDashboardProducts(items) {
    const select = $('#dashboardProduct');
    const current = state.dashboardProduct;
    select.innerHTML = '<option value="">Все товары</option>' + (items || []).map(item => `<option value="${esc(item.id)}">${esc(item.title)}</option>`).join('');
    select.value = current;
  }

  function renderDashboardKpis(analytics) {
    const current = analytics.current || {};
    const delta = analytics.deltas || {};
    const cards = [
      ['Сумма продаж', money(current.gross, 'USD'), delta.gross, 'За выбранный период'],
      ['Продажи', Number(current.count || 0).toLocaleString('ru-RU'), delta.count, 'Оплаченных заказов'],
      ['К зачислению', money(current.received, 'USD'), delta.received, 'После комиссий'],
      ['Средний чек', money(current.average, 'USD'), delta.average, 'На одну продажу'],
    ];
    $('#dashboardKpis').innerHTML = cards.map(([label,value,change,hint], index) => `<article class="metric-card dashboard-metric ${index === 0 ? 'accent' : ''}">
      <div class="metric-top"><span class="label">${esc(label)}</span>${trendMarkup(change)}</div>
      <span class="value">${value}</span><small>${esc(hint)}</small>
    </article>`).join('');
  }

  async function loadDashboard(force = false) {
    if (!state.dashboardStart || !state.dashboardEnd) [state.dashboardStart, state.dashboardEnd] = dashboardRange(state.dashboardPeriod);
    $('#dashboardKpis').innerHTML = '<div class="metric-card"><span class="label">Загрузка</span><span class="value">…</span></div>'.repeat(4);
    $('#dashboardRevenueChart').innerHTML = '<div class="empty">Считаю продажи…</div>';
    $('#dashboardOrdersChart').innerHTML = '<div class="empty">Считаю заказы…</div>';
    const params = new URLSearchParams({start:state.dashboardStart, end:state.dashboardEnd});
    if (state.dashboardProduct) params.set('product_id', state.dashboardProduct);
    if (force) params.set('refresh', '1');
    const payload = await api(`/app/api/dashboard?${params}`);
    const {balance = {}, stats = {}, sales = [], analytics = {}, products = [], low_stock_items = [], support = {}} = payload.data || {};
    populateDashboardProducts(products);
    renderDashboardKpis(analytics);
    $('#dashboardPeriodLabel').textContent = formatPeriodLabel(analytics.start, analytics.end);
    $('#dashboardGrossTotal').textContent = money(analytics.current?.gross, 'USD');
    $('#dashboardOrdersTotal').textContent = `${Number(analytics.current?.count || 0).toLocaleString('ru-RU')} заказов`;
    renderVerticalChart($('#dashboardRevenueChart'), analytics.series || [], 'money');
    renderVerticalChart($('#dashboardOrdersChart'), analytics.series || [], 'orders');

    $('#dashboardInventoryCards').innerHTML = [
      ['Доступно', money(balance.amount_t_free, 'USD')],
      ['Ожидают зачисления', money(balance.amount_t_lock, 'USD')],
      ['Активных', stats.active ?? 0],
      ['На паузе', stats.paused ?? 0],
      ['Без остатка', stats.out_of_stock ?? 0],
      ['Заканчиваются', stats.low_stock ?? 0],
    ].map(([label,value]) => `<div class="metric-card compact-card"><span class="label">${esc(label)}</span><span class="value">${typeof value === 'number' ? value.toLocaleString('ru-RU') : value}</span></div>`).join('');

    const supportOpen = Number(support.new || 0) + Number(support.waiting || 0) + Number(support.replacement || 0);
    $('#dashboardSupport').innerHTML = `<button class="support-summary plain-button" data-goto="chats"><div><span class="label">Поддержка</span><strong>${supportOpen} активных диалогов</strong><small>${Number(support.messages_today || 0)} сообщений покупателей сегодня</small></div><span class="support-arrow">→</span></button>`;

    $('#dashboardTopProducts').innerHTML = (analytics.top_products || []).slice(0,6).map((item,index) => `<button class="list-item plain-button dashboard-product-link" data-product-id="${esc(item.id)}"><div><strong>${index+1}. ${esc(item.name)}</strong><span class="muted">${esc(item.count)} продаж · средний чек ${money(item.average,'USD')}</span></div><span class="amount">${money(item.gross,'USD')}</span></button>`).join('') || '<div class="empty">Нет продаж за период</div>';
    $('#dashboardLowStock').innerHTML = low_stock_items.map(item => `<button class="list-item plain-button dashboard-stock-link" data-offer-id="${esc(item.id)}"><div><strong>${esc(item.title)}</strong><span class="muted">Минимум: ${esc(item.min_stock)}</span></div><span class="badge warning">Остаток ${esc(item.quantity)}</span></button>`).join('') || '<div class="empty">Остатки в порядке</div>';
    renderSales(sales, $('#dashboardSales'), 8);

    const notes = [];
    if (!analytics.complete) notes.push('История чеков загружена не полностью. Для длинных периодов увеличьте RECEIPTS_MAX_PAGES в Railway.');
    if (analytics.api_limits?.note) notes.push(analytics.api_limits.note);
    const noteNode = $('#dashboardApiNote');
    if (notes.length) { noteNode.textContent = notes.join(' '); noteNode.classList.remove('hidden'); } else noteNode.classList.add('hidden');
    if (payload.errors && Object.keys(payload.errors).length) console.warn(payload.errors);
    await Promise.allSettled([loadAttention(false), loadRecent()]);
  }

  async function setDashboardPeriod(period) {
    state.dashboardPeriod = period;
    $$('#dashboardPeriods [data-dashboard-period]').forEach(button => button.classList.toggle('active', button.dataset.dashboardPeriod === period));
    const form = $('#dashboardDateForm');
    if (period === 'custom') { form.classList.remove('hidden'); return; }
    form.classList.add('hidden');
    [state.dashboardStart, state.dashboardEnd] = dashboardRange(period);
    $('#dashboardStart').value = state.dashboardStart;
    $('#dashboardEnd').value = state.dashboardEnd;
    await loadDashboard();
  }

  function renderSales(items, container, limit = 30) {
    if (!items?.length) { container.innerHTML = '<div class="empty">Продаж пока нет</div>'; return; }
    container.innerHTML = items.slice(0, limit).map(item => `
      <button class="list-item plain-button" data-invoice="${esc(item.invoice_id)}">
        <div><strong>${esc(item.name || 'Товар')}</strong><span class="muted">#${esc(item.invoice_id)} · ${formatDate(item.date)}</span></div>
        <span class="amount">${money(item.price_rub, 'RUB')}</span>
      </button>`).join('');
  }

  async function loadOffers(force = false) {
    $('#offersList').innerHTML = '<div class="empty">Загрузка товаров…</div>';
    const params = new URLSearchParams({status: state.offerStatus, q: state.offerQuery, page: state.offerPage, per_page: '30'});
    if (force) params.set('refresh', '1');
    const payload = await api(`/app/api/offers?${params}`);
    state.offerPages = payload.pagination?.pages || 1;
    renderOffers(payload.data || [], payload.pagination || {});
  }

  function renderOffers(items, pagination) {
    $('#offerCount').textContent = `${pagination.total ?? items.length} товаров`;
    $('#offersPage').textContent = `${pagination.page || 1} / ${pagination.pages || 1}`;
    $('#offersPager').classList.toggle('hidden', (pagination.pages || 1) <= 1);
    $('#offersPrev').disabled = state.offerPage <= 1;
    $('#offersNext').disabled = state.offerPage >= state.offerPages;
    if (!items.length) { $('#offersList').innerHTML = '<div class="empty">Ничего не найдено</div>'; return; }
    $('#offersList').innerHTML = items.map(item => {
      const selected = state.selectedOffers.has(String(item.id));
      const stockClass = Number(item.quantity) <= 0 ? 'archived' : '';
      return `<article class="offer-card" data-id="${esc(item.id)}">
        <div class="offer-head">
          ${state.selectionMode ? `<input class="offer-select" type="checkbox" ${selected ? 'checked' : ''} aria-label="Выбрать товар">` : ''}
          <div class="offer-main">
            <div class="offer-title-row"><div class="offer-title">${esc(item.title)}</div><button class="favorite-button ${item.favorite ? 'active' : ''}" data-action="favorite" aria-label="Избранное">★</button></div>
            <div class="offer-meta"><span class="badge ${esc(item.status)}">${esc(statusLabel(item.status))}</span><span class="badge ${stockClass}">Остаток: ${esc(item.quantity)}</span>${item.low_stock ? '<span class="badge warning">Заканчивается</span>' : ''}<span class="badge">ID ${esc(item.id)}</span></div>
          </div>
        </div>
        <div class="offer-bottom">
          <div><div class="offer-price">${money(item.price, item.currency)}</div><span class="muted">${esc(item.category || 'Без категории')}</span></div>
          <div class="offer-actions">
            <button class="secondary-button compact" data-action="details">Подробнее</button>
            <button class="primary-button compact" data-action="stock">＋ Ключи</button>
          </div>
        </div>
      </article>`;
    }).join('');
    updateBatchBar();
  }

  function updateBatchBar() {
    const count = state.selectedOffers.size;
    $('#batchBar').classList.toggle('hidden', !state.selectionMode || count === 0);
    $('#batchCount').textContent = `${count} выбрано`;
  }

  async function showOffer(id) {
    state.currentOffer = String(id);
    $('#offerDialogTitle').textContent = `Товар #${id}`;
    $('#offerDialogBody').innerHTML = '<div class="empty">Загрузка…</div>';
    openDialog($('#offerDialog'));
    try {
      const [offerResult, stockResult] = await Promise.all([
        api(`/app/api/offers/${id}`),
        api(`/app/api/offers/${id}/products?limit=50`).catch(error => ({data:[], stockError:error.message})),
      ]);
      const item = offerResult.data?.normalized || {};
      const raw = offerResult.data?.raw || {};
      const settings = offerResult.data?.settings || item.settings || {};
      const products = stockResult.data || [];
      $('#offerDialogTitle').textContent = item.title || `Товар #${id}`;
      $('#offerDialogBody').innerHTML = `
        <div class="offer-detail-head"><div class="offer-meta"><span class="badge ${esc(item.status)}">${esc(statusLabel(item.status))}</span><span class="badge">ID ${esc(item.id || id)}</span></div><button class="favorite-button large ${item.favorite ? 'active' : ''}" data-modal-action="favorite" aria-label="Избранное">★</button></div>
        <div class="detail-grid">
          <div class="detail"><span>Цена</span><b>${item.price === null || item.price === undefined ? '—' : money(item.price, item.currency)}</b></div>
          <div class="detail"><span>Остаток</span><b>${esc(item.quantity ?? '—')}</b></div>
          <div class="detail"><span>Продано</span><b>${esc(item.sold_products_count ?? '—')}</b></div>
          <div class="detail"><span>Автовыдача</span><b>${item.is_autoselling === true ? 'Да' : item.is_autoselling === false ? 'Нет' : '—'}</b></div>
        </div>
        <p class="muted">${esc(item.category || '')}</p>
        <p>${esc(getAny(raw, ['description_ru','info','description'], 'Описание не указано'))}</p>
        <div class="settings-card">
          <div class="panel-heading"><h3>Контроль остатков</h3><span class="badge">мин. ${esc(settings.min_stock ?? 3)}</span></div>
          <label>Минимальный остаток<input id="offerMinStock" type="number" min="0" value="${esc(settings.min_stock ?? 3)}"></label>
          <label class="switch-row"><span>Автоматически включать после пополнения</span><input id="offerAutoActivate" type="checkbox" ${settings.auto_activate ? 'checked' : ''}></label>
          <label class="switch-row"><span>Автоматически ставить на паузу при нуле</span><input id="offerAutoPause" type="checkbox" ${settings.auto_pause ? 'checked' : ''}></label>
          <button class="secondary-button full" data-modal-action="save-settings">Сохранить настройки</button>
        </div>
        <div class="offer-actions modal-actions">
          <button class="primary-button" data-modal-action="stock">＋ Добавить содержимое</button>
          <button class="secondary-button" data-modal-action="edit">Изменить</button>
          ${item.status === 'active' ? '<button class="secondary-button" data-modal-action="pause">Пауза</button>' : '<button class="success-button" data-modal-action="activate">Включить</button>'}
          <button class="danger-button" data-modal-action="delete">В архив</button>
        </div>
        <div class="panel-heading stock-heading"><h3>Содержимое (${products.length})</h3>${products.length ? '<button class="danger-button compact" data-modal-action="archive-all">Очистить склад</button>' : ''}</div>
        <div class="stock-list">${products.length ? products.map(product => `<div class="stock-row"><span>${esc(product.value)}</span><span>${esc(product.status || '')}</span></div>`).join('') : `<div class="empty">${esc(stockResult.stockError || 'Склад пуст')}</div>`}</div>`;
      $('#offerDialog').dataset.offer = JSON.stringify({item, raw, settings});
    } catch (error) {
      $('#offerDialogBody').innerHTML = `<div class="notice">${esc(error.message)}</div>`;
    }
  }

  function openStock(id, title = '') {
    state.currentOffer = String(id);
    $('#stockOfferLabel').textContent = `${title || 'Оффер'} · ID ${id}`;
    $('#stockValues').value = '';
    $('#stockCount').textContent = '0 строк';
    $('#stockWarnings').classList.add('hidden');
    $('#stockSupplier').value = '';
    $('#stockUnitCost').value = '';
    $('#stockCostCurrency').value = 'RUB';
    $('#stockFxRate').value = '1';
    $('#stockPurchasedAt').value = new Date().toISOString().slice(0,10);
    $('#stockWarrantyDays').value = '7';
    $('#stockBatchNotes').value = '';
    populateSupplierDatalist().catch(()=>{});
    let settings = {};
    try { settings = JSON.parse($('#offerDialog').dataset.offer || '{}').settings || {}; } catch {}
    $('#stockAutoActivate').checked = !!settings.auto_activate;
    openDialog($('#stockDialog'));
  }

  function parsedStockValues() {
    const all = $('#stockValues').value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
    return [...new Set(all)];
  }

  async function submitStock() {
    const values = parsedStockValues();
    if (!values.length) return toast('Добавьте хотя бы одну строку', true);
    const duplicates = $('#stockValues').value.split(/\r?\n/).map(v => v.trim()).filter(Boolean).length - values.length;
    const ok = await confirmAction(`Добавить ${values.length} позиций${duplicates ? ` (дубликатов удалено: ${duplicates})` : ''}?`);
    if (!ok) return;
    const button = $('#stockSubmit'); button.disabled = true; button.textContent = 'Добавляю…';
    try {
      const batch = {
        supplier_name: $('#stockSupplier').value.trim(),
        unit_cost: Number($('#stockUnitCost').value || 0),
        currency: $('#stockCostCurrency').value,
        fx_to_rub: Number($('#stockFxRate').value || 0),
        purchased_at: $('#stockPurchasedAt').value,
        warranty_days: Number($('#stockWarrantyDays').value || 0),
        notes: $('#stockBatchNotes').value.trim(),
      };
      const result = await api(`/app/api/offers/${state.currentOffer}/products`, {method:'POST', body:JSON.stringify({values, batch, auto_activate:$('#stockAutoActivate').checked, confirm:true})});
      toast(result.data.auto_activation ? `Добавлено: ${result.data.added}. Активация запущена` : `Добавлено: ${result.data.added}`);
      closeDialog($('#stockDialog'));
      closeDialog($('#offerDialog'));
      await loadOffers(true);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = 'Добавить содержимое'; }
  }

  async function saveOfferSettings(id) {
    const payload = {
      min_stock: Number($('#offerMinStock').value || 0),
      auto_activate: $('#offerAutoActivate').checked,
      auto_pause: $('#offerAutoPause').checked,
      confirm: true,
    };
    if (!await confirmAction('Сохранить настройки контроля остатков?')) return;
    try {
      await api(`/app/api/offers/${id}/settings`, {method:'PUT', body:JSON.stringify(payload)});
      toast('Настройки сохранены');
      await showOffer(id);
    } catch (error) { toast(error.message, true); }
  }

  async function offerAction(id, action) {
    const labels = {activate:'включить продажу', pause:'приостановить', delete:'перенести в архив'};
    if (!await confirmAction(`Точно ${labels[action]} для товара #${id}?`)) return;
    try {
      await api(`/app/api/offers/${id}/action`, {method:'POST', body:JSON.stringify({action, confirm:true})});
      toast(action === 'activate' ? 'Активация поставлена в очередь' : 'Операция запущена');
      closeDialog($('#offerDialog'));
      await loadOffers(true);
    } catch (error) { toast(error.message, true); }
  }

  async function batchAction(action) {
    const ids = [...state.selectedOffers].map(Number);
    if (!ids.length) return;
    if (!await confirmAction(`${action === 'activate' ? 'Включить' : action === 'pause' ? 'Приостановить' : 'Архивировать'} ${ids.length} товаров?`)) return;
    try {
      await api('/app/api/offers/batch-action', {method:'POST', body:JSON.stringify({action, offer_ids:ids, confirm:true})});
      toast('Массовая операция запущена');
      state.selectedOffers.clear(); state.selectionMode = false;
      await loadOffers(true);
    } catch (error) { toast(error.message, true); }
  }

  function openEditOffer() {
    const stored = JSON.parse($('#offerDialog').dataset.offer || '{}');
    const {item = {}, raw = {}} = stored;
    $('#editOfferId').value = item.id || state.currentOffer;
    $('#editTitle').value = getAny(raw, ['title_ru','name_goods','name'], item.title || '');
    $('#editPrice').value = item.price ?? '';
    $('#editCategory').value = item.category_id ?? '';
    $('#editAutoselling').checked = !!item.is_autoselling;
    $('#editDescription').value = getAny(raw, ['description_ru','info','description'], '');
    openDialog($('#editOfferDialog'));
  }

  async function saveOffer(event) {
    event.preventDefault();
    const id = $('#editOfferId').value;
    const patch = {
      title_ru: $('#editTitle').value.trim(),
      price: Number($('#editPrice').value),
      is_autoselling: $('#editAutoselling').checked,
      description_ru: $('#editDescription').value,
    };
    if ($('#editCategory').value) patch.category_id = Number($('#editCategory').value);
    if (!await confirmAction(`Сохранить изменения товара #${id}?`)) return;
    try {
      await api(`/app/api/offers/${id}`, {method:'PATCH', body:JSON.stringify({patch, confirm:true})});
      toast('Товар обновлён');
      closeDialog($('#editOfferDialog')); closeDialog($('#offerDialog'));
      await loadOffers(true);
    } catch (error) { toast(error.message, true); }
  }

  async function createOffer(event) {
    event.preventDefault();
    const data = {
      title_ru: $('#createTitle').value.trim(),
      price: Number($('#createPrice').value),
      currency: 'RUB',
      category_id: Number($('#createCategory').value),
      description_ru: $('#createDescription').value,
      instructions_ru: $('#createInstructions').value,
      is_autoselling: $('#createAutoselling').checked,
      delivery: 'auto', min_quantity: 1, max_quantity: 1, quantity: 0,
    };
    if (!await confirmAction(`Создать товар «${data.title_ru}»?`)) return;
    try {
      await api('/app/api/offers', {method:'POST', body:JSON.stringify({data, confirm:true})});
      toast('Товар создан');
      event.target.reset(); closeDialog($('#createOfferDialog'));
      await loadOffers(true);
    } catch (error) { toast(error.message, true); }
  }

  async function archiveAllStock(id) {
    if (!await confirmAction(`Удалить всё невыданное содержимое товара #${id}?`)) return;
    try {
      await api(`/app/api/offers/${id}/products`, {method:'DELETE', body:JSON.stringify({delete_all:true, confirm:true})});
      toast('Очистка склада запущена'); closeDialog($('#offerDialog')); await loadOffers(true);
    } catch (error) { toast(error.message, true); }
  }

  async function loadSales() {
    $('#salesList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/sales');
    renderSales(data || [], $('#salesList'));
  }

  async function findOrder(invoice) {
    $('#orderResult').innerHTML = '<div class="panel"><div class="empty">Загрузка…</div></div>';
    try {
      const {data} = await api(`/app/api/orders/${encodeURIComponent(invoice)}`);
      state.currentOrder = data;
      const buyer = data.buyer || {};
      const note = data.note || {};
      const profit = data.profitability || {};
      const problem = data.problem_case || {};
      const replacements = data.replacements || [];
      const netBlock = profit.net_profit_rub !== null && profit.net_profit_rub !== undefined
        ? `<div class="detail accent-detail"><span>Чистая прибыль</span><b>${money(profit.net_profit_rub,'RUB')}</b><small>маржа ${profit.margin_percent ?? '—'}%</small></div>`
        : `<div class="detail"><span>Чистая прибыль</span><b>—</b><small>${profit.cost_known ? 'задайте курс начислений' : 'себестоимость не привязана'}</small></div>`;
      $('#orderResult').innerHTML = `<article class="order-card panel" data-order-id="${esc(data.invoice_id)}" data-offer-id="${esc(data.item_id || '')}">
        <div class="panel-heading"><h3>${esc(data.name || 'Товар')}</h3><span class="badge">#${esc(data.invoice_id)}</span></div>
        <div class="detail-grid">
          <div class="detail"><span>Зачислено</span><b>${money(data.amount, data.currency || 'RUB')}</b></div>
          <div class="detail"><span>До себестоимости</span><b>${money(data.profit, data.currency || 'RUB')}</b></div>
          <div class="detail"><span>Себестоимость</span><b>${profit.cost_known ? money(profit.cost_rub,'RUB') : '—'}</b></div>
          ${netBlock}
          <div class="detail"><span>Статус</span><b>${esc(data.invoice_state_label || `Неизвестный статус (${data.invoice_state ?? '—'})`)}</b></div>
          <div class="detail"><span>Оплата</span><b>${formatDate(data.date_pay)}</b></div>
        </div>
        <p><b>Покупатель:</b> ${esc(buyer.email || buyer.account || '—')}</p>
        <p class="muted">Телефон: ${esc(buyer.phone || '—')} · ID товара: ${esc(data.item_id || '—')} · Внешний ID: ${esc(data.external_order_id || '—')}</p>
        ${problem.id ? `<div class="case-strip priority-${esc(problem.priority)}"><b>⚑ ${esc(caseStatusName(problem.status))}</b><span>${esc(problem.error_code || problem.reason || '')}</span><button class="link-button" data-edit-case="${problem.id}">Открыть</button></div>` : ''}
        ${replacements.length ? `<p class="muted">Замен выполнено: ${replacements.filter(x=>x.status==='sent').length}</p>` : ''}
        <div class="order-actions-grid">
          <button class="secondary-button" data-order-diagnostic>⌁ Диагностика</button>
          <button class="secondary-button" data-order-case>⚑ Проблема</button>
          <button class="danger-button" data-order-replacement>↺ Заменить содержимое</button>
        </div>
        <div class="order-note-box">
          <div class="panel-heading"><h3>Моя заметка</h3><span class="muted">видна только тебе</span></div>
          <label>Метка<select id="orderNoteTag"><option value="">Без метки</option><option value="check">Проверить</option><option value="replacement">Замена</option><option value="waiting">Ждём клиента</option><option value="vip">VIP</option><option value="resolved">Решено</option></select></label>
          <label>Комментарий<textarea id="orderNoteText" rows="3" placeholder="Что важно помнить по этому заказу">${esc(note.note || '')}</textarea></label>
          <button class="secondary-button full" data-save-order-note>Сохранить заметку</button>
        </div>
      </article>`;
      $('#orderNoteTag').value = note.tag || '';
    } catch (error) { $('#orderResult').innerHTML = `<div class="notice">${esc(error.message)}</div>`; }
  }

  async function saveOrderNote() {
    const card = $('#orderResult [data-order-id]');
    if (!card) return;
    const invoice = card.dataset.orderId;
    try {
      await api(`/app/api/orders/${encodeURIComponent(invoice)}/note`, {method:'PUT', body:JSON.stringify({tag:$('#orderNoteTag').value, note:$('#orderNoteText').value})});
      toast('Заметка сохранена');
    } catch (error) { toast(error.message, true); }
  }

  async function loadFinance() {
    const today = new Date();
    const start = new Date(today); start.setDate(today.getDate() - 29);
    $('#revenueEnd').value = today.toISOString().slice(0,10);
    $('#revenueStart').value = start.toISOString().slice(0,10);
    await Promise.all([loadBalance(), loadReceipts()]);
    await calculateRevenue({preventDefault(){}});
  }

  async function loadBalance() {
    const {data} = await api('/app/api/balance');
    $('#balanceCards').innerHTML = [
      ['Доступно', money(data.amount_t_free, 'USD'), true], ['Заблокировано', money(data.amount_t_lock, 'USD')], ['С ограничением', money(data.amount_t_plus, 'USD')]
    ].map(([label,value,accent]) => `<div class="metric-card ${accent?'accent':''}"><span class="label">${label}</span><span class="value">${esc(value)}</span></div>`).join('');
  }

  async function loadReceipts() {
    $('#receiptsList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/receipts?count=30');
    if (!data?.length) { $('#receiptsList').innerHTML = '<div class="empty">Чеков нет</div>'; return; }
    $('#receiptsList').innerHTML = data.map(item => {
      const op = item.operation || {}; const product = item.product || {};
      const productName = typeof product.name === 'string' ? product.name : (product.name?.[0]?.value || 'Операция');
      return `<div class="list-item"><div><strong>${esc(productName)}</strong><span class="muted">${formatDate(op.datetime)} · ${esc(op.type || '')}</span></div><div class="amount">${money(op.on_account ?? op.price ?? 0, 'USD')}</div></div>`;
    }).join('');
  }

  async function calculateRevenue(event) {
    event?.preventDefault?.();
    $('#revenueResult').innerHTML = '<div class="empty">Считаю…</div>';
    try {
      const params = new URLSearchParams({start:$('#revenueStart').value, end:$('#revenueEnd').value});
      const {data} = await api(`/app/api/analytics?${params}`);
      $('#revenueResult').innerHTML = `<div class="detail-grid"><div class="detail"><span>Получено</span><b>${money(data.received,'USD')}</b></div><div class="detail"><span>Оборот</span><b>${money(data.gross,'USD')}</b></div><div class="detail"><span>Продаж</span><b>${esc(data.count)}</b></div><div class="detail"><span>Средний чек</span><b>${money(data.average,'USD')}</b></div></div><p class="muted">Данные: ${data.complete ? 'полные' : 'частичные'}</p>`;
      const max = Math.max(1, ...(data.daily || []).map(x => Number(x.received || 0)));
      $('#financeDaily').innerHTML = (data.daily || []).map(row => `<div class="chart-row"><span>${esc(row.date.slice(5))}</span><div class="chart-track"><i style="width:${Math.max(2, Number(row.received || 0) / max * 100)}%"></i></div><b>${money(row.received,'USD')}</b></div>`).join('') || '<div class="empty">Нет продаж за период</div>';
      $('#financeTopProducts').innerHTML = (data.top_products || []).map((item,index) => `<div class="list-item"><div><strong>${index+1}. ${esc(item.name)}</strong><span class="muted">${esc(item.count)} продаж</span></div><span class="amount">${money(item.received,'USD')}</span></div>`).join('') || '<div class="empty">Нет данных</div>';
    } catch (error) { $('#revenueResult').innerHTML = `<div class="notice">${esc(error.message)}</div>`; }
  }

  function applyFinancePeriod(period) {
    const end = new Date();
    const start = new Date(end);
    if (period === 'today') start.setTime(end.getTime());
    if (period === '7d') start.setDate(end.getDate() - 6);
    if (period === '30d') start.setDate(end.getDate() - 29);
    if (period === 'month') start.setDate(1);
    $('#revenueStart').value = start.toISOString().slice(0,10);
    $('#revenueEnd').value = end.toISOString().slice(0,10);
    calculateRevenue({preventDefault(){}});
  }


  function chatLabelName(label) {
    return ({new:'Новый', waiting:'Ждём клиента', replacement:'Замена', resolved:'Решено'})[label] || 'Новый';
  }

  function renderChats() {
    const query = state.chatQuery.toLowerCase();
    const chatTimestamp = item => {
      const raw = item.last_message_iso || item.last_message || item.date || item.date_written || item.message_date || item.created_at || '';
      if (!raw) return 0;
      if (/^\d+(?:\.\d+)?$/.test(String(raw))) {
        let value = Number(raw);
        if (value < 100000000000) value *= 1000;
        return Number.isFinite(value) ? value : 0;
      }
      const normalized = String(raw)
        .replace(/^(\d{2})[./](\d{2})[./](\d{4})(?:[ ,T]+(\d{2}):(\d{2})(?::(\d{2}))?)?$/, (_,d,m,y,h='00',min='00',sec='00') => `${y}-${m}-${d}T${h}:${min}:${sec}Z`);
      const value = new Date(normalized).getTime();
      return Number.isNaN(value) ? 0 : value;
    };
    const items = (state.chats || []).filter(item => {
      if (state.chatLabel !== 'all' && (item.label || 'new') !== state.chatLabel) return false;
      if (!query) return true;
      return [item.email,item.invoice_id,item.product_name,item.preview,item.id_i].some(value => String(value || '').toLowerCase().includes(query));
    }).sort((a, b) => chatTimestamp(b) - chatTimestamp(a));
    if (!items.length) { $('#chatsList').innerHTML = '<div class="empty">Чаты не найдены</div>'; return; }
    $('#chatsList').innerHTML = items.map(item => {
      const id = getAny(item, ['conversation_id','id_i','debate_id','id','invoice_id']);
      const title = getAny(item, ['product_name','name','subject','email'], item.invoice_id ? `Заказ #${item.invoice_id}` : `Диалог #${id}`);
      const preview = getAny(item, ['preview','message','text'], 'Открыть переписку');
      const unread = Number(item.cnt_new || 0);
      const meta = [item.email, item.invoice_id ? `заказ #${item.invoice_id}` : '', formatDate(item.last_message)].filter(Boolean).join(' · ');
      return `<button class="chat-card plain-button" data-chat="${esc(id)}"><div class="panel-heading"><strong>${item.pinned ? '📌 ' : ''}${item.favorite ? '★ ' : ''}${esc(title)}</strong><span class="badge">${Number(item.debate_count||1)>1 ? `${esc(item.debate_count)} ветки` : `#${esc(item.id_i||id)}`}${unread > 0 ? ` · ${unread} новых` : ''}</span></div><p>${esc(typeof preview === 'object' ? JSON.stringify(preview) : preview)}</p><div class="chat-card-footer"><span class="badge chat-${esc(item.label || 'new')}">${esc(chatLabelName(item.label || 'new'))}</span><span class="muted">${esc(meta)}</span></div></button>`;
    }).join('');
  }

  async function loadChatStatus() {
    try {
      const {data} = await api('/app/api/chats/status');
      const last = data.last_webhook_message;
      const lastDate = last ? formatDate(last.message_date || last.created_at) : 'ещё не было';
      const lastMs = last ? new Date(last.message_date || last.created_at).getTime() : NaN;
      const stale = !last || Number.isNaN(lastMs) || (Date.now() - lastMs > 24*60*60*1000);
      $('#chatStatus').className = `notice ${stale ? 'warning-notice' : 'success-notice'}`;
      $('#chatStatus').innerHTML = `<b>Webhook: ${stale ? 'нужно проверить' : 'работает'}</b><br>Последнее входящее: ${esc(lastDate)} · Локальных диалогов: ${esc(data.local_chats)}<br><small>${esc(data.note)}</small><button id="copyWebhookUrl" class="link-button">Скопировать URL webhook</button>`;
      $('#copyWebhookUrl')?.addEventListener('click', async () => { await navigator.clipboard.writeText(data.webhook_url); toast('URL скопирован'); });
    } catch (error) { $('#chatStatus').innerHTML = `<b>Не удалось проверить webhook:</b> ${esc(error.message)}`; }
  }

  async function loadChats(query = state.chatQuery) {
    $('#chatsList').innerHTML = '<div class="empty">Загрузка…</div>';
    await loadChatStatus();
    const params = new URLSearchParams();
    if (query && query.trim().length >= 2) params.set('query', query.trim());
    const {data} = await api(`/app/api/chats${params.toString() ? `?${params}` : ''}`);
    state.chats = data || [];
    renderChats();
  }

  function renderCustomerPanel(conv) {
    const profile = conv.profile || {};
    const orders = conv.orders || [];
    $('#customerStats').innerHTML = `
      <div class="customer-stat"><span>Заказов</span><b>${Number(conv.orders_count || 0)}</b></div>
      <div class="customer-stat"><span>Потрачено</span><b>${money(conv.total_spent || 0, conv.currency || 'RUB')}</b></div>
      <div class="customer-stat"><span>Диалогов GGSEL</span><b>${(conv.debate_ids || []).length}</b></div>`;
    $('#customerNote').value = profile.note || '';
    $('#customerTags').value = (profile.tags || []).join(', ');
    $('#customerPinned').checked = !!profile.pinned;
    $('#customerFavorite').checked = !!profile.favorite;
    $('#customerOrders').innerHTML = orders.length ? orders.slice(0,20).map(order => `
      <button class="customer-order plain-button" data-open-order="${esc(order.invoice_id)}">
        <div><strong>${esc(order.product_name || 'Товар')}</strong><span class="muted">Заказ #${esc(order.invoice_id)} · ${formatDate(order.date_pay || order.purchase_date)}</span></div>
        <b>${money(order.amount || 0, order.currency || 'RUB')}</b>
      </button>`).join('') : '<div class="empty">История покупок пока не собрана</div>';
  }

  async function openChat(id) {
    state.currentChat = String(id);
    state.currentChatInfo = state.chats.find(item => String(getAny(item,['conversation_id','id_i','debate_id','id'])) === String(id)) || {};
    $('#chatDialogTitle').textContent = state.currentChatInfo.product_name || state.currentChatInfo.email || 'Переписка с клиентом';
    $('#chatDialogMeta').innerHTML = `<b>${esc(state.currentChatInfo.email || '')}</b><span class="muted">${state.currentChatInfo.invoice_id ? `Последний заказ #${esc(state.currentChatInfo.invoice_id)} · ` : ''}${Number(state.currentChatInfo.debate_count||1)} веток GGSEL объединено</span>`;
    $$('.chat-label-actions [data-set-chat-label]').forEach(btn => btn.classList.toggle('active', btn.dataset.setChatLabel === (state.currentChatInfo.label || 'new')));
    $('#chatMessages').innerHTML = '<div class="empty">Загрузка…</div>';
    openDialog($('#chatDialog'));
    try {
      const {data} = await api(`/app/api/conversations/${encodeURIComponent(id)}`);
      const conv = data.conversation || {};
      state.currentChatInfo = {...state.currentChatInfo, ...conv};
      renderCustomerPanel(conv);
      const messages = data.messages || [];
      if (!messages.length) { $('#chatMessages').innerHTML = '<div class="empty">Сообщений нет</div>'; return; }
      $('#chatMessages').innerHTML = messages.map(item => {
        const seller = item.seller === true || Number(item.seller) === 1 || item.is_seller === true || Number(item.is_seller) === 1 || String(item.sender || '').toLowerCase() === 'seller';
        const body = getAny(item, ['message','text'], item.is_img ? '[Изображение]' : '[Файл]');
        const when = getAny(item, ['date_written','date','created_at']);
        const attachment = item.url ? `<a href="${esc(item.url)}" target="_blank" rel="noopener">Открыть вложение</a>` : '';
        const thread = item.debate_id ? `<em>ветка #${esc(item.debate_id)}</em>` : '';
        return `<div class="message ${seller ? 'seller' : ''}">${esc(body)}${attachment}<small>${thread}${formatDate(when)}</small></div>`;
      }).join('');
      $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;
    } catch (error) { $('#chatMessages').innerHTML = `<div class="notice">${esc(error.message)}</div>`; }
  }

  async function setChatLabel(label) {
    if (!state.currentChat) return;
    try {
      await api(`/app/api/conversations/${encodeURIComponent(state.currentChat)}/label`, {method:'PUT', body:JSON.stringify({label, confirm:true})});
      const item = state.chats.find(x => String(getAny(x,['conversation_id','id_i','debate_id','id'])) === String(state.currentChat));
      if (item) item.label = label;
      state.currentChatInfo = item || state.currentChatInfo;
      $$('.chat-label-actions [data-set-chat-label]').forEach(btn => btn.classList.toggle('active', btn.dataset.setChatLabel === label));
      renderChats(); toast('Метка сохранена');
    } catch (error) { toast(error.message, true); }
  }


  async function sendChatReply(event) {
    event.preventDefault();
    const message = $('#chatReplyText').value.trim();
    if (!message) return;
    if (!await confirmAction('Отправить сообщение покупателю?')) return;
    try {
      await api(`/app/api/conversations/${encodeURIComponent(state.currentChat)}/messages`, {method:'POST', body:JSON.stringify({message, confirm:true})});
      $('#chatReplyText').value = ''; toast('Сообщение отправлено'); await openChat(state.currentChat);
    } catch (error) { toast(error.message, true); }
  }

  async function loadReviews() {
    $('#reviewsList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/reviews');
    if (!data?.length) { $('#reviewsList').innerHTML = '<div class="empty">Отзывов нет</div>'; return; }
    $('#reviewsList').innerHTML = data.map(item => {
      const rating = getAny(item, ['rating_label','rating','feedback_type','type'], 'Отзыв');
      const text = getAny(item, ['text','info','review','feedback','message'], 'Без текста');
      const product = getAny(item, ['product_name','name_goods','name'], 'Товар');
      const badgeClass = item.is_positive === false ? 'danger' : 'active';
      const comment = getAny(item, ['seller_comment','comment'], '');
      return `<article class="review-card"><div class="panel-heading"><strong>${esc(product)}</strong><span class="badge ${badgeClass}">★ ${esc(rating)}</span></div><p>${esc(text || 'Без текста')}</p>${comment ? `<p class="muted"><b>Ваш ответ:</b> ${esc(comment)}</p>` : ''}<span class="muted">Заказ #${esc(item.invoice_id || '—')} · ${formatDate(getAny(item,['date','created_at','date_written']))}</span></article>`;
    }).join('');
  }

  async function loadCategories(query = '') {
    $('#categoriesList').innerHTML = '<div class="empty">Загрузка…</div>';
    const params = query ? `?q=${encodeURIComponent(query)}` : '';
    const {data} = await api(`/app/api/categories${params}`);
    if (!data?.length) { $('#categoriesList').innerHTML = '<div class="empty">Категории не найдены</div>'; return; }
    $('#categoriesList').innerHTML = data.slice(0,200).map(item => `<div class="list-item"><div><strong>${esc(item.title)}</strong><span class="muted">ID категории</span></div><span class="badge code">${esc(item.id)}</span></div>`).join('');
  }

  async function loadOperations(force = false) {
    $('#operationsList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api(`/app/api/operations${force ? '?refresh=1' : ''}`);
    if (!data?.length) { $('#operationsList').innerHTML = '<div class="empty">Операций пока нет</div>'; return; }
    const labels = {queued:'В очереди',running:'Выполняется',completed:'Готово',failed:'Ошибка'};
    $('#operationsList').innerHTML = data.map(item => `<article class="operation-card"><div class="panel-heading"><strong>${esc(item.operation)}</strong><span class="badge op-${esc(item.status)}">${esc(labels[item.status] || item.status)}</span></div><p>Цель: ${esc(item.target || '—')}</p><span class="muted">Job: ${esc(item.job_id || '—')} · ${formatDate(item.updated_at || item.created_at)}</span></article>`).join('');
  }

  async function exportOffers() {
    try {
      const {data} = await api('/app/api/export/offers');
      const blob = new Blob([JSON.stringify(data, null, 2)], {type:'application/json'});
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = `ggsel-offers-${new Date().toISOString().slice(0,10)}.json`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000); toast('Экспорт готов');
    } catch (error) { toast(error.message, true); }
  }

  async function loadAudit() {
    $('#auditList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/audit');
    if (!data?.length) { $('#auditList').innerHTML = '<div class="empty">Действий пока нет</div>'; return; }
    $('#auditList').innerHTML = data.map(item => `<div class="list-item"><div><strong>${esc(item.action)}</strong><span class="muted">${formatDate(item.created_at)}</span></div><span class="badge">${esc(item.target || '—')}</span></div>`).join('');
  }

  async function globalRefresh() {
    state.loaded.delete(state.section);
    if (state.section === 'dashboard') await loadDashboard(true);
    else if (state.section === 'offers') await loadOffers(true);
    else if (state.section === 'orders') await loadSales();
    else if (state.section === 'finance') await loadFinance();
    else if (state.section === 'chats') await loadChats();
    else if (state.section === 'reviews') await loadReviews();
    else if (state.section === 'categories') await loadCategories();
    else if (state.section === 'operations') await loadOperations(true);
    else if (state.section === 'audit') await loadAudit();
    state.loaded.add(state.section);
    toast('Данные обновлены');
  }


  async function loadInventoryTools() {
    const {data} = await api('/app/api/inventory/history?limit=100');
    $('#inventoryHistory').innerHTML = data.length ? data.map(x => `<div class="list-item"><div><strong>${esc(x.product_name || `Товар ${x.offer_id || '—'}`)}</strong><span class="muted">${esc(x.content_masked)} · ${esc(x.status)}${x.invoice_id ? ` · заказ #${esc(x.invoice_id)}` : ''}</span></div><span>${formatDate(x.sold_at || x.added_at)}</span></div>`).join('') : '<div class="empty">История пока пуста. Новые загрузки начнут индексироваться автоматически.</div>';
  }

  async function searchSoldContent() {
    const q = $('#soldContentQuery').value.trim();
    if (!q) return toast('Вставьте содержимое', true);
    const {data} = await api(`/app/api/inventory/search?q=${encodeURIComponent(q)}`);
    $('#soldContentResults').innerHTML = data.length ? data.map(x => `<button class="list-item plain-button" data-open-order="${esc(x.invoice_id || '')}"><div><strong>${esc(x.product_name || 'Товар')}</strong><span class="muted">${esc(x.content_masked)} · ${esc(x.status)} · заказ #${esc(x.invoice_id || '—')}</span></div><span class="support-arrow">→</span></button>`).join('') : '<div class="empty">Совпадений нет. Для старых продаж сначала выполните индексацию.</div>';
  }

  async function reindexSales() {
    if (!(await confirmAction('Индексировать содержимое последних 20 заказов?'))) return;
    const {data} = await api('/app/api/inventory/reindex',{method:'POST',body:JSON.stringify({confirmed:true,limit:20})});
    toast(`Проиндексировано позиций: ${data.indexed}`); await loadInventoryTools();
  }

  async function validateStockInput() {
    if (!state.currentOffer) return;
    const values=$('#stockValues').value.split(/\r?\n/).map(x=>x.trim()).filter(Boolean);
    if (!values.length) return;
    try {
      const {data}=await api('/app/api/inventory/validate',{method:'POST',body:JSON.stringify({offer_id:state.currentOffer.id,values})});
      $('#stockWarnings').classList.remove('hidden');
      $('#stockWarnings').innerHTML=`Готово: <b>${data.valid_count}</b> · дубли в файле: <b>${data.duplicates}</b> · уже известны: <b>${data.known.length}</b> · подозрительных: <b>${data.malformed.length}</b>`;
    } catch(e) { console.warn(e); }
  }

  async function loadWorkspace() {
    const [today,sla,recs,templates,rules,analytics]=await Promise.all([api('/app/api/today'),api('/app/api/sla'),api('/app/api/recommendations'),api('/app/api/templates'),api('/app/api/automations'),api('/app/api/product-analytics?days=30')]);
    const a=today.data.analytics||{};
    $('#todayWorkspace').innerHTML=[['Продаж сегодня',a.orders||a.count||0],['Без ответа',sla.data.unanswered],['Критичных',sla.data.critical],['Рекомендаций',recs.data.length]].map(([l,v])=>`<div class="metric-card"><span class="label">${l}</span><span class="value">${Number(v||0).toLocaleString('ru-RU')}</span></div>`).join('');
    renderSla(sla.data); renderRecommendations(recs.data); renderTemplates(templates.data); renderRules(rules.data); renderProductAnalytics(analytics.data);
  }
  function renderSla(data){ $('#slaSummary').innerHTML=[['Без ответа',data.unanswered],['30+ минут',data.warning],['60+ минут',data.critical]].map(([l,v])=>`<div class="metric-card compact-card"><span class="label">${l}</span><span class="value">${v}</span></div>`).join(''); $('#slaList').innerHTML=data.items.length?data.items.slice(0,20).map(x=>`<button class="list-item plain-button severity-${x.severity}" data-open-chat="${esc(x.debate_id)}"><div><strong>Диалог #${esc(x.debate_id)}</strong><span class="muted">${x.minutes} мин без ответа · заказ #${esc(x.invoice_id||'—')}</span></div><span>→</span></button>`).join(''):'<div class="empty">Нет диалогов без ответа</div>'; }
  function renderRecommendations(data){ $('#recommendationsList').innerHTML=data.length?data.map(x=>`<button class="list-item plain-button severity-${esc(x.severity)}" ${x.offer_id?`data-open-offer="${esc(x.offer_id)}"`:''}><div><strong>${esc(x.title||'Рекомендация')}</strong><span class="muted">${esc(x.text)}</span></div><span>→</span></button>`).join(''):'<div class="empty">Рекомендаций нет</div>'; }
  function renderTemplates(data){ $('#templatesList').innerHTML=data.length?data.map(x=>`<div class="list-item"><div><strong>${esc(x.name)}</strong><span class="muted">${esc(x.category)} · ${esc(x.body).slice(0,100)}</span></div><button class="danger-link" data-delete-template="${x.id}">Удалить</button></div>`).join(''):'<div class="empty">Шаблонов пока нет</div>'; }
  function renderRules(data){ $('#rulesList').innerHTML=data.length?data.map(x=>`<div class="list-item"><div><strong>${esc(x.name)}</strong><span class="muted">${esc(x.trigger_type)} · ${x.enabled?'включено':'выключено'}</span></div><div class="inline-actions"><button class="secondary-button small" data-toggle-rule="${x.id}" data-enabled="${x.enabled?0:1}">${x.enabled?'Выкл.':'Вкл.'}</button><button class="danger-link" data-delete-rule="${x.id}">Удалить</button></div></div>`).join(''):'<div class="empty">Правил пока нет</div>'; }
  function renderProductAnalytics(data){ $('#productAnalyticsList').innerHTML=data.length?data.slice(0,30).map(x=>`<div class="list-item"><div><strong>${esc(x.product_name)}</strong><span class="muted">${x.sales_count} продаж · ${x.daily_rate}/день</span></div><span>${money(x.revenue_rub||x.revenue_usd,x.revenue_rub?'RUB':'USD')}</span></div>`).join(''):'<div class="empty">Нет данных</div>'; }

  async function loadHealth(){ const [backups,errors,settings]=await Promise.all([api('/app/api/backups'),api('/app/api/errors'),api('/app/api/report-settings')]); renderBackups(backups.data); renderErrors(errors.data); $('#morningReportToggle').checked=!!settings.data.morning_enabled; $('#eveningReportToggle').checked=!!settings.data.evening_enabled; }
  function renderBackups(data){ $('#backupsList').innerHTML=data.length?data.map(x=>`<div class="list-item"><div><strong>${esc(x.name)}</strong><span class="muted">${(x.size/1024/1024).toFixed(2)} МБ · ${formatDate(x.created_at)}</span></div></div>`).join(''):'<div class="empty">Копий пока нет</div>'; }
  function renderErrors(data){ $('#errorsList').innerHTML=data.length?data.slice(0,50).map(x=>`<div class="list-item"><div><strong>${esc(x.service)} · HTTP ${esc(x.status)}</strong><span class="muted">${esc(x.endpoint)} · ${esc(x.message)}</span></div><span>${formatDate(x.created_at)}</span></div>`).join(''):'<div class="empty">Ошибок не зафиксировано</div>'; }

  function caseStatusName(status) {
    return ({new:'Новый',diagnosis:'Диагностика',waiting:'Ждём клиента',replacement:'Нужна замена',resolved:'Решено',closed:'Закрыто'})[status] || status || 'Новый';
  }
  function casePriorityName(priority) {
    return ({low:'Низкий',normal:'Обычный',high:'Высокий',critical:'Критичный'})[priority] || priority || 'Обычный';
  }

  async function populateSupplierDatalist() {
    const {data} = await api('/app/api/suppliers');
    $('#supplierNames').innerHTML = (data || []).map(x => `<option value="${esc(x.name)}"></option>`).join('');
  }

  function openCaseEditor(item = {}) {
    $('#caseId').value = item.id || '';
    $('#caseInvoice').value = item.invoice_id || state.currentOrder?.invoice_id || state.currentChatInfo?.invoice_id || '';
    $('#caseProduct').value = item.product_name || state.currentOrder?.name || state.currentChatInfo?.product_name || '';
    $('#caseStatus').value = item.status || 'new';
    $('#casePriority').value = item.priority || 'normal';
    $('#caseCategory').value = item.category || 'activation';
    $('#caseError').value = item.error_code || '';
    $('#caseReason').value = item.reason || '';
    $('#caseNote').value = item.note || '';
    openDialog($('#caseDialog'));
  }

  async function loadCases() {
    const params = new URLSearchParams();
    if (state.caseStatus !== 'all') params.set('status', state.caseStatus);
    const query = $('#caseSearch')?.value.trim(); if (query) params.set('q', query);
    const {data} = await api(`/app/api/problem-cases?${params}`);
    state.cases = data || [];
    const counts = {new:0,diagnosis:0,waiting:0,replacement:0,critical:0};
    state.cases.forEach(x=>{ if(counts[x.status]!==undefined) counts[x.status]++; if(x.priority==='critical') counts.critical++; });
    $('#caseMetrics').innerHTML = [['Новых',counts.new],['Диагностика',counts.diagnosis],['Ждём',counts.waiting],['Замена',counts.replacement],['Критичных',counts.critical]].map(([l,v])=>`<div class="metric-card compact-card"><span class="label">${l}</span><span class="value">${v}</span></div>`).join('');
    $('#casesList').innerHTML = state.cases.length ? state.cases.map(item=>`<article class="case-card priority-${esc(item.priority)}" data-case-id="${item.id}"><div class="panel-heading"><div><strong>${esc(item.product_name || 'Проблемный заказ')}</strong><span class="muted">Заказ #${esc(item.invoice_id || '—')} · ${formatDate(item.updated_at)}</span></div><span class="badge">${esc(caseStatusName(item.status))}</span></div><p>${esc(item.reason || item.note || 'Причина не указана')}</p><div class="case-footer"><span class="badge severity-${esc(item.priority)}">${esc(casePriorityName(item.priority))}</span><span class="muted">${esc(item.error_code || item.category || '')}</span><button class="link-button" data-edit-case="${item.id}">Открыть</button></div></article>`).join('') : '<div class="empty">Проблемных заказов по этому фильтру нет</div>';
  }

  async function saveCase(event) {
    event.preventDefault();
    const id = $('#caseId').value;
    const payload = {invoice_id:$('#caseInvoice').value.trim(),product_name:$('#caseProduct').value.trim(),status:$('#caseStatus').value,priority:$('#casePriority').value,category:$('#caseCategory').value,error_code:$('#caseError').value.trim(),reason:$('#caseReason').value.trim(),note:$('#caseNote').value.trim(),conversation_id:state.currentChat || '',offer_id:state.currentOrder?.item_id || ''};
    await api(id ? `/app/api/problem-cases/${id}` : '/app/api/problem-cases',{method:id?'PATCH':'POST',body:JSON.stringify(payload)});
    closeDialog($('#caseDialog')); toast('Обращение сохранено'); await loadCases();
    if (state.currentOrder?.invoice_id === payload.invoice_id) await findOrder(payload.invoice_id);
  }

  async function openReplacement(invoice, offerId = '') {
    if (!invoice) return toast('Сначала откройте заказ', true);
    $('#replacementPreview').innerHTML = '<div class="empty">Проверяю склад…</div>';
    $('#replacementReason').value = '';
    openDialog($('#replacementDialog'));
    try {
      const params = offerId ? `?offer_id=${encodeURIComponent(offerId)}` : '';
      const {data} = await api(`/app/api/orders/${encodeURIComponent(invoice)}/replacement/preview${params}`);
      state.replacementPreview = data;
      $('#replacementPreview').innerHTML = `<div class="notice warning-notice"><b>Заказ #${esc(data.invoice_id)}</b><br>${esc(data.product_name || `Товар ${data.offer_id}`)}<br>Новая позиция: <code>${esc(data.content_masked)}</code><br>Доступно на складе: ${esc(data.stock_available)}<br><small>Перед отправкой позиция будет исключена из склада GGSEL.</small></div>`;
    } catch (error) { state.replacementPreview=null; $('#replacementPreview').innerHTML=`<div class="notice">${esc(error.message)}</div>`; }
  }

  async function confirmReplacement() {
    const p = state.replacementPreview;
    if (!p) return toast('Нет доступного содержимого для замены', true);
    if (!await confirmAction(`Исключить ${p.content_masked} из склада и отправить покупателю по заказу #${p.invoice_id}?`)) return;
    const btn=$('#replacementConfirm'); btn.disabled=true; btn.textContent='Выполняю замену…';
    try {
      const {data}=await api(`/app/api/orders/${encodeURIComponent(p.invoice_id)}/replacement`,{method:'POST',body:JSON.stringify({offer_id:p.offer_id,product_id:p.product_id,reason:$('#replacementReason').value.trim(),confirm:true})});
      toast(`Замена отправлена: ${data.content_masked}`); closeDialog($('#replacementDialog')); state.replacementPreview=null;
      if(state.currentOrder?.invoice_id===p.invoice_id) await findOrder(p.invoice_id); await loadAttention(false);
    } catch(error){ toast(error.message,true); }
    finally{btn.disabled=false;btn.textContent='Подтвердить и отправить покупателю';}
  }

  function openDiagnostic(invoice='', conversation='') {
    $('#diagnosticInvoice').value=invoice || state.currentOrder?.invoice_id || state.currentChatInfo?.invoice_id || '';
    $('#diagnosticConversation').value=conversation || state.currentChat || '';
    const product=(state.currentOrder?.name || state.currentChatInfo?.product_name || '').toLowerCase();
    $('#diagnosticFamily').value=product.includes('office')?'office':product.includes('windows')?'windows':product.includes('mac')?'mac':'other';
    $('#diagnosticInstalled').value=''; $('#diagnosticPurchased').value=state.currentOrder?.name || state.currentChatInfo?.product_name || '';
    $('#diagnosticError').value=''; $('#diagnosticDescription').value=''; $('#diagnosticResult').innerHTML=''; openDialog($('#diagnosticDialog'));
  }

  async function runDiagnostic(event){
    event.preventDefault();
    const payload={invoice_id:$('#diagnosticInvoice').value,conversation_id:$('#diagnosticConversation').value,product_family:$('#diagnosticFamily').value,installed_edition:$('#diagnosticInstalled').value,purchased_edition:$('#diagnosticPurchased').value,error_code:$('#diagnosticError').value,description:$('#diagnosticDescription').value};
    const {data}=await api('/app/api/diagnostics',{method:'POST',body:JSON.stringify(payload)});
    $('#diagnosticResult').innerHTML=`<div class="diagnostic-answer"><h3>${esc(data.title)}</h3><ol>${(data.steps||[]).map(x=>`<li>${esc(x)}</li>`).join('')}</ol><div class="button-row"><button class="secondary-button" type="button" data-use-diagnostic>Вставить в ответ</button>${data.action==='replacement'?'<button class="danger-button" type="button" data-diagnostic-replacement>Перейти к замене</button>':''}</div><textarea id="diagnosticResponseText" rows="8">${esc(data.response_text||'')}</textarea></div>`;
    $('#diagnosticResult').dataset.result=JSON.stringify(data);
  }

  async function loadKnowledge(query=''){
    const q=query || $('#knowledgeSearch')?.value.trim() || '';
    const {data}=await api(`/app/api/knowledge${q?`?q=${encodeURIComponent(q)}`:''}`); state.knowledge=data||[];
    $('#knowledgeList').innerHTML=state.knowledge.length?state.knowledge.map(x=>`<article class="knowledge-card" data-article-id="${x.id}"><div class="panel-heading"><div><span class="kicker">${esc(x.category)}</span><strong>${esc(x.title)}</strong></div><button class="danger-link" data-delete-article="${x.id}">Удалить</button></div><p>${esc(x.body)}</p><div class="knowledge-footer"><span class="muted">${esc((x.error_codes||[]).join(', ') || x.product_pattern || 'Общая инструкция')}</span><button class="secondary-button small" data-use-article="${x.id}">Использовать</button></div></article>`).join(''):'<div class="empty">Инструкции не найдены</div>';
  }

  async function openKnowledgePicker(){
    const {data}=await api('/app/api/knowledge'); state.knowledge=data||[]; renderKnowledgePicker(state.knowledge); openDialog($('#knowledgePickerDialog')); setTimeout(()=>$('#knowledgePickerSearch').focus(),60);
  }
  function renderKnowledgePicker(items){ $('#knowledgePickerList').innerHTML=items.length?items.map(x=>`<button class="list-item plain-button" data-pick-article="${x.id}"><div><strong>${esc(x.title)}</strong><span class="muted">${esc(x.category)} · ${esc((x.error_codes||[]).join(', '))}</span></div><span>→</span></button>`).join(''):'<div class="empty">Нет инструкций</div>'; }
  function useArticle(id){ const article=state.knowledge.find(x=>String(x.id)===String(id)); if(!article)return; if($('#chatDialog').open){$('#chatReplyText').value=article.body;closeDialog($('#knowledgePickerDialog'));toast('Инструкция вставлена в ответ');}else{navigator.clipboard?.writeText(article.body);toast('Инструкция скопирована');} }

  async function saveKnowledge(event){event.preventDefault();const payload={category:$('#knowledgeCategory').value,title:$('#knowledgeTitle').value,product_pattern:$('#knowledgeProductPattern').value,error_codes:$('#knowledgeErrors').value.split(',').map(x=>x.trim().toLowerCase()).filter(Boolean),body:$('#knowledgeBody').value};await api('/app/api/knowledge',{method:'POST',body:JSON.stringify(payload)});closeDialog($('#knowledgeDialog'));event.target.reset();toast('Инструкция сохранена');await loadKnowledge();}

  async function loadSupply(){
    const [profit,suppliers,batches,settings]=await Promise.all([api('/app/api/profitability'),api('/app/api/suppliers'),api('/app/api/batches'),api('/app/api/business-settings')]);
    const p=profit.data||{}; const st=settings.data||{};
    $('#profitMetrics').innerHTML=[['Прибыль до закупки',money(p.platform_profit_rub||0,'RUB')],['Себестоимость',money(p.cost_rub||0,'RUB')],['Чистая прибыль',money(p.net_profit_rub||0,'RUB')],['Заказов с ценой',`${p.cost_known_orders||0} / ${p.orders||0}`]].map(([l,v])=>`<div class="metric-card"><span class="label">${l}</span><span class="value">${esc(v)}</span></div>`).join('');
    $('#profitCoverage').textContent=`Покрытие себестоимостью: ${p.coverage_percent||0}%`;
    $('#settlementCurrency').value=st.settlement_currency||'USD'; $('#settlementRate').value=st.settlement_to_rub||''; $('#targetMargin').value=st.target_margin||30;
    $('#profitOrders').innerHTML=(p.items||[]).slice(0,30).map(x=>`<button class="list-item plain-button" data-open-order="${esc(x.invoice_id)}"><div><strong>${esc(x.product_name||`Заказ #${x.invoice_id}`)}</strong><span class="muted">#${esc(x.invoice_id)} · ${formatDate(x.date)}${x.cost_known?'':' · нет себестоимости'}</span></div><b>${x.net_profit_rub!==null?money(x.net_profit_rub,'RUB'):'—'}</b></button>`).join('')||'<div class="empty">Откройте заказы, чтобы собрать расчёт</div>';
    $('#suppliersList').innerHTML=(suppliers.data||[]).map(x=>`<div class="list-item"><div><strong>${esc(x.name)}</strong><span class="muted">${esc(x.contact||'Без контакта')} · ${esc(x.default_currency)}</span></div><button class="danger-link" data-delete-supplier="${x.id}">Удалить</button></div>`).join('')||'<div class="empty">Поставщики пока не добавлены</div>';
    $('#batchesList').innerHTML=(batches.data||[]).map(x=>`<div class="list-item batch-item"><div><strong>${esc(x.product_name||`Товар ${x.offer_id}`)}</strong><span class="muted">Партия #${x.id} · ${esc(x.supplier_display||'без поставщика')} · ${x.ledger_count||0} шт. · продано ${x.sold||0} · замен ${x.replaced||0}</span></div><b>${money((x.unit_cost_rub||0)*(x.quantity||0),'RUB')}</b></div>`).join('')||'<div class="empty">Партии появятся после пополнения товара с заполненной себестоимостью</div>';
  }

  async function syncProfitability(){
    const btn=$('#profitSyncBtn'); const original=btn?.textContent;
    if(btn){btn.disabled=true;btn.textContent='Обновляю…';}
    try{
      const {data}=await api('/app/api/profitability/sync',{method:'POST',body:JSON.stringify({limit:20})});
      const errors=(data.errors||[]).length;
      toast(`Обновлено заказов: ${data.synced||0}${errors?` · ошибок: ${errors}`:''}`);
      await loadSupply();
    }finally{if(btn){btn.disabled=false;btn.textContent=original||'Обновить 20 заказов';}}
  }

  async function saveProfitSettings(event){event.preventDefault();await api('/app/api/business-settings',{method:'PUT',body:JSON.stringify({settlement_currency:$('#settlementCurrency').value,settlement_to_rub:Number($('#settlementRate').value||0),target_margin:Number($('#targetMargin').value||30)})});toast('Настройки прибыли сохранены');await loadSupply();}
  async function saveSupplier(event){event.preventDefault();await api('/app/api/suppliers',{method:'POST',body:JSON.stringify({name:$('#supplierName').value,contact:$('#supplierContact').value,default_currency:$('#supplierCurrency').value,notes:$('#supplierNotes').value})});closeDialog($('#supplierDialog'));event.target.reset();toast('Поставщик сохранён');await loadSupply();}

  async function loadQuality(){
    const {data}=await api('/app/api/quality');
    $('#incidentsList').innerHTML=(data.incidents||[]).length?data.incidents.map(x=>`<div class="list-item severity-critical"><div><strong>${esc(x.product_name||`Товар ${x.offer_id}`)}</strong><span class="muted">${x.signal_count} похожих обращений за ${x.window_minutes} мин. · ${esc(x.error_code||x.category)}</span></div><button class="secondary-button small" data-resolve-incident="${x.id}">Решено</button></div>`).join(''):'<div class="empty">Массовых проблем не обнаружено</div>';
    $('#productQualityList').innerHTML=(data.products||[]).map(x=>`<div class="list-item"><div><strong>${esc(x.product_name)}</strong><span class="muted">${x.sales_count} продаж · проблем ${x.problem_orders} · замен ${x.replacements} · негативных ${x.negative_reviews}</span></div><b>${x.reliability===null?'—':`${x.reliability}%`}</b></div>`).join('')||'<div class="empty">Недостаточно данных</div>';
    $('#supplierQualityList').innerHTML=(data.suppliers||[]).map(x=>`<div class="list-item"><div><strong>${esc(x.supplier_name)}</strong><span class="muted">${x.total_items||0} загружено · ${x.sold_items||0} продано · ${x.replaced_items||0} замен</span></div><b>${x.quality===null?'—':`${x.quality}%`}</b></div>`).join('')||'<div class="empty">Добавьте поставщиков и партии</div>';
  }

  function bindEvents() {
    $('#casesRefresh')?.addEventListener('click', loadCases);
    $('#caseCreateBtn')?.addEventListener('click', ()=>openCaseEditor());
    $('#caseForm')?.addEventListener('submit', saveCase);
    $('#caseFilters')?.addEventListener('click', e=>{const b=e.target.closest('[data-case-status]');if(!b)return;state.caseStatus=b.dataset.caseStatus;$$('#caseFilters .chip').forEach(x=>x.classList.toggle('active',x===b));loadCases();});
    let caseSearchTimer; $('#caseSearch')?.addEventListener('input',()=>{clearTimeout(caseSearchTimer);caseSearchTimer=setTimeout(loadCases,300);});
    $('#replacementConfirm')?.addEventListener('click', confirmReplacement);
    $('#diagnosticForm')?.addEventListener('submit', runDiagnostic);
    $('#knowledgeAddBtn')?.addEventListener('click',()=>openDialog($('#knowledgeDialog')));
    $('#knowledgeForm')?.addEventListener('submit', saveKnowledge);
    let knowledgeTimer; $('#knowledgeSearch')?.addEventListener('input',e=>{clearTimeout(knowledgeTimer);knowledgeTimer=setTimeout(()=>loadKnowledge(e.target.value),260);});
    $('#knowledgePickerSearch')?.addEventListener('input',e=>{const q=e.target.value.toLowerCase();renderKnowledgePicker(state.knowledge.filter(x=>[x.title,x.body,x.category,(x.error_codes||[]).join(' ')].some(v=>String(v||'').toLowerCase().includes(q))));});
    $('#chatKnowledgeBtn')?.addEventListener('click', openKnowledgePicker);
    $('#chatDiagnosticBtn')?.addEventListener('click',()=>openDiagnostic(state.currentChatInfo?.invoice_id||'',state.currentChat||''));
    $('#chatCaseBtn')?.addEventListener('click',()=>openCaseEditor({invoice_id:state.currentChatInfo?.invoice_id||'',conversation_id:state.currentChat||'',product_name:state.currentChatInfo?.product_name||''}));
    $('#chatReplacementBtn')?.addEventListener('click',()=>openReplacement(state.currentChatInfo?.invoice_id||'',state.currentChatInfo?.item_id||''));
    $('#supplyRefresh')?.addEventListener('click',loadSupply);
    $('#profitSyncBtn')?.addEventListener('click',syncProfitability);
    $('#profitSettingsForm')?.addEventListener('submit',saveProfitSettings);
    $('#supplierAddBtn')?.addEventListener('click',()=>openDialog($('#supplierDialog')));
    $('#supplierForm')?.addEventListener('submit',saveSupplier);
    $('#qualityRefresh')?.addEventListener('click',loadQuality);
    $('#stockCostCurrency')?.addEventListener('change',e=>{if(['RUB','RUR'].includes(e.target.value))$('#stockFxRate').value='1';});
    $('#soldContentSearchBtn')?.addEventListener('click', searchSoldContent);
    $('#reindexSalesBtn')?.addEventListener('click', reindexSales);
    $('#inventoryRefresh')?.addEventListener('click', loadInventoryTools);
    $('#stockValues')?.addEventListener('input', (()=>{let t; return ()=>{clearTimeout(t); t=setTimeout(validateStockInput,400);};})());
    $('#slaRefresh')?.addEventListener('click', async()=>renderSla((await api('/app/api/sla')).data));
    $('#recommendationsRefresh')?.addEventListener('click', async()=>renderRecommendations((await api('/app/api/recommendations')).data));
    $('#templateAddBtn')?.addEventListener('click', ()=>openDialog($('#templateDialog')));
    $('#ruleAddBtn')?.addEventListener('click', ()=>openDialog($('#ruleDialog')));
    $('#templateForm')?.addEventListener('submit', async e=>{e.preventDefault(); await api('/app/api/templates',{method:'POST',body:JSON.stringify({category:$('#templateCategory').value,name:$('#templateName').value,body:$('#templateBody').value})}); closeDialog($('#templateDialog')); renderTemplates((await api('/app/api/templates')).data); toast('Шаблон сохранён');});
    $('#ruleForm')?.addEventListener('submit', async e=>{e.preventDefault(); const actionType=$('#ruleAction').value; const val=$('#ruleActionValue').value; await api('/app/api/automations',{method:'POST',body:JSON.stringify({name:$('#ruleName').value,trigger_type:$('#ruleTrigger').value,condition:{contains:$('#ruleContains').value},action:actionType==='notify'?{type:'notify',text:val}:{type:'label_chat',label:val}})}); closeDialog($('#ruleDialog')); renderRules((await api('/app/api/automations')).data); toast('Правило сохранено');});
    $('#backupCreateBtn')?.addEventListener('click', async()=>{const {data}=await api('/app/api/backups',{method:'POST',body:'{}'}); toast(`Копия создана: ${data.name}`); renderBackups((await api('/app/api/backups')).data);});
    $('#errorsRefresh')?.addEventListener('click', async()=>renderErrors((await api('/app/api/errors')).data));
    $('#reportSettingsSave')?.addEventListener('click', async()=>{await api('/app/api/report-settings',{method:'PUT',body:JSON.stringify({morning_enabled:$('#morningReportToggle').checked,evening_enabled:$('#eveningReportToggle').checked})}); toast('Настройки отчёта сохранены');});

    $$('.tab').forEach(tab => tab.addEventListener('click', () => switchSection(tab.dataset.section)));
    $$('[data-goto]').forEach(button => button.addEventListener('click', () => switchSection(button.dataset.goto)));
    $('#globalRefresh').addEventListener('click', globalRefresh);
    $('#globalSearchBtn').addEventListener('click', openGlobalSearch);
    $('#globalSearchMoreBtn').addEventListener('click', openGlobalSearch);
    $('#attentionBtn').addEventListener('click', () => loadAttention(true).catch(error => toast(error.message, true)));
    $('#dashboardAttentionRefresh').addEventListener('click', () => loadAttention(false).catch(error => toast(error.message, true)));
    $('#dashboardRecentClear').addEventListener('click', () => loadRecent().catch(error => toast(error.message, true)));
    $('#compactModeToggle').addEventListener('change', event => { localStorage.setItem('ggselCompactMode', event.target.checked ? '1' : '0'); applyPreferences(); });
    $('#hideBalanceToggle').addEventListener('change', event => { localStorage.setItem('ggselHideBalance', event.target.checked ? '1' : '0'); applyPreferences(); });
    $('#dashboardPeriods').addEventListener('click', event => { const button = event.target.closest('[data-dashboard-period]'); if (button) setDashboardPeriod(button.dataset.dashboardPeriod); });
    $('#dashboardProduct').addEventListener('change', async event => { state.dashboardProduct = event.target.value; await loadDashboard(); });
    $('#dashboardDateForm').addEventListener('submit', async event => { event.preventDefault(); state.dashboardStart = $('#dashboardStart').value; state.dashboardEnd = $('#dashboardEnd').value; if (!state.dashboardStart || !state.dashboardEnd) return; await loadDashboard(); });
    $('#dashboardTopProducts').addEventListener('click', event => { const id = event.target.closest('[data-product-id]')?.dataset.productId; if (id) { state.dashboardProduct = id; $('#dashboardProduct').value = id; loadDashboard(); } });
    $('#dashboardLowStock').addEventListener('click', event => { const id = event.target.closest('[data-offer-id]')?.dataset.offerId; if (id) switchSection('offers').then(() => showOffer(id)); });
    $('#dashboardSupport').addEventListener('click', event => { if (event.target.closest('[data-goto="chats"]')) switchSection('chats'); });
    const attentionClick = event => { const node = event.target.closest('[data-attention-action]'); if (!node) return; const action = node.dataset.attentionAction; const id = node.dataset.entityId; if (action === 'offer') switchSection('offers').then(() => showOffer(id)); else if (action === 'chats') { state.chatLabel = id || 'all'; switchSection('chats').then(() => { $$('#chatFilters .chip').forEach(chip => chip.classList.toggle('active', chip.dataset.chatLabel === state.chatLabel)); renderChats(); }); } else if (action === 'operations') switchSection('operations'); closeDialog($('#attentionDialog')); };
    $('#dashboardAttention').addEventListener('click', attentionClick);
    $('#attentionList').addEventListener('click', attentionClick);
    $('#dashboardRecent').addEventListener('click', event => { const node = event.target.closest('[data-recent-type]'); if (node) handleNavigationItem(node.dataset.recentType, node.dataset.recentId); });
    $$('.quick-actions-grid [data-quick-action]').forEach(button => button.addEventListener('click', () => { const action = button.dataset.quickAction; if (action === 'search') openGlobalSearch(); if (action === 'low-stock') { state.offerStatus = 'low_stock'; switchSection('offers').then(() => { $$('#offerFilters .chip').forEach(chip => chip.classList.toggle('active', chip.dataset.status === 'low_stock')); loadOffers(); }); } if (action === 'order') switchSection('orders').then(() => $('#invoiceInput').focus()); if (action === 'chats') switchSection('chats'); }));

    let searchTimer;
    $('#offerSearch').addEventListener('input', event => {
      clearTimeout(searchTimer); searchTimer = setTimeout(async () => { state.offerQuery = event.target.value.trim(); state.offerPage = 1; await loadOffers(); }, 350);
    });
    $('#offerFilters').addEventListener('click', async event => {
      const chip = event.target.closest('[data-status]'); if (!chip) return;
      $$('#offerFilters .chip').forEach(x => x.classList.toggle('active', x === chip));
      state.offerStatus = chip.dataset.status; state.offerPage = 1; await loadOffers();
    });
    $('#offerRefresh').addEventListener('click', () => loadOffers(true));
    $('#offersPrev').addEventListener('click', async () => { if (state.offerPage > 1) { state.offerPage--; await loadOffers(); } });
    $('#offersNext').addEventListener('click', async () => { if (state.offerPage < state.offerPages) { state.offerPage++; await loadOffers(); } });
    $('#toggleSelection').addEventListener('click', async () => { state.selectionMode = !state.selectionMode; state.selectedOffers.clear(); $('#toggleSelection').textContent = state.selectionMode ? 'Отменить выбор' : 'Выбрать несколько'; await loadOffers(); });
    $('#offersList').addEventListener('click', event => {
      const card = event.target.closest('.offer-card'); if (!card) return;
      const id = card.dataset.id;
      if (event.target.matches('.offer-select')) { event.target.checked ? state.selectedOffers.add(id) : state.selectedOffers.delete(id); updateBatchBar(); return; }
      const action = event.target.closest('[data-action]')?.dataset.action;
      if (action === 'details') showOffer(id);
      if (action === 'stock') openStock(id, $('.offer-title', card)?.textContent || '');
      if (action === 'favorite') toggleFavorite(id, !event.target.closest('[data-action=\"favorite\"]').classList.contains('active'));
    });
    $('#batchBar').addEventListener('click', event => { const action = event.target.closest('[data-batch]')?.dataset.batch; if (action) batchAction(action); });
    $('#createOfferBtn').addEventListener('click', () => openDialog($('#createOfferDialog')));

    $('#offerDialogBody').addEventListener('click', event => {
      const action = event.target.closest('[data-modal-action]')?.dataset.modalAction; if (!action) return;
      const id = state.currentOffer;
      if (action === 'stock') openStock(id, $('#offerDialogTitle').textContent);
      if (action === 'edit') openEditOffer();
      if (action === 'save-settings') saveOfferSettings(id);
      if (action === 'favorite') { const current = JSON.parse($('#offerDialog').dataset.offer || '{}').item || {}; toggleFavorite(id, !current.favorite); }
      if (['activate','pause','delete'].includes(action)) offerAction(id, action);
      if (action === 'archive-all') archiveAllStock(id);
    });
    $('#stockValues').addEventListener('input', () => { const raw = $('#stockValues').value.split(/\r?\n/).filter(v=>v.trim()).length; const unique = parsedStockValues().length; $('#stockCount').textContent = `${unique} строк${raw !== unique ? ` · ${raw-unique} дублей` : ''}`; });
    $('#stockFile').addEventListener('change', async event => { const file = event.target.files?.[0]; if (file) { $('#stockValues').value = await file.text(); $('#stockValues').dispatchEvent(new Event('input')); } });
    $('#stockSubmit').addEventListener('click', submitStock);
    $('#editOfferForm').addEventListener('submit', saveOffer);
    $('#createOfferForm').addEventListener('submit', createOffer);

    $('#orderSearchForm').addEventListener('submit', event => { event.preventDefault(); const invoice = $('#invoiceInput').value.trim(); if (invoice) findOrder(invoice); });
    $('#salesRefresh').addEventListener('click', loadSales);
    $('#salesList').addEventListener('click', event => { const invoice = event.target.closest('[data-invoice]')?.dataset.invoice; if (invoice) { $('#invoiceInput').value = invoice; findOrder(invoice); } });
    $('#dashboardSales').addEventListener('click', event => { const invoice = event.target.closest('[data-invoice]')?.dataset.invoice; if (invoice) { switchSection('orders').then(() => { $('#invoiceInput').value = invoice; findOrder(invoice); }); } });
    $('#orderResult').addEventListener('click', event => { if (event.target.closest('[data-save-order-note]')) saveOrderNote(); const card=event.target.closest('[data-order-id]')||$('#orderResult [data-order-id]'); if(event.target.closest('[data-order-replacement]')) openReplacement(card?.dataset.orderId,card?.dataset.offerId); if(event.target.closest('[data-order-diagnostic]')) openDiagnostic(card?.dataset.orderId,''); if(event.target.closest('[data-order-case]')) openCaseEditor({invoice_id:card?.dataset.orderId,product_name:state.currentOrder?.name||'',offer_id:card?.dataset.offerId}); const edit=event.target.closest('[data-edit-case]'); if(edit){const item=state.cases.find(x=>String(x.id)===String(edit.dataset.editCase))||state.currentOrder?.problem_case;openCaseEditor(item||{});} });

    $('#revenueForm').addEventListener('submit', calculateRevenue);
    $$('.finance-presets [data-period]').forEach(button => button.addEventListener('click', () => applyFinancePeriod(button.dataset.period)));
    $('#receiptsRefresh').addEventListener('click', loadReceipts);
    $('#chatsRefresh').addEventListener('click', loadChats);
    let chatSearchTimer; $('#chatSearch').addEventListener('input', event => { state.chatQuery = event.target.value.trim(); clearTimeout(chatSearchTimer); chatSearchTimer = setTimeout(() => loadChats(state.chatQuery), 320); });
    $('#chatFilters').addEventListener('click', event => { const chip = event.target.closest('[data-chat-label]'); if (!chip) return; $$('#chatFilters .chip').forEach(x => x.classList.toggle('active', x === chip)); state.chatLabel = chip.dataset.chatLabel; renderChats(); });
    $$('.chat-label-actions [data-set-chat-label]').forEach(button => button.addEventListener('click', () => setChatLabel(button.dataset.setChatLabel)));
    $('#saveCustomerProfile')?.addEventListener('click', async () => {
      if (!state.currentChat) return;
      const tags = $('#customerTags').value.split(',').map(x => x.trim()).filter(Boolean);
      try {
        await api(`/app/api/conversations/${encodeURIComponent(state.currentChat)}/profile`, {method:'PUT', body:JSON.stringify({note:$('#customerNote').value,tags,pinned:$('#customerPinned').checked,favorite:$('#customerFavorite').checked})});
        toast('Карточка клиента сохранена'); await loadChats();
      } catch (error) { toast(error.message, true); }
    });
    $('#customerOrders')?.addEventListener('click', event => { const id=event.target.closest('[data-open-order]')?.dataset.openOrder; if(id){ closeDialog($('#chatDialog')); switchSection('orders').then(()=>{ $('#invoiceInput').value=id; findOrder(id); }); } });
    $('#chatsList').addEventListener('click', event => { const id = event.target.closest('[data-chat]')?.dataset.chat; if (id) openChat(id); });
    $('#chatReplyForm').addEventListener('submit', sendChatReply);
    $('#reviewsRefresh').addEventListener('click', loadReviews);
    $('#categorySearchForm').addEventListener('submit', event => { event.preventDefault(); loadCategories($('#categorySearch').value.trim()); });
    $('#auditRefresh').addEventListener('click', loadAudit);
    $('#operationsRefresh').addEventListener('click', () => loadOperations(true));
    $('#exportOffersBtn').addEventListener('click', exportOffers);
    $('#globalSearchInput').addEventListener('input', event => { clearTimeout(state.searchTimer); state.searchTimer = setTimeout(() => runGlobalSearch(event.target.value), 280); });
    $('#globalSearchDialog').addEventListener('click', event => { const example=event.target.closest('[data-search-example]'); if(example) applySearchExample(example.dataset.searchExample); });
    $('#globalSearchClearBtn')?.addEventListener('click', () => { $('#globalSearchInput').value=''; $('#globalSearchMeta').innerHTML=''; $('#globalSearchResults').innerHTML='<div class="empty">Введите минимум 2 символа или выберите пример команды</div>'; $('#globalSearchInput').focus(); });
    $('#globalSearchResults').addEventListener('click', event => { const node = event.target.closest('[data-search-type]'); if (node) handleNavigationItem(node.dataset.searchType, node.dataset.searchId, node.dataset.searchInvoice, node.dataset.searchQuery, node.dataset.searchOffer, node.dataset.searchConversation); });
    document.addEventListener('keydown', event => { const typing=['INPUT','TEXTAREA','SELECT'].includes(document.activeElement?.tagName); if ((event.key === '/' && !typing) || ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k')) { event.preventDefault(); openGlobalSearch(); } if (event.key === 'Escape') $$('.modal').forEach(closeDialog); });
    $$('[data-close]').forEach(button => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
  }

  async function boot() {
    if (tg) {
      tg.ready(); tg.expand();
      try { tg.setHeaderColor('secondary_bg_color'); } catch {}
      try { tg.setBackgroundColor('bg_color'); } catch {}
      tg.enableClosingConfirmation?.();
    }
    applyPreferences();
  
  document.addEventListener('click', async e=>{
    const order=e.target.closest('[data-open-order]'); if(order?.dataset.openOrder){ switchSection('orders'); $('#invoiceInput').value=order.dataset.openOrder; setTimeout(()=>findOrder(order.dataset.openOrder),150); }
    const chat=e.target.closest('[data-open-chat]'); if(chat?.dataset.openChat){ switchSection('chats'); setTimeout(()=>openChat(chat.dataset.openChat),150); }
    const offer=e.target.closest('[data-open-offer]'); if(offer?.dataset.openOffer){ switchSection('offers'); setTimeout(()=>showOffer(offer.dataset.openOffer),150); }
    const delTpl=e.target.closest('[data-delete-template]'); if(delTpl){ await api(`/app/api/templates/${delTpl.dataset.deleteTemplate}`,{method:'DELETE'}); renderTemplates((await api('/app/api/templates')).data); }
    const delRule=e.target.closest('[data-delete-rule]'); if(delRule){ await api(`/app/api/automations/${delRule.dataset.deleteRule}`,{method:'DELETE'}); renderRules((await api('/app/api/automations')).data); }
    const toggle=e.target.closest('[data-toggle-rule]'); if(toggle){ await api(`/app/api/automations/${toggle.dataset.toggleRule}`,{method:'PATCH',body:JSON.stringify({enabled:toggle.dataset.enabled==='1'})}); renderRules((await api('/app/api/automations')).data); }
    const editCase=e.target.closest('[data-edit-case]'); if(editCase && !e.target.closest('#orderResult')){const item=state.cases.find(x=>String(x.id)===String(editCase.dataset.editCase));if(item)openCaseEditor(item);}
    const useArticleBtn=e.target.closest('[data-use-article]'); if(useArticleBtn)useArticle(useArticleBtn.dataset.useArticle);
    const pickArticle=e.target.closest('[data-pick-article]'); if(pickArticle)useArticle(pickArticle.dataset.pickArticle);
    const deleteArticle=e.target.closest('[data-delete-article]'); if(deleteArticle && await confirmAction('Удалить инструкцию?')){await api(`/app/api/knowledge/${deleteArticle.dataset.deleteArticle}`,{method:'DELETE'});await loadKnowledge();}
    const deleteSupplier=e.target.closest('[data-delete-supplier]'); if(deleteSupplier && await confirmAction('Удалить поставщика? Партии сохранятся.')){await api(`/app/api/suppliers/${deleteSupplier.dataset.deleteSupplier}`,{method:'DELETE'});await loadSupply();}
    const resolveIncident=e.target.closest('[data-resolve-incident]'); if(resolveIncident){await api(`/app/api/incidents/${resolveIncident.dataset.resolveIncident}`,{method:'PATCH',body:JSON.stringify({status:'resolved'})});await loadQuality();}
    if(e.target.closest('[data-use-diagnostic]')){const text=$('#diagnosticResponseText')?.value||'';if($('#chatDialog').open){$('#chatReplyText').value=text;closeDialog($('#diagnosticDialog'));toast('Решение вставлено в ответ');}else{navigator.clipboard?.writeText(text);toast('Решение скопировано');}}
    if(e.target.closest('[data-diagnostic-replacement]')){closeDialog($('#diagnosticDialog'));openReplacement($('#diagnosticInvoice').value,state.currentOrder?.item_id||state.currentChatInfo?.item_id||'');}
  });
  bindEvents();
    if (!initData) {
      const notice = $('#authNotice'); notice.textContent = 'Откройте эту панель кнопкой «Управление» внутри Telegram-бота.'; notice.classList.remove('hidden');
      return;
    }
    try { [state.dashboardStart, state.dashboardEnd] = dashboardRange(state.dashboardPeriod); $('#dashboardStart').value = state.dashboardStart; $('#dashboardEnd').value = state.dashboardEnd; await loadIdentity(); await loadDashboard(); state.loaded.add('dashboard'); }
    catch (error) { $('#authNotice').textContent = error.message; $('#authNotice').classList.remove('hidden'); toast(error.message, true); }
  }

  boot();
})();
