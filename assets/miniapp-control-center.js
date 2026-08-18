(() => {
  'use strict';

  const X = window.XASS;
  if (!X) return;

  const $ = id => document.getElementById(id);
  const esc = X.esc;
  const ROOTS = {
    desktop: 'Рабочий стол',
    downloads: 'Загрузки',
    documents: 'Документы',
    xass_files: 'XASS Files',
  };
  const COMMAND_LABELS = {
    ping: 'Проверка связи', check_update: 'Проверка обновления', screenshot: 'Снимок экрана',
    sleep: 'Сон', lock: 'Блокировка экрана', restart: 'Перезапуск агента', update: 'Обновление агента',
    reboot: 'Перезагрузка ПК', shutdown: 'Выключение ПК', open_archive: 'Открытие архива',
    cleanup_archive: 'Очистка медиа', files_list: 'Открытие папки', file_download: 'Подготовка файла',
    file_delete: 'Удаление файла', clipboard_get: 'Получение буфера', clipboard_set: 'Отправка в буфер',
  };
  const DANGEROUS = new Set(['lock', 'sleep', 'restart', 'update', 'reboot', 'shutdown', 'cleanup_archive', 'file_delete']);
  const ui = {
    activeAgent: '', activePanel: '', fileRoot: 'desktop', filePath: '',
    clipboardHistory: readClipboardHistory(), objectUrls: new Set(),
  };

  const icons = {
    agent: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/></svg>',
    server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.01M7 17.5h.01"/></svg>',
    bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg>',
    archive: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 7h16v14H4zM3 3h18v4H3zM9 11h6"/></svg>',
    file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 6h7l2 2h9v12H3z"/></svg>',
    clipboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M9 5h6v3H9z"/><path d="M7 6H5v15h14V6h-2"/></svg>',
    timeline: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
    rules: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M4 5h10M4 12h16M10 19h10"/><circle cx="17" cy="5" r="2"/><circle cx="7" cy="19" r="2"/></svg>',
    bot: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="4" y="7" width="16" height="13" rx="4"/><path d="M12 3v4M8 12h.01M16 12h.01M8 16h8"/></svg>',
    phone: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 18h4"/></svg>',
    data: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></svg>',
    scenarios: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="m13 2-9 12h7l-1 8 10-13h-7z"/></svg>',
  };

  function readClipboardHistory() {
    try {
      const value = JSON.parse(localStorage.getItem('xass-clipboard-history') || '[]');
      return Array.isArray(value) ? value.slice(0, 10) : [];
    } catch (_) { return []; }
  }

  function storeClipboard(text, direction) {
    const value = String(text || '').slice(0, 65536);
    if (!value) return;
    ui.clipboardHistory = [{ text: value, direction, at: new Date().toISOString() }, ...ui.clipboardHistory.filter(x => x.text !== value)].slice(0, 10);
    try { localStorage.setItem('xass-clipboard-history', JSON.stringify(ui.clipboardHistory)); } catch (_) {}
    renderClipboardHistory();
  }

  function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }
  function formatBytes(value) {
    const size = Number(value || 0);
    if (size < 1024) return size + ' Б';
    if (size < 1048576) return (size / 1024).toFixed(1) + ' КБ';
    return (size / 1048576).toFixed(1) + ' МБ';
  }
  function dateText(value) {
    if (!value) return '—';
    const numeric = typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value)) ? Number(value) : NaN;
    const date = new Date(Number.isFinite(numeric) && numeric < 1e12 ? numeric * 1000 : value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString('ru-RU', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  }
  function pcAgents() { return (X.state.boot?.sources || []).filter(X.isPc); }
  function selectedAgent() { return (X.state.boot?.sources || []).find(item => item.source_name === ui.activeAgent) || null; }
  function ensureAgent() {
    const agents = pcAgents();
    if (!agents.some(item => item.source_name === ui.activeAgent)) ui.activeAgent = (agents.find(item => item.is_online) || agents[0] || {}).source_name || '';
    return ui.activeAgent;
  }
  function confirmAction(text) { return new Promise(resolve => X.ask(text, resolve)); }

  async function ccApi(path, options = {}) {
    if (!X.demo) return X.api(path, options);
    if (path.startsWith('timeline')) return { status: 200, data: { ok: true, items: demoTimeline() } };
    if (path === 'rules' && (!options.method || options.method === 'GET')) return { status: 200, data: { ok: true, rules: X.state.boot?.rules || [] } };
    if (path === 'rules' && options.method === 'POST') {
      const item = { ...options.body, id: options.body.id || 'demo-' + Date.now() };
      X.state.boot.rules = [...(X.state.boot.rules || []).filter(x => x.id !== item.id), item];
      return { status: 200, data: { ok: true, rule: item, rules: X.state.boot.rules } };
    }
    if (path.startsWith('rules/') && options.method === 'DELETE') {
      const id = decodeURIComponent(path.slice(6));
      X.state.boot.rules = (X.state.boot.rules || []).filter(x => x.id !== id);
      return { status: 200, data: { ok: true, rules: X.state.boot.rules } };
    }
    if (path.includes('/screenshot')) return { status: 200, data: { ok: true, available: false } };
    return X.api(path, options);
  }

  async function rawRequest(path, options = {}) {
    if (X.demo) return new Response(new Blob(['XASS demo file'], { type: 'text/plain' }), { status: 200, headers: { 'Content-Type': 'text/plain', 'X-XASS-Status': '200' } });
    const headers = new Headers(options.headers || {});
    if (X.initData) headers.set('X-Telegram-Init-Data', X.initData);
    return fetch('/proxy.php?_passthrough=1&_p=' + encodeURIComponent('/api/mini/' + path), { ...options, headers, credentials: 'include' });
  }

  function demoTimeline() {
    const now = Date.now();
    return [
      { id: '1', type: 'agent', event_type: 'agent_online', title: 'Домашний ПК подключён', message: 'Агент отвечает без ошибок', device: 'Домашний ПК', level: 'success', created_at: new Date(now - 120000).toISOString() },
      { id: '2', type: 'command', event_type: 'command_screenshot', title: 'Screenshot получен', message: 'Новый снимок готов', device: 'Домашний ПК', level: 'success', created_at: new Date(now - 420000).toISOString() },
      { id: '3', type: 'notification', event_type: 'high_load', title: 'Высокая нагрузка CPU', message: 'CPU был выше 90%', device: 'Рабочий ноутбук', level: 'warning', created_at: new Date(now - 960000).toISOString() },
    ];
  }

  function agentIcon() { return '<span class="cc-agent-icon">' + icons.agent + '</span>'; }
  function agentState(item) {
    if (item.requires_attention) return ['attention', 'ВНИМАНИЕ'];
    return item.is_online ? ['online', 'В СЕТИ'] : ['', 'OFFLINE'];
  }
  function compactAgentHtml(item) {
    const state = agentState(item);
    const platform = item.last_payload?.platform || item.source_type || 'Устройство';
    return '<button class="cc-agent-row" data-cc-agent="' + esc(item.source_name) + '">' + agentIcon() +
      '<span><span class="cc-agent-name">' + esc(item.source_name) + '</span><span class="cc-agent-meta">' + esc(platform) + ' · v' + esc(item.agent_version || '0.0.0') + ' · ' + esc(X.age(item.last_seen_at)) + '</span></span>' +
      '<span class="cc-agent-side"><span class="cc-agent-state ' + state[0] + '">' + state[1] + '</span><span class="cc-chevron">›</span></span></button>';
  }

  function currentFilteredAgents() {
    const list = X.state.boot?.sources || [];
    const filter = X.state.agentFilter;
    if (filter === 'online') return list.filter(x => x.is_online);
    if (filter === 'offline') return list.filter(x => !x.is_online);
    if (filter === 'attention') return list.filter(x => x.requires_attention);
    if (filter === 'update') return list.filter(x => x.requires_update);
    return list;
  }

  function renderCompactAgents() {
    const home = $('homeDevices');
    const all = $('deviceList');
    if (home) home.innerHTML = '<div class="cc-agent-list">' + (X.state.boot?.sources || []).slice(0, 3).map(compactAgentHtml).join('') + '</div>' || '<div class="cc-empty">Устройства ещё не подключены.</div>';
    if (all) {
      const list = currentFilteredAgents();
      all.innerHTML = list.length ? '<div class="cc-agent-list">' + list.map(compactAgentHtml).join('') + '</div>' : '<div class="cc-empty">В этой группе устройств нет.</div>';
    }
  }

  function renderHomeOverview() {
    const home = $('view-home');
    if (!home || !X.state.boot) return;
    let host = home.querySelector('.cc-home-overview');
    if (!host) {
      host = document.createElement('div');
      host.className = 'cc-home-overview';
      home.querySelector('.intro')?.appendChild(host);
    }
    const boot = X.state.boot, sources = boot.sources || [], online = sources.filter(x => x.is_online).length;
    const system = boot.system_status || (X.demo ? {
      backend: { available: true, status: 'online' }, database: { available: true, status: 'online' },
      telegram_bot: { available: true, status: 'online' }, public_site: { available: true, status: 'online' },
    } : {});
    const attention = sources.filter(x => x.requires_attention).length + Number(boot.notifications_unread || 0);
    const statuses = [
      ['Backend', system.backend], ['База', system.database],
      ['Telegram', system.telegram_bot], ['Сайт', system.public_site],
      ['Агенты', { available: online > 0, status: online + ' / ' + sources.length }],
      ['Внимание', { available: attention === 0, status: attention ? String(attention) : 'нет' }],
    ];
    const healthy = statuses.filter(([, value]) => value?.available).length;
    const health = Math.round(healthy / statuses.length * 100);
    host.innerHTML = '<div class="cc-overview-card"><div class="cc-overview-head"><div class="cc-health-ring" style="--health:' + health + '">' + health + '%</div><div><div class="cc-overview-title">Общий статус XASS</div><div class="cc-overview-copy">Сервер, сервисы и устройства в одной сводке</div></div><div class="cc-attention ' + (attention ? '' : 'ok') + '">' + (attention ? attention + ' требуют внимания' : 'Всё спокойно') + '</div></div><div class="cc-status-grid">' + statuses.map(([label, value]) => '<div class="cc-service ' + (value?.available ? 'ok' : '') + '"><div class="cc-service-label">' + label + '</div><div class="cc-service-value"><i class="cc-service-dot"></i><span>' + esc(value?.status === 'online' ? 'Работает' : value?.status || 'Недоступно') + '</span></div></div>').join('') + '</div></div>';
  }

  const toolDefinitions = [
    ['agents', 'Агенты', 'Устройства и команды', 'agent', 'Windows'],
    ['server', 'Сервер', 'Сервисы и обновления', 'server', 'Сервер'],
    ['files', 'Файлы', 'Разрешённые папки ПК', 'file', ''],
    ['clipboard', 'Буфер', 'Текст между XASS и ПК', 'clipboard', ''],
    ['timeline', 'Timeline', 'Единый журнал событий', 'timeline', ''],
    ['rules', 'Правила', 'Простые ЕСЛИ → ТО', 'rules', ''],
    ['notifications', 'Уведомления', 'События и каналы', 'bell', 'Уведомления'],
    ['archive', 'Архив', 'Удалённые и медиа', 'archive', 'Архив переписки'],
    ['bot', 'Бот', 'Ответы и тихие часы', 'bot', 'Бот'],
    ['scenarios', 'Сценарии', 'Группы действий', 'scenarios', 'Сценарии'],
    ['iphone', 'iPhone', 'Вход и Face ID', 'phone', 'iPhone'],
    ['data', 'Данные', 'Копии и диагностика', 'data', 'Данные и диагностика'],
  ];

  function buildToolsHub() {
    const view = $('view-tools');
    if (!view || view.classList.contains('cc-organized')) return;
    view.classList.add('cc-organized');
    const shell = document.createElement('div');
    shell.className = 'cc-tools-shell';
    shell.innerHTML = '<div class="cc-tools-head"><div><h2>Центр управления</h2><p>Выберите задачу — остальные разделы не мешают.</p></div></div><div class="cc-hub-grid">' + toolDefinitions.map(([id, title, note, icon]) => '<button class="cc-hub-card" data-cc-panel="' + id + '"><span class="cc-hub-icon">' + icons[icon] + '</span><span class="cc-hub-title">' + title + '</span><span class="cc-hub-note">' + note + '</span></button>').join('') + '</div><div id="ccPanels"></div>';
    view.querySelector('.intro')?.after(shell);
    const panels = shell.querySelector('#ccPanels');
    const sections = [...view.querySelectorAll(':scope > .section')];
    for (const [id, title, , , heading] of toolDefinitions) {
      const panel = document.createElement('div');
      panel.className = 'cc-panel'; panel.dataset.ccPanelBody = id;
      panel.innerHTML = '<div class="cc-tools-head"><div><h2>' + title + '</h2><p>Инструменты XASS</p></div><button class="btn cc-close-panel" aria-label="Закрыть">×</button></div>';
      const section = heading ? sections.find(node => node.querySelector('.section-title')?.textContent.trim().startsWith(heading)) : null;
      if (section) panel.appendChild(section);
      else panel.insertAdjacentHTML('beforeend', '<div class="cc-card" id="ccDynamic-' + id + '"></div>');
      panels.appendChild(panel);
    }
    shell.querySelectorAll('[data-cc-panel]').forEach(button => button.addEventListener('click', () => openPanel(button.dataset.ccPanel)));
    shell.querySelectorAll('.cc-close-panel').forEach(button => button.addEventListener('click', closePanels));
  }

  function closePanels() {
    ui.activePanel = '';
    document.querySelector('.cc-tools-shell')?.classList.remove('panel-open');
    document.querySelectorAll('.cc-panel,.cc-hub-card').forEach(node => node.classList.remove('active'));
    document.querySelector('.cc-tools-shell')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function openPanel(id) {
    ui.activePanel = id;
    document.querySelector('.cc-tools-shell')?.classList.add('panel-open');
    document.querySelectorAll('.cc-panel').forEach(node => node.classList.toggle('active', node.dataset.ccPanelBody === id));
    document.querySelectorAll('.cc-hub-card').forEach(node => node.classList.toggle('active', node.dataset.ccPanel === id));
    const panel = document.querySelector('[data-cc-panel-body="' + CSS.escape(id) + '"]');
    if (id === 'files') renderFilesPanel();
    if (id === 'clipboard') renderClipboardPanel();
    if (id === 'timeline') loadTimeline();
    if (id === 'rules') renderRulesPanel();
    if (id === 'agents') renderCompactAgents();
    setTimeout(() => panel?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 20);
  }

  function createOverlays() {
    if ($('ccAgentDetail')) return;
    document.body.insertAdjacentHTML('beforeend', '<section class="cc-overlay" id="ccAgentDetail" aria-hidden="true"><header class="cc-overlay-head"><button id="ccAgentBack" aria-label="Назад">‹</button><div class="cc-overlay-heading"><strong id="ccAgentTitle">Агент</strong><small id="ccAgentSubtitle">Устройство</small></div><button id="ccAgentRefresh" aria-label="Обновить">↻</button></header><div class="cc-overlay-body" id="ccAgentBody"></div></section><div class="cc-lightbox" id="ccLightbox"><button id="ccLightboxClose" aria-label="Закрыть">×</button><img id="ccLightboxImage" alt="Снимок экрана"></div>');
    $('ccAgentBack').onclick = closeAgent;
    $('ccAgentRefresh').onclick = async () => { await X.loadBoot(); openAgent(ui.activeAgent); };
    $('ccLightboxClose').onclick = () => $('ccLightbox').classList.remove('open');
    $('ccLightbox').addEventListener('click', event => { if (event.target === $('ccLightbox')) $('ccLightbox').classList.remove('open'); });
  }

  function closeAgent() {
    $('ccAgentDetail')?.classList.remove('open');
    $('ccAgentDetail')?.setAttribute('aria-hidden', 'true');
  }

  function openAgent(source) {
    ui.activeAgent = source;
    const item = selectedAgent();
    if (!item) return X.toast('Агент не найден');
    createOverlays();
    $('ccAgentTitle').textContent = item.source_name;
    $('ccAgentSubtitle').textContent = (item.last_payload?.platform || item.source_type || 'Устройство') + ' · v' + (item.agent_version || '0.0.0');
    $('ccAgentBody').innerHTML = agentDetailHtml(item);
    $('ccAgentDetail').classList.add('open');
    $('ccAgentDetail').setAttribute('aria-hidden', 'false');
    bindAgentDetail(item);
    loadLatestScreenshot(item.source_name);
  }

  function agentDetailHtml(item) {
    const metrics = item.last_payload?.metrics || {}, system = item.system || item.last_payload?.system || {}, state = agentState(item);
    const reason = (item.attention_reasons || []).join(', ');
    const action = (command, label, icon = '→', kind = '') => '<button class="cc-action ' + kind + '" data-cc-command="' + command + '"><span class="cc-action-icon">' + icon + '</span><span>' + label + '</span></button>';
    return '<div class="cc-detail-hero">' + agentIcon() + '<div><div class="cc-detail-name">' + esc(item.source_name) + '</div><div class="cc-agent-meta">' + esc(item.last_payload?.platform || item.source_type || 'Устройство') + ' · ' + esc(X.age(item.last_seen_at)) + '</div></div><span class="cc-agent-state ' + state[0] + '">' + state[1] + '</span></div>' +
      (reason ? '<div class="cc-card" style="border-color:rgba(240,182,87,.35);color:#f3c879">Требует внимания: ' + esc(reason) + '</div>' : '') +
      '<div class="cc-card"><div class="cc-card-title"><span>Состояние</span><span class="cc-card-note">' + esc(item.is_online ? 'связь активна' : X.age(item.last_seen_at)) + '</span></div><div class="cc-metrics">' +
      [['CPU', metrics.cpu_percent], ['RAM', metrics.ram_used_percent], ['Диск', metrics.disk_used_percent]].map(([name, value]) => '<div class="cc-metric"><span>' + name + '</span><strong>' + esc(X.valueText(value, '%')) + '</strong></div>').join('') +
      '</div><div class="cc-detail-facts"><div class="cc-fact"><span>Uptime</span><strong>' + esc(system.uptime || item.last_payload?.uptime || '—') + '</strong></div><div class="cc-fact"><span>Последняя связь</span><strong>' + esc(X.age(item.last_seen_at)) + '</strong></div><div class="cc-fact"><span>Версия агента</span><strong>' + esc(item.agent_version || '0.0.0') + '</strong></div><div class="cc-fact"><span>Последняя команда</span><strong>' + esc(item.latest_command?.command ? COMMAND_LABELS[item.latest_command.command] || item.latest_command.command : 'нет команд') + '</strong></div></div></div>' +
      '<div class="cc-card"><div class="cc-card-title"><span>Быстрые действия</span><span class="cc-card-note">команды выполняет агент</span></div>' +
      '<div class="cc-action-group"><div class="cc-action-label">Диагностика</div><div class="cc-action-grid">' + action('ping', 'Проверить связь', '⌁') + action('check_update', 'Проверить обновление', '↻') + action('history', 'История команд', '≡') + action('screenshot', 'Сделать снимок', '▣') + '</div></div>' +
      '<div class="cc-action-group"><div class="cc-action-label">Система</div><div class="cc-action-grid">' + action('sleep', 'Сон', '☾') + action('lock', 'Заблокировать экран', '▢') + action('restart', 'Перезапустить агент', '↻') + action('update', 'Обновить агент', '⇧') + '</div></div>' +
      '<div class="cc-action-group"><div class="cc-action-label">Архив</div><div class="cc-action-grid">' + action('open_archive', 'Открыть архив', '▤') + action('cleanup_archive', 'Очистить медиа', '⌫') + '<button class="cc-action wide" id="ccArchiveToggle"><span class="cc-action-icon">◉</span><span>' + (item.archive_enabled ? 'Не хранить архив на этом ПК' : 'Хранить архив на этом ПК') + '</span></button></div></div>' +
      '<div class="cc-action-group"><div class="cc-action-label">Опасные</div><div class="cc-action-grid">' + action('reboot', 'Перезагрузить ПК', '↻', 'danger') + action('shutdown', 'Выключить ПК', '⏻', 'danger') + '</div></div><div class="cc-command-status" id="ccCommandStatus"></div></div>' +
      '<div class="cc-card"><div class="cc-card-title"><span>Экран</span><button class="btn" id="ccScreenshotRefresh">Обновить снимок</button></div><div class="cc-screenshot-shell" id="ccScreenshot"><div class="cc-screenshot-empty">Снимок загружается только по вашему запросу и временно хранится на сервере.</div></div><div class="cc-card-note" id="ccScreenshotMeta" style="margin-top:8px"></div></div>' +
      '<div class="cc-card" id="ccCommandHistory" style="display:none"><div class="cc-card-title"><span>История команд</span><button class="btn" id="ccHistoryClose">Скрыть</button></div><div id="ccCommandHistoryList"></div></div>';
  }

  function bindAgentDetail(item) {
    $('ccAgentBody').querySelectorAll('[data-cc-command]').forEach(button => button.onclick = () => {
      const command = button.dataset.ccCommand;
      if (command === 'history') return loadCommandHistory(item.source_name);
      runAgentCommand(item.source_name, command, {}, button);
    });
    $('ccScreenshotRefresh').onclick = () => runAgentCommand(item.source_name, 'screenshot', {}, $('ccScreenshotRefresh'), () => loadLatestScreenshot(item.source_name));
    $('ccArchiveToggle').onclick = async () => {
      const enabled = !item.archive_enabled;
      if (!await confirmAction((enabled ? 'Хранить' : 'Не хранить') + ' архив на «' + item.source_name + '»?')) return;
      const response = await ccApi('agents/' + encodeURIComponent(item.source_name) + '/archive', { method: 'POST', body: { enabled } });
      if (!response.data?.ok) return X.toast(response.data?.detail || 'Не удалось изменить архив');
      X.toast(enabled ? 'Локальный архив включён' : 'Локальный архив выключен');
      await X.loadBoot(); openAgent(item.source_name);
    };
  }

  async function runAgentCommand(source, command, payload = {}, button = null, after = null) {
    const dangerous = DANGEROUS.has(command);
    if (dangerous && !await confirmAction((COMMAND_LABELS[command] || command) + ' на «' + source + '»?')) return null;
    if (button) button.disabled = true;
    const status = $('ccCommandStatus');
    if (status) { status.className = 'cc-command-status'; status.textContent = (COMMAND_LABELS[command] || command) + ': отправляю…'; }
    try {
      if (X.demo) {
        await delay(350);
        const fake = demoCommand(command, payload);
        if (status) { status.classList.add('ok'); status.textContent = fake.result.message; }
        if (after) await after(fake);
        return fake;
      }
      const proof = dangerous ? await X.passkeyAction('agent:' + command + ':' + source) : '';
      const response = await ccApi('agents/' + encodeURIComponent(source) + '/commands', { method: 'POST', body: { command, payload, action_proof: proof } });
      if (!response.data?.ok) throw new Error(response.data?.detail || 'Команда не принята');
      if (status) status.textContent = (COMMAND_LABELS[command] || command) + ': агент выполняет…';
      const result = await waitForCommand(source, response.data.command.id);
      const ok = result.status === 'completed' && result.result?.ok !== false;
      if (!ok) throw new Error(result.result?.message || 'Команда завершилась ошибкой');
      if (status) { status.classList.add('ok'); status.textContent = result.result?.message || 'Готово'; }
      if (after) await after(result);
      return result;
    } catch (error) {
      if (status) { status.classList.add('bad'); status.textContent = error.message || 'Ошибка команды'; }
      if (error?.name !== 'NotAllowedError') X.toast(error.message || 'Команда не выполнена');
      return null;
    } finally { if (button) button.disabled = false; }
  }

  function demoCommand(command, payload) {
    let details = {};
    if (command === 'files_list') details = { root: payload.root, root_label: ROOTS[payload.root], path: payload.path || '', entries: [{ name: 'Проекты', type: 'directory', size: 0, modified_at: new Date().toISOString() }, { name: 'xass-notes.txt', type: 'file', size: 1842, modified_at: new Date(Date.now() - 3600000).toISOString() }] };
    if (command === 'clipboard_get') details = { text: 'Текст из буфера обмена Windows', length: 34 };
    if (command === 'file_download') details = { asset_token: 'demo', filename: String(payload.path || 'demo.txt').split('/').pop(), size: 128 };
    return { id: Date.now(), status: 'completed', result: { ok: true, message: (COMMAND_LABELS[command] || command) + ': готово', details } };
  }

  async function waitForCommand(source, id) {
    for (let attempt = 0; attempt < 55; attempt++) {
      const response = await ccApi('agents/' + encodeURIComponent(source) + '/commands?limit=40');
      const command = (response.data?.commands || []).find(item => Number(item.id) === Number(id));
      if (command && ['completed', 'failed', 'cancelled'].includes(command.status)) return command;
      await delay(1200);
    }
    throw new Error('Агент долго не отвечает. Команда остаётся в очереди.');
  }

  async function loadCommandHistory(source) {
    const card = $('ccCommandHistory'), list = $('ccCommandHistoryList');
    if (!card || !list) return;
    card.style.display = 'block'; list.innerHTML = '<div class="cc-skeleton" style="height:80px"></div>';
    $('ccHistoryClose').onclick = () => { card.style.display = 'none'; };
    try {
      const response = X.demo ? { data: { ok: true, commands: [demoCommand('ping', {}), demoCommand('screenshot', {})] } } : await ccApi('agents/' + encodeURIComponent(source) + '/commands?limit=20');
      if (!response.data?.ok) throw new Error(response.data?.detail || 'История недоступна');
      list.innerHTML = (response.data.commands || []).map(item => '<div class="cc-file-row"><div class="cc-file-icon">' + (item.status === 'completed' ? '✓' : item.status === 'failed' ? '!' : '…') + '</div><div><div class="cc-file-name">' + esc(COMMAND_LABELS[item.command] || item.command || 'Команда') + '</div><div class="cc-file-meta">' + esc(item.result?.message || item.status || 'ожидает') + '</div></div><div class="cc-file-meta">' + dateText(item.completed_at || item.created_at) + '</div></div>').join('') || '<div class="cc-empty">Команд ещё не было.</div>';
    } catch (error) { list.innerHTML = '<div class="cc-error">' + esc(error.message) + '</div>'; }
    card.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function loadLatestScreenshot(source) {
    const shell = $('ccScreenshot'), meta = $('ccScreenshotMeta');
    if (!shell) return;
    shell.innerHTML = '<div class="cc-screenshot-empty">Проверяю последний снимок…</div>';
    try {
      const response = await ccApi('agents/' + encodeURIComponent(source) + '/screenshot');
      if (!response.data?.available) {
        shell.innerHTML = '<div class="cc-screenshot-empty">Снимка пока нет. Нажмите «Обновить снимок» — агент сделает один кадр.</div>';
        if (meta) meta.textContent = 'Снимки заменяются и не накапливаются на VPS.';
        return;
      }
      const shot = response.data.screenshot;
      const raw = await rawRequest('agents/' + encodeURIComponent(source) + '/assets/' + encodeURIComponent(shot.token));
      if (!raw.ok) throw new Error('Не удалось загрузить снимок');
      const url = URL.createObjectURL(await raw.blob()); ui.objectUrls.add(url);
      shell.innerHTML = '<img src="' + url + '" alt="Последний снимок экрана ' + esc(source) + '">';
      if (meta) meta.textContent = 'Получен ' + dateText(shot.created_at) + ' · временное хранение';
      shell.querySelector('img').onclick = () => { $('ccLightboxImage').src = url; $('ccLightbox').classList.add('open'); };
    } catch (error) { shell.innerHTML = '<div class="cc-error">' + esc(error.message || 'Снимок недоступен') + '</div>'; }
  }

  function agentSelectHtml(id) {
    const agents = pcAgents(); ensureAgent();
    return '<label class="label" for="' + id + '"><span>Windows-агент</span><span>по запросу</span></label><select class="input" id="' + id + '">' + agents.map(item => '<option value="' + esc(item.source_name) + '" ' + (item.source_name === ui.activeAgent ? 'selected' : '') + '>' + esc(item.source_name) + (item.is_online ? ' · в сети' : ' · offline') + '</option>').join('') + '</select>';
  }

  function renderFilesPanel() {
    const host = $('ccDynamic-files'); if (!host) return;
    ensureAgent();
    host.innerHTML = '<div class="cc-card-title"><span>Файлы ПК</span><span class="cc-card-note">только 4 разрешённые папки</span></div>' + agentSelectHtml('ccFilesAgent') + '<div class="cc-root-tabs" id="ccRootTabs" style="margin-top:12px">' + Object.entries(ROOTS).map(([id, label]) => '<button class="cc-root-tab ' + (id === ui.fileRoot ? 'active' : '') + '" data-root="' + id + '">' + label + '</button>').join('') + '</div><div class="cc-breadcrumb" id="ccBreadcrumb"></div><div id="ccFileList"><div class="cc-empty">Выберите папку, чтобы запросить список у агента.</div></div><div class="toolbar" style="margin-top:12px"><button class="btn" id="ccFilesRefresh">Обновить</button><label class="cc-upload-label">Загрузить на ПК<input type="file" id="ccFileUpload" hidden></label></div><div class="cc-card-note" style="margin-top:10px">Каталог не копируется на сервер. XASS запрашивает только открытую папку.</div>';
    $('ccFilesAgent').onchange = event => { ui.activeAgent = event.target.value; loadFiles(); };
    host.querySelectorAll('[data-root]').forEach(button => button.onclick = () => { ui.fileRoot = button.dataset.root; ui.filePath = ''; renderFilesPanel(); loadFiles(); });
    $('ccFilesRefresh').onclick = loadFiles;
    $('ccFileUpload').onchange = event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) uploadFile(file); };
    loadFiles();
  }

  function renderBreadcrumb() {
    const host = $('ccBreadcrumb'); if (!host) return;
    const parts = ui.filePath.split('/').filter(Boolean);
    host.innerHTML = '<button data-path="">' + esc(ROOTS[ui.fileRoot]) + '</button>' + parts.map((part, index) => '<span>›</span><button data-path="' + esc(parts.slice(0, index + 1).join('/')) + '">' + esc(part) + '</button>').join('');
    host.querySelectorAll('button').forEach(button => button.onclick = () => { ui.filePath = button.dataset.path; loadFiles(); });
  }

  async function loadFiles() {
    const source = ensureAgent(), list = $('ccFileList');
    if (!list) return; renderBreadcrumb();
    if (!source) { list.innerHTML = '<div class="cc-empty">Сначала подключите Windows-агент.</div>'; return; }
    list.innerHTML = '<div class="cc-skeleton" style="height:62px"></div><div class="cc-skeleton" style="height:62px;margin-top:7px"></div>';
    const result = await runAgentCommand(source, 'files_list', { root: ui.fileRoot, path: ui.filePath });
    if (!result) { list.innerHTML = '<div class="cc-error">Агент не вернул содержимое папки.</div>'; return; }
    const details = result.result?.details || {}; ui.filePath = details.path || ui.filePath; renderBreadcrumb();
    const entries = details.entries || [];
    list.innerHTML = entries.length ? entries.map(item => '<div class="cc-file-row"><div class="cc-file-icon">' + (item.type === 'directory' ? '▰' : '▤') + '</div><button class="cc-file-open" data-name="' + esc(item.name) + '" data-type="' + esc(item.type) + '" style="border:0;background:transparent;color:inherit;text-align:left;min-width:0"><div class="cc-file-name">' + esc(item.name) + '</div><div class="cc-file-meta">' + (item.type === 'directory' ? 'Папка' : formatBytes(item.size)) + ' · ' + dateText(item.modified_at) + '</div></button><div class="cc-file-actions">' + (item.type === 'file' ? '<button class="btn" data-download="' + esc(item.name) + '" aria-label="Скачать">↓</button><button class="btn danger" data-delete="' + esc(item.name) + '" aria-label="Удалить">×</button>' : '<span class="cc-chevron">›</span>') + '</div></div>').join('') : '<div class="cc-empty">Папка пуста.</div>';
    list.querySelectorAll('.cc-file-open[data-type="directory"]').forEach(button => button.onclick = () => { ui.filePath = [ui.filePath, button.dataset.name].filter(Boolean).join('/'); loadFiles(); });
    list.querySelectorAll('[data-download]').forEach(button => button.onclick = () => downloadFile(button.dataset.download, button));
    list.querySelectorAll('[data-delete]').forEach(button => button.onclick = () => deleteFile(button.dataset.delete, button));
  }

  function joinedFilePath(name) { return [ui.filePath, name].filter(Boolean).join('/'); }
  async function downloadFile(name, button) {
    const source = ensureAgent(), result = await runAgentCommand(source, 'file_download', { root: ui.fileRoot, path: joinedFilePath(name) }, button);
    const token = result?.result?.details?.asset_token; if (!token) return;
    try {
      const response = await rawRequest('agents/' + encodeURIComponent(source) + '/assets/' + encodeURIComponent(token));
      if (!response.ok) throw new Error('Временный файл недоступен');
      const url = URL.createObjectURL(await response.blob()), anchor = document.createElement('a');
      anchor.href = url; anchor.download = result.result.details.filename || name; document.body.appendChild(anchor); anchor.click(); anchor.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (error) { X.toast(error.message); }
  }

  async function deleteFile(name, button) {
    const source = ensureAgent(), path = joinedFilePath(name);
    if (!await confirmAction('Удалить «' + name + '» с ПК? Это действие нельзя отменить.')) return;
    const result = await runAgentCommand(source, 'file_delete', { root: ui.fileRoot, path }, button);
    if (result) loadFiles();
  }

  async function uploadFile(file) {
    const source = ensureAgent();
    if (!source) return X.toast('Выберите агент');
    if (file.size > 32 * 1024 * 1024) return X.toast('Максимальный размер — 32 МБ');
    const upload = $('ccFileUpload'); if (upload) upload.disabled = true;
    try {
      const path = 'agents/' + encodeURIComponent(source) + '/files/upload?root=' + encodeURIComponent(ui.fileRoot) + '&path=' + encodeURIComponent(ui.filePath) + '&filename=' + encodeURIComponent(file.name);
      const response = await rawRequest(path, { method: 'POST', headers: { 'Content-Type': file.type || 'application/octet-stream', 'X-XASS-Filename': file.name }, body: file });
      const data = await response.json().catch(() => ({}));
      if (!response.ok || !data.ok) throw new Error(data.detail || 'Файл не принят');
      const result = X.demo ? demoCommand('file_upload', {}) : await waitForCommand(source, data.command.id);
      if (result.status !== 'completed') throw new Error(result.result?.message || 'Агент не сохранил файл');
      X.toast('Файл загружен на ПК'); loadFiles();
    } catch (error) { X.toast(error.message || 'Не удалось загрузить файл'); }
    finally { if (upload) upload.disabled = false; }
  }

  function renderClipboardPanel() {
    const host = $('ccDynamic-clipboard'); if (!host) return;
    ensureAgent();
    host.innerHTML = '<div class="cc-card-title"><span>Буфер обмена</span><span class="cc-card-note">только по вашему запросу</span></div>' + agentSelectHtml('ccClipboardAgent') + '<div class="field"><label class="label"><span>ПК → XASS</span><span>не отслеживается автоматически</span></label><div class="cc-clipboard-text" id="ccClipboardValue">Нажмите «Получить с ПК».</div><div class="toolbar" style="margin-top:8px"><button class="btn primary" id="ccClipboardGet">Получить с ПК</button><button class="btn" id="ccClipboardCopy" disabled>Скопировать</button></div></div><div class="field"><label class="label" for="ccClipboardSend"><span>XASS → ПК</span><span>до 64 КБ</span></label><textarea class="input" id="ccClipboardSend" maxlength="65536" placeholder="Текст для буфера Windows"></textarea><button class="btn blue" id="ccClipboardSet" style="width:100%;margin-top:8px">Отправить в буфер ПК</button></div><div class="cc-card-title" style="margin-top:18px"><span>Последние элементы</span><button class="btn" id="ccClipboardClear">Очистить</button></div><div class="cc-history-list" id="ccClipboardHistory"></div>';
    $('ccClipboardAgent').onchange = event => { ui.activeAgent = event.target.value; };
    $('ccClipboardGet').onclick = getClipboard;
    $('ccClipboardSet').onclick = setClipboard;
    $('ccClipboardClear').onclick = () => { ui.clipboardHistory = []; localStorage.removeItem('xass-clipboard-history'); renderClipboardHistory(); };
    renderClipboardHistory();
  }

  async function getClipboard() {
    const result = await runAgentCommand(ensureAgent(), 'clipboard_get', {}, $('ccClipboardGet'));
    if (!result) return;
    const text = String(result.result?.details?.text || '');
    $('ccClipboardValue').textContent = text || 'Буфер ПК пуст.';
    $('ccClipboardCopy').disabled = !text;
    $('ccClipboardCopy').onclick = () => X.copyText(text).then(() => X.toast('Скопировано'));
    storeClipboard(text, 'ПК → XASS');
  }

  async function setClipboard() {
    const text = $('ccClipboardSend').value;
    if (!text) return X.toast('Введите текст');
    const result = await runAgentCommand(ensureAgent(), 'clipboard_set', { text }, $('ccClipboardSet'));
    if (result) { storeClipboard(text, 'XASS → ПК'); X.toast('Текст отправлен в буфер ПК'); }
  }

  function renderClipboardHistory() {
    const host = $('ccClipboardHistory'); if (!host) return;
    host.innerHTML = ui.clipboardHistory.map((item, index) => '<button class="cc-history-item" data-history="' + index + '" style="color:inherit;text-align:left;background:#0a0f16"><p>' + esc(item.text.replace(/\s+/g, ' ')) + '</p><span>' + esc(item.direction) + '<br>' + dateText(item.at) + '</span></button>').join('') || '<div class="cc-empty">История хранится только в этом браузере.</div>';
    host.querySelectorAll('[data-history]').forEach(button => button.onclick = () => { const item = ui.clipboardHistory[Number(button.dataset.history)]; if (item) X.copyText(item.text).then(() => X.toast('Скопировано')); });
  }

  function timelinePanelHtml() {
    ensureAgent();
    return '<div class="cc-card-title"><span>Timeline</span><button class="btn" id="ccTimelineRefresh">Обновить</button></div><div class="cc-filter-row"><select class="input" id="ccTimelineType"><option value="">Все типы</option><option value="agent">Агенты</option><option value="command">Команды</option><option value="notification">Уведомления</option><option value="action">Действия</option></select><select class="input" id="ccTimelineDevice"><option value="">Все устройства</option>' + (X.state.boot?.sources || []).map(item => '<option>' + esc(item.source_name) + '</option>').join('') + '</select><select class="input" id="ccTimelineLevel"><option value="">Все уровни</option><option value="info">Info</option><option value="success">Success</option><option value="warning">Warning</option><option value="critical">Critical</option></select></div><div class="cc-timeline" id="ccTimelineList"></div>';
  }

  async function loadTimeline() {
    const host = $('ccDynamic-timeline'); if (!host) return;
    if (!$('ccTimelineList')) host.innerHTML = timelinePanelHtml();
    const list = $('ccTimelineList'); list.innerHTML = '<div class="cc-skeleton" style="height:72px"></div><div class="cc-skeleton" style="height:72px;margin-top:8px"></div>';
    const type = $('ccTimelineType')?.value || '', device = $('ccTimelineDevice')?.value || '', level = $('ccTimelineLevel')?.value || '';
    try {
      const query = new URLSearchParams({ limit: '100', event_type: type, device, level });
      const response = await ccApi('timeline?' + query.toString());
      if (!response.data?.ok) throw new Error(response.data?.detail || 'Timeline недоступен');
      const items = response.data.items || [];
      list.innerHTML = items.length ? items.map(item => '<div class="cc-event ' + esc(item.level || 'info') + '"><i class="cc-event-dot"></i><div><div class="cc-event-title">' + esc(item.title || item.event_type) + '</div><div class="cc-event-copy">' + esc(item.message || '') + '</div><div class="cc-event-meta"><span>' + dateText(item.created_at) + '</span>' + (item.device ? '<button data-open-agent="' + esc(item.device) + '" style="border:0;background:transparent;color:#8aa9ff;padding:0">' + esc(item.device) + '</button>' : '') + '<span>' + esc(item.level || 'info') + '</span></div></div></div>').join('') : '<div class="cc-empty">По этим фильтрам событий нет.</div>';
      list.querySelectorAll('[data-open-agent]').forEach(button => button.onclick = () => openAgent(button.dataset.openAgent));
    } catch (error) { list.innerHTML = '<div class="cc-error">' + esc(error.message) + '</div>'; }
    $('ccTimelineRefresh').onclick = loadTimeline;
    ['ccTimelineType', 'ccTimelineDevice', 'ccTimelineLevel'].forEach(id => { $(id).onchange = loadTimeline; });
  }

  const conditionLabels = {
    agent_offline: 'Агент offline', cpu_high: 'CPU выше порога', disk_low: 'Свободное место ниже',
    service_down: 'Сервис не работает', agent_outdated: 'Агент устарел',
  };

  function renderRulesPanel() {
    const host = $('ccDynamic-rules'); if (!host) return;
    const rules = X.state.boot?.rules || [];
    host.innerHTML = '<div class="cc-card-title"><span>Правила ЕСЛИ → ТО</span><button class="btn blue" id="ccRuleNew">Новое</button></div><div id="ccRuleList">' + (rules.map(ruleHtml).join('') || '<div class="cc-empty">Правил пока нет. Добавьте первое условие.</div>') + '</div><div class="cc-rule-form" id="ccRuleForm" hidden><input type="hidden" id="ccRuleId"><label class="label"><span>Название</span><span>IF → THEN</span></label><input class="input" id="ccRuleName" maxlength="120" placeholder="Например: Домашний ПК offline"><div class="cc-flow-marker">ЕСЛИ</div><select class="input" id="ccRuleCondition">' + Object.entries(conditionLabels).map(([value, label]) => '<option value="' + value + '">' + label + '</option>').join('') + '</select><select class="input" id="ccRuleDevice"><option value="">Любое устройство</option>' + (X.state.boot?.sources || []).map(item => '<option>' + esc(item.source_name) + '</option>').join('') + '</select><input class="input" id="ccRuleService" placeholder="Сервис, например xass-backend"><div class="form-grid"><label class="field"><span class="label">Порог</span><input class="input" id="ccRuleThreshold" type="number" min="0" max="10000" value="90"></label><label class="field"><span class="label">Длительность, мин</span><input class="input" id="ccRuleDuration" type="number" min="0" max="1440" value="5"></label></div><div class="cc-flow-marker">ТО</div><select class="input" id="ccRulePriority"><option value="info">Info-уведомление</option><option value="success">Success-уведомление</option><option value="warning" selected>Warning-уведомление</option><option value="critical">Critical-уведомление</option></select><label class="row"><input id="ccRuleEnabled" type="checkbox" checked><span>Правило включено</span></label><div class="toolbar"><button class="btn primary" id="ccRuleSave">Сохранить</button><button class="btn" id="ccRuleCancel">Отмена</button></div></div>';
    $('ccRuleNew').onclick = () => openRuleForm();
    host.querySelectorAll('[data-edit-rule]').forEach(button => button.onclick = () => openRuleForm(rules.find(item => item.id === button.dataset.editRule)));
    host.querySelectorAll('[data-delete-rule]').forEach(button => button.onclick = () => removeRule(button.dataset.deleteRule));
    $('ccRuleCancel').onclick = () => { $('ccRuleForm').hidden = true; };
    $('ccRuleSave').onclick = saveRule;
  }

  function ruleHtml(rule) {
    const target = rule.device || rule.service || 'вся система';
    return '<div class="cc-rule"><div class="cc-rule-head"><div><div class="cc-rule-name">' + esc(rule.name) + '</div><div class="cc-rule-flow">ЕСЛИ ' + esc(conditionLabels[rule.condition] || rule.condition) + ' · ' + esc(target) + ' → ТО ' + esc(rule.priority) + '</div></div><span class="status ' + (rule.enabled ? '' : 'off') + '">' + (rule.enabled ? 'Работает' : 'Выключено') + '</span></div><div class="cc-rule-actions"><button class="btn" data-edit-rule="' + esc(rule.id) + '">Изменить</button><button class="btn danger" data-delete-rule="' + esc(rule.id) + '">Удалить</button></div></div>';
  }

  function openRuleForm(rule = {}) {
    $('ccRuleForm').hidden = false;
    $('ccRuleId').value = rule.id || ''; $('ccRuleName').value = rule.name || '';
    $('ccRuleCondition').value = rule.condition || 'agent_offline'; $('ccRuleDevice').value = rule.device || '';
    $('ccRuleService').value = rule.service || ''; $('ccRuleThreshold').value = rule.threshold ?? 90;
    $('ccRuleDuration').value = rule.duration_minutes ?? 5; $('ccRulePriority').value = rule.priority || 'warning';
    $('ccRuleEnabled').checked = rule.enabled !== false; $('ccRuleForm').scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  async function saveRule() {
    const payload = { id: $('ccRuleId').value, name: $('ccRuleName').value.trim(), condition: $('ccRuleCondition').value, device: $('ccRuleDevice').value, service: $('ccRuleService').value.trim(), threshold: Number($('ccRuleThreshold').value || 0), duration_minutes: Number($('ccRuleDuration').value || 0), cooldown_minutes: 60, priority: $('ccRulePriority').value, enabled: $('ccRuleEnabled').checked };
    if (!payload.name) return X.toast('Введите название правила');
    const response = await ccApi('rules', { method: 'POST', body: payload });
    if (!response.data?.ok) return X.toast(response.data?.detail || 'Правило не сохранено');
    X.state.boot.rules = response.data.rules || []; renderRulesPanel(); X.toast('Правило сохранено');
  }

  async function removeRule(id) {
    if (!await confirmAction('Удалить это правило автоматизации?')) return;
    const response = await ccApi('rules/' + encodeURIComponent(id), { method: 'DELETE' });
    if (!response.data?.ok) return X.toast(response.data?.detail || 'Правило не удалено');
    X.state.boot.rules = response.data.rules || []; renderRulesPanel(); X.toast('Правило удалено');
  }

  function enhanceNotifications() {
    document.querySelectorAll('#notificationList .notification-item').forEach(row => {
      if (row.dataset.ccEnhanced) return;
      const text = row.querySelector('.log-meta span')?.textContent || '';
      const source = (X.state.boot?.sources || []).find(item => text.includes(item.source_name));
      if (!source) return;
      row.dataset.ccEnhanced = '1';
      const actions = row.querySelector('.notification-actions');
      if (actions) {
        const button = document.createElement('button'); button.className = 'btn blue'; button.textContent = 'Открыть агент';
        button.onclick = () => openAgent(source.source_name); actions.prepend(button);
      }
    });
  }

  function enhance() {
    if (!X.state.boot) return;
    buildToolsHub(); createOverlays(); renderHomeOverview(); renderCompactAgents();
    if (ui.activePanel === 'files') renderFilesPanel();
    if (ui.activePanel === 'clipboard') renderClipboardPanel();
    if (ui.activePanel === 'rules') renderRulesPanel();
    if (ui.activeAgent && $('ccAgentDetail')?.classList.contains('open')) openAgent(ui.activeAgent);
    setTimeout(enhanceNotifications, 250);
  }

  document.addEventListener('click', event => {
    const agent = event.target.closest('[data-cc-agent]');
    if (agent) { event.preventDefault(); openAgent(agent.dataset.ccAgent); }
    const filter = event.target.closest('[data-agent-filter]');
    if (filter) setTimeout(renderCompactAgents, 0);
  });
  document.addEventListener('xass:boot', () => setTimeout(enhance, 0));
  document.addEventListener('xass:view', event => {
    if (event.detail?.name === 'tools') setTimeout(() => { buildToolsHub(); renderCompactAgents(); enhanceNotifications(); }, 0);
  });
  window.addEventListener('beforeunload', () => ui.objectUrls.forEach(url => URL.revokeObjectURL(url)));

  if (X.state.boot) enhance();
})();
