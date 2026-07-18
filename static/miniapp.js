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
      } catch (error) { toast(error.message, true); }
    }
    window.scrollTo({top:0, behavior:'smooth'});
  }

  async function loadIdentity() {
    const {data} = await api('/app/api/me');
    $('#ownerName').textContent = data.first_name ? `${data.first_name} · ID ${data.seller_id || '—'}` : '';
  }

  async function loadDashboard() {
    $('#dashboardCards').innerHTML = '<div class="metric-card"><span class="label">Загрузка</span><span class="value">…</span></div>'.repeat(4);
    const payload = await api('/app/api/dashboard');
    const {balance = {}, stats = {}, sales = []} = payload.data || {};
    $('#dashboardCards').innerHTML = [
      ['Доступно', balance.amount_t_free ?? '—', true],
      ['Активных товаров', stats.active ?? 0],
      ['На паузе', stats.paused ?? 0],
      ['Без остатка', stats.out_of_stock ?? 0],
    ].map(([label, value, accent]) => `<div class="metric-card ${accent ? 'accent' : ''}"><span class="label">${esc(label)}</span><span class="value">${esc(value)}</span></div>`).join('');
    renderSales(sales, $('#dashboardSales'), 8);
    if (payload.errors && Object.keys(payload.errors).length) {
      const message = Object.entries(payload.errors).map(([k,v]) => `${k}: ${v}`).join('\n');
      console.warn(message);
    }
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
            <div class="offer-title">${esc(item.title)}</div>
            <div class="offer-meta"><span class="badge ${esc(item.status)}">${esc(statusLabel(item.status))}</span><span class="badge ${stockClass}">Остаток: ${esc(item.quantity)}</span><span class="badge">ID ${esc(item.id)}</span></div>
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
      const products = stockResult.data || [];
      $('#offerDialogTitle').textContent = item.title || `Товар #${id}`;
      $('#offerDialogBody').innerHTML = `
        <div class="offer-meta"><span class="badge ${esc(item.status)}">${esc(statusLabel(item.status))}</span><span class="badge">ID ${esc(item.id || id)}</span></div>
        <div class="detail-grid">
          <div class="detail"><span>Цена</span><b>${money(item.price, item.currency)}</b></div>
          <div class="detail"><span>Остаток</span><b>${esc(item.quantity)}</b></div>
          <div class="detail"><span>Продано</span><b>${esc(item.sold_products_count)}</b></div>
          <div class="detail"><span>Автовыдача</span><b>${item.is_autoselling ? 'Да' : 'Нет'}</b></div>
        </div>
        <p class="muted">${esc(item.category || '')}</p>
        <p>${esc(getAny(raw, ['description_ru','info','description'], 'Описание не указано'))}</p>
        <div class="offer-actions modal-actions">
          <button class="primary-button" data-modal-action="stock">＋ Добавить содержимое</button>
          <button class="secondary-button" data-modal-action="edit">Изменить</button>
          ${item.status === 'active' ? '<button class="secondary-button" data-modal-action="pause">Пауза</button>' : '<button class="success-button" data-modal-action="activate">Включить</button>'}
          <button class="danger-button" data-modal-action="delete">В архив</button>
        </div>
        <div class="panel-heading stock-heading"><h3>Содержимое (${products.length})</h3>${products.length ? '<button class="danger-button compact" data-modal-action="archive-all">Очистить склад</button>' : ''}</div>
        <div class="stock-list">${products.length ? products.map(product => `<div class="stock-row"><span>${esc(product.value)}</span><span>${esc(product.status || '')}</span></div>`).join('') : `<div class="empty">${esc(stockResult.stockError || 'Склад пуст')}</div>`}</div>`;
      $('#offerDialog').dataset.offer = JSON.stringify({item, raw});
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
      const result = await api(`/app/api/offers/${state.currentOffer}/products`, {method:'POST', body:JSON.stringify({values, confirm:true})});
      toast(`Добавлено: ${result.data.added}`);
      closeDialog($('#stockDialog'));
      closeDialog($('#offerDialog'));
      await loadOffers(true);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = 'Добавить содержимое'; }
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
      const buyer = data.buyer || {};
      $('#orderResult').innerHTML = `<article class="order-card panel">
        <div class="panel-heading"><h3>${esc(data.name || 'Заказ')}</h3><span class="badge">#${esc(data.invoice_id)}</span></div>
        <div class="detail-grid">
          <div class="detail"><span>Зачислено</span><b>${money(data.amount, data.currency || 'RUB')}</b></div>
          <div class="detail"><span>Прибыль</span><b>${esc(data.profit)}</b></div>
          <div class="detail"><span>Статус</span><b>${esc(data.invoice_state)}</b></div>
          <div class="detail"><span>Оплата</span><b>${formatDate(data.date_pay)}</b></div>
        </div>
        <p><b>Покупатель:</b> ${esc(buyer.email || buyer.account || '—')}</p>
        <p class="muted">Телефон: ${esc(buyer.phone || '—')} · Внешний ID: ${esc(data.external_order_id || '—')}</p>
      </article>`;
    } catch (error) { $('#orderResult').innerHTML = `<div class="notice">${esc(error.message)}</div>`; }
  }

  async function loadFinance() {
    const today = new Date();
    const start = new Date(today); start.setDate(today.getDate() - 29);
    $('#revenueEnd').value = today.toISOString().slice(0,10);
    $('#revenueStart').value = start.toISOString().slice(0,10);
    await Promise.all([loadBalance(), loadReceipts()]);
  }

  async function loadBalance() {
    const {data} = await api('/app/api/balance');
    $('#balanceCards').innerHTML = [
      ['Доступно', data.amount_t_free, true], ['Заблокировано', data.amount_t_lock], ['С ограничением', data.amount_t_plus]
    ].map(([label,value,accent]) => `<div class="metric-card ${accent?'accent':''}"><span class="label">${label}</span><span class="value">${esc(value)}</span></div>`).join('');
  }

  async function loadReceipts() {
    $('#receiptsList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/receipts?count=30');
    if (!data?.length) { $('#receiptsList').innerHTML = '<div class="empty">Чеков нет</div>'; return; }
    $('#receiptsList').innerHTML = data.map(item => {
      const op = item.operation || {}; const product = item.product || {};
      const productName = typeof product.name === 'string' ? product.name : (product.name?.[0]?.value || 'Операция');
      return `<div class="list-item"><div><strong>${esc(productName)}</strong><span class="muted">${formatDate(op.datetime)} · ${esc(op.type || '')}</span></div><div class="amount">${esc(op.on_account ?? op.price ?? '—')}</div></div>`;
    }).join('');
  }

  async function calculateRevenue(event) {
    event.preventDefault();
    $('#revenueResult').innerHTML = '<div class="empty">Считаю…</div>';
    try {
      const params = new URLSearchParams({start:$('#revenueStart').value, end:$('#revenueEnd').value});
      const {data} = await api(`/app/api/revenue?${params}`);
      $('#revenueResult').innerHTML = `<div class="detail-grid"><div class="detail"><span>Получено</span><b>${money(data.received,'RUB')}</b></div><div class="detail"><span>Оборот</span><b>${money(data.gross,'RUB')}</b></div><div class="detail"><span>Операций</span><b>${esc(data.count)}</b></div><div class="detail"><span>Данные</span><b>${data.complete ? 'Полные' : 'Частичные'}</b></div></div>`;
    } catch (error) { $('#revenueResult').innerHTML = `<div class="notice">${esc(error.message)}</div>`; }
  }

  async function loadChats() {
    $('#chatsList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/chats');
    if (!data?.length) { $('#chatsList').innerHTML = '<div class="empty">Чатов нет</div>'; return; }
    $('#chatsList').innerHTML = data.map(item => {
      const id = getAny(item, ['id_i','debate_id','id','invoice_id']);
      const title = getAny(item, ['product_name','name','subject','email'], `Диалог ${id}`);
      const preview = getAny(item, ['last_message','message','text','buyer_email'], 'Открыть переписку');
      return `<button class="chat-card plain-button" data-chat="${esc(id)}"><div class="panel-heading"><strong>${esc(title)}</strong><span class="badge">#${esc(id)}</span></div><p class="muted">${esc(typeof preview === 'object' ? JSON.stringify(preview) : preview)}</p></button>`;
    }).join('');
  }

  async function openChat(id) {
    state.currentChat = String(id);
    $('#chatDialogTitle').textContent = `Диалог #${id}`;
    $('#chatMessages').innerHTML = '<div class="empty">Загрузка…</div>';
    openDialog($('#chatDialog'));
    try {
      const {data} = await api(`/app/api/chats/${encodeURIComponent(id)}`);
      if (!data?.length) { $('#chatMessages').innerHTML = '<div class="empty">Сообщений нет</div>'; return; }
      $('#chatMessages').innerHTML = data.map(item => {
        const seller = item.seller === true || item.is_seller === true || String(item.sender || '').toLowerCase() === 'seller';
        const body = getAny(item, ['message','text'], item.is_img ? '[Изображение]' : '[Файл]');
        const when = getAny(item, ['date_written','date','created_at']);
        return `<div class="message ${seller ? 'seller' : ''}">${esc(body)}<small>${formatDate(when)}</small></div>`;
      }).join('');
      $('#chatMessages').scrollTop = $('#chatMessages').scrollHeight;
    } catch (error) { $('#chatMessages').innerHTML = `<div class="notice">${esc(error.message)}</div>`; }
  }

  async function sendChatReply(event) {
    event.preventDefault();
    const message = $('#chatReplyText').value.trim();
    if (!message) return;
    if (!await confirmAction('Отправить сообщение покупателю?')) return;
    try {
      await api(`/app/api/chats/${encodeURIComponent(state.currentChat)}/messages`, {method:'POST', body:JSON.stringify({message, confirm:true})});
      $('#chatReplyText').value = ''; toast('Сообщение отправлено'); await openChat(state.currentChat);
    } catch (error) { toast(error.message, true); }
  }

  async function loadReviews() {
    $('#reviewsList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/reviews');
    if (!data?.length) { $('#reviewsList').innerHTML = '<div class="empty">Отзывов нет</div>'; return; }
    $('#reviewsList').innerHTML = data.map(item => {
      const rating = getAny(item, ['rating','feedback_type','type'], '—');
      const text = getAny(item, ['text','review','feedback','message'], 'Без текста');
      const product = getAny(item, ['product_name','name_goods','name'], 'Товар');
      return `<article class="review-card"><div class="panel-heading"><strong>${esc(product)}</strong><span class="badge active">★ ${esc(rating)}</span></div><p>${esc(text)}</p><span class="muted">${formatDate(getAny(item,['date','created_at','date_written']))}</span></article>`;
    }).join('');
  }

  async function loadCategories(query = '') {
    $('#categoriesList').innerHTML = '<div class="empty">Загрузка…</div>';
    const params = query ? `?q=${encodeURIComponent(query)}` : '';
    const {data} = await api(`/app/api/categories${params}`);
    if (!data?.length) { $('#categoriesList').innerHTML = '<div class="empty">Категории не найдены</div>'; return; }
    $('#categoriesList').innerHTML = data.slice(0,200).map(item => `<div class="list-item"><div><strong>${esc(item.title)}</strong><span class="muted">ID категории</span></div><span class="badge code">${esc(item.id)}</span></div>`).join('');
  }

  async function loadAudit() {
    $('#auditList').innerHTML = '<div class="empty">Загрузка…</div>';
    const {data} = await api('/app/api/audit');
    if (!data?.length) { $('#auditList').innerHTML = '<div class="empty">Действий пока нет</div>'; return; }
    $('#auditList').innerHTML = data.map(item => `<div class="list-item"><div><strong>${esc(item.action)}</strong><span class="muted">${formatDate(item.created_at)}</span></div><span class="badge">${esc(item.target || '—')}</span></div>`).join('');
  }

  async function globalRefresh() {
    state.loaded.delete(state.section);
    await switchSection(state.section);
    toast('Данные обновлены');
  }

  function bindEvents() {
    $$('.tab').forEach(tab => tab.addEventListener('click', () => switchSection(tab.dataset.section)));
    $$('[data-goto]').forEach(button => button.addEventListener('click', () => switchSection(button.dataset.goto)));
    $('#globalRefresh').addEventListener('click', globalRefresh);

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
    });
    $('#batchBar').addEventListener('click', event => { const action = event.target.closest('[data-batch]')?.dataset.batch; if (action) batchAction(action); });
    $('#createOfferBtn').addEventListener('click', () => openDialog($('#createOfferDialog')));

    $('#offerDialogBody').addEventListener('click', event => {
      const action = event.target.closest('[data-modal-action]')?.dataset.modalAction; if (!action) return;
      const id = state.currentOffer;
      if (action === 'stock') openStock(id, $('#offerDialogTitle').textContent);
      if (action === 'edit') openEditOffer();
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

    $('#revenueForm').addEventListener('submit', calculateRevenue);
    $('#receiptsRefresh').addEventListener('click', loadReceipts);
    $('#chatsRefresh').addEventListener('click', loadChats);
    $('#chatsList').addEventListener('click', event => { const id = event.target.closest('[data-chat]')?.dataset.chat; if (id) openChat(id); });
    $('#chatReplyForm').addEventListener('submit', sendChatReply);
    $('#reviewsRefresh').addEventListener('click', loadReviews);
    $('#categorySearchForm').addEventListener('submit', event => { event.preventDefault(); loadCategories($('#categorySearch').value.trim()); });
    $('#auditRefresh').addEventListener('click', loadAudit);
    $$('[data-close]').forEach(button => button.addEventListener('click', () => closeDialog(button.closest('dialog'))));
  }

  async function boot() {
    if (tg) {
      tg.ready(); tg.expand();
      try { tg.setHeaderColor('secondary_bg_color'); } catch {}
      try { tg.setBackgroundColor('bg_color'); } catch {}
      tg.enableClosingConfirmation?.();
    }
    bindEvents();
    if (!initData) {
      const notice = $('#authNotice'); notice.textContent = 'Откройте эту панель кнопкой «Управление» внутри Telegram-бота.'; notice.classList.remove('hidden');
      return;
    }
    try { await loadIdentity(); await loadDashboard(); state.loaded.add('dashboard'); }
    catch (error) { $('#authNotice').textContent = error.message; $('#authNotice').classList.remove('hidden'); toast(error.message, true); }
  }

  boot();
})();
