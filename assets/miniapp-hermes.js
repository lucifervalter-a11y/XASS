/**
 * XASS AI - Frontend Client Logic for XASS Mini App
 * Displays only backend-confirmed sessions, messages, tool events, and status.
 * Version: 0.17.1
 */

(() => {
  'use strict';

  const X = window.XASS;
  const $ = id => document.getElementById(id);
  const tg = window.Telegram?.WebApp;
  const esc = X?.esc || (v => String(v ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch])));

  function haptic(type = 'light') {
    try { tg?.HapticFeedback?.impactOccurred(type); } catch (e) {}
  }

  function toast(msg) {
    if (X?.toast) return X.toast(msg);
    const t = $('toast');
    if (t) {
      t.textContent = msg;
      t.classList.add('show');
      clearTimeout(toast.timer);
      toast.timer = setTimeout(() => t.classList.remove('show'), 2600);
    }
  }

  function copyText(str) {
    if (X?.copyText) return X.copyText(str);
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(str);
    const ta = document.createElement('textarea');
    ta.value = str;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
    return Promise.resolve();
  }

  // --- State ---
  const STORAGE_KEY_SESSIONS = 'xass_hermes_sessions_v1';
  const STORAGE_KEY_ACTIVE = 'xass_hermes_active_session_v1';
  const STORAGE_KEY_MODEL = 'xass_hermes_model_v1';

  const state = {
    sessions: [],
    activeSessionId: '',
    messages: {}, // { [sessionId]: [messageObject] }
    model: localStorage.getItem(STORAGE_KEY_MODEL) || 'xass-ai-local',
    status: 'offline', // 'online' | 'thinking' | 'offline' | 'error'
    isGenerating: false,
    totalTokens: 0,
    lastError: '',
    interactiveAvailable: false,
    interactiveStatus: 'unavailable',
    initialized: false
  };

  // --- Markdown Parser ---
  function renderMarkdown(raw) {
    if (!raw) return '';
    let text = esc(raw);

    // Code blocks with syntax highlighting container
    text = text.replace(/```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g, (match, lang, code) => {
      lang = lang.trim() || 'plaintext';
      const cleanCode = code.replace(/\n$/, '');
      return `<div class="hermes-code-block" data-code="${cleanCode.replace(/"/g, '&quot;')}">
        <div class="hermes-code-header">
          <span>${lang}</span>
          <button type="button" class="hermes-code-copy-btn">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="13" height="13"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            Копировать
          </button>
        </div>
        <pre><code class="language-${lang}">${cleanCode}</code></pre>
      </div>`;
    });

    // Inline code
    text = text.replace(/`([^`]+)`/g, '<code>$1</code>');

    // Bold & italic
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\*([^*]+)\*/g, '<em>$1</em>');

    // Headers
    text = text.replace(/^### (.*$)/gim, '<h3>$1</h3>');
    text = text.replace(/^## (.*$)/gim, '<h2>$1</h2>');
    text = text.replace(/^# (.*$)/gim, '<h1>$1</h1>');

    // Blockquotes
    text = text.replace(/^\> (.*$)/gim, '<blockquote>$1</blockquote>');

    // Lists
    text = text.replace(/^\s*[-*]\s+(.*)$/gim, '<li>$1</li>');
    text = text.replace(/(<li>[\s\S]*?<\/li>)/g, '<ul>$1</ul>');
    text = text.replace(/<\/ul>\s*<ul>/g, '');

    // Paragraphs
    const paras = text.split(/\n{2,}/).map(p => {
      p = p.trim();
      if (!p) return '';
      if (/^<(h[1-3]|ul|ol|blockquote|div)/.test(p)) return p;
      return `<p>${p.replace(/\n/g, '<br>')}</p>`;
    }).filter(Boolean);

    return paras.join('');
  }

  // --- Session Management ---
  // Session metadata always comes from the backend. Local storage only remembers
  // the selected real session and its already displayed transcript.
  function loadPersistedSessions() {
    state.sessions = [];
    state.activeSessionId = localStorage.getItem(STORAGE_KEY_ACTIVE) || '';
  }

  function loadPersistedMessages(sessionId) {
    if (!sessionId || state.messages[sessionId]) return;
    try {
      const stored = localStorage.getItem('xass_hermes_msgs_' + sessionId);
      const parsed = stored ? JSON.parse(stored) : [];
      state.messages[sessionId] = Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      state.messages[sessionId] = [];
    }
  }

  function savePersistedSessions() {
    try {
      if (state.activeSessionId) {
        localStorage.setItem(STORAGE_KEY_ACTIVE, state.activeSessionId);
        localStorage.setItem(
          'xass_hermes_msgs_' + state.activeSessionId,
          JSON.stringify(state.messages[state.activeSessionId] || [])
        );
      } else {
        localStorage.removeItem(STORAGE_KEY_ACTIVE);
      }
      // Remove the legacy cached session catalog so stale/demo sessions cannot
      // be presented as live after an offline launch.
      localStorage.removeItem(STORAGE_KEY_SESSIONS);
    } catch (e) {
      console.warn('XASS AI could not persist the local transcript', e);
    }
  }

  function renderSessionSelector() {
    const sel = $('hermesSessionSelect');
    if (!sel) return;
    if (!state.sessions.length) {
      sel.innerHTML = '<option value="">Нет активных сессий</option>';
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML = state.sessions.map(s =>
      `<option value="${esc(s.id)}" ${s.id === state.activeSessionId ? 'selected' : ''}>${esc(s.title || 'Сессия XASS AI')}</option>`
    ).join('');
  }

  function updateTokenCounter() {
    const tokenEl = $('hermesTokenText');
    if (!tokenEl) return;
    const msgs = state.messages[state.activeSessionId] || [];
    const count = msgs.reduce((acc, m) => {
      const value = Number(m.tokens);
      return acc + (Number.isFinite(value) && value > 0 ? value : 0);
    }, 0);
    state.totalTokens = count;
    tokenEl.textContent = count > 0 ? count.toLocaleString('ru-RU') : '—';
  }

  // --- Rendering Chat History ---
  function renderChatHistory() {
    const history = $('hermesChatHistory');
    if (!history) return;

    const msgs = state.messages[state.activeSessionId] || [];
    if (!msgs.length) {
      const emptyMessage = state.interactiveAvailable === true && state.status === 'online'
        ? (state.activeSessionId
          ? 'Сессия готова. Ответы появятся только после подтверждения сервером.'
          : 'Создайте сессию, чтобы начать работу с XASS AI.')
        : state.status === 'runtime'
          ? 'Локальный runtime проверен, но интерактивный WebSocket-транспорт пока не подключён.'
        : 'XASS AI сейчас недоступен. Локальные демонстрационные ответы отключены.';
      history.innerHTML = `
        <div class="hermes-welcome-card">
          <div class="hermes-welcome-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <path d="M12 2a4 4 0 0 1 4 4v1a4 4 0 0 1 4 4v2a4 4 0 0 1-4 4h-1v3a2 2 0 0 1-2 2h-2a2 2 0 0 1-2-2v-3H8a4 4 0 0 1-4-4v-2a4 4 0 0 1 4-4V6a4 4 0 0 1 4-4Z"/>
              <circle cx="9" cy="11" r="1.5" fill="currentColor"/>
              <circle cx="15" cy="11" r="1.5" fill="currentColor"/>
              <path d="M9 15h6"/>
              <path d="M12 2v3M5 12H2M22 12h-3"/>
            </svg>
          </div>
          <h3>XASS AI</h3>
          <p>${esc(emptyMessage)}</p>
        </div>
      `;
      updateTokenCounter();
      return;
    }

    history.innerHTML = msgs.map(m => {
      const timeStr = m.timestamp ? new Date(m.timestamp).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '';

      if (m.role === 'user') {
        return `
          <div class="hermes-msg-row user" data-id="${esc(m.id)}">
            <div class="hermes-bubble-user">
              <div class="hermes-user-text">${esc(m.content)}</div>
              <div class="hermes-user-meta"><span>${timeStr}</span></div>
            </div>
          </div>
        `;
      }

      // Render only backend-confirmed tool events. Private model reasoning is
      // intentionally neither accepted nor displayed.
      const tools = Array.isArray(m.tools) ? m.tools : [];
      const toolsHtml = tools.length ? `
        <div class="hermes-tool-list" aria-label="Подтверждённые действия">
          ${tools.map(t => `
            <div class="hermes-tool-card">
              <div class="hermes-tool-head">
                <span class="hermes-tool-name">${esc(t.name || 'Действие')}</span>
                <span class="hermes-tool-badge ${t.status === 'ok' ? 'ok' : 'run'}">${esc(t.status || 'unknown')}</span>
              </div>
              ${t.payload ? `<div class="hermes-tool-content"><pre>${esc(JSON.stringify(t.payload, null, 2))}</pre></div>` : ''}
            </div>
          `).join('')}
        </div>` : '';
      const tokenValue = Number(m.tokens);
      const tokenMeta = Number.isFinite(tokenValue) && tokenValue > 0 ? `${tokenValue} токенов` : '';

      return `
        <div class="hermes-msg-row agent" data-id="${esc(m.id)}">
          <div class="hermes-bubble-agent">
            <div class="hermes-agent-head">
              <div class="hermes-agent-brand">
                <div class="hermes-agent-icon-badge">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2a4 4 0 0 1 4 4v1a4 4 0 0 1 4 4v2a4 4 0 0 1-4 4h-1v3a2 2 0 0 1-2 2h-2a2 2 0 0 1-2-2v-3H8a4 4 0 0 1-4-4v-2a4 4 0 0 1 4-4V6a4 4 0 0 1 4-4Z"/><circle cx="9" cy="11" r="1.5" fill="currentColor"/><circle cx="15" cy="11" r="1.5" fill="currentColor"/></svg>
                </div>
                <span class="hermes-agent-title">XASS AI</span>
              </div>
              <div class="hermes-agent-actions">
                <button type="button" class="hermes-copy-btn" title="Копировать ответ" data-copy-id="${esc(m.id)}">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
                </button>
              </div>
            </div>

            ${toolsHtml}

            <div class="hermes-bubble-content">
              ${renderMarkdown(m.content)}
            </div>

            <div class="hermes-agent-footer">
              <span>${timeStr}</span>
              ${tokenMeta ? `<span>${tokenMeta}</span>` : ''}
            </div>
          </div>
        </div>
      `;
    }).join('');

    bindCopyButtons();
    updateTokenCounter();
    scrollChatToBottom();
  }

  function scrollChatToBottom(smooth = true) {
    const history = $('hermesChatHistory');
    if (!history) return;
    requestAnimationFrame(() => {
      history.scrollTo({
        top: history.scrollHeight,
        behavior: smooth ? 'smooth' : 'auto'
      });
    });
  }

  function bindCopyButtons() {
    // Message copy
    document.querySelectorAll('.hermes-copy-btn').forEach(btn => {
      btn.onclick = async () => {
        const row = btn.closest('.hermes-msg-row');
        const msgId = btn.dataset.copyId;
        const msg = (state.messages[state.activeSessionId] || []).find(m => m.id === msgId);
        if (msg) {
          await copyText(msg.content);
          btn.classList.add('copied');
          btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="15" height="15"><polyline points="20 6 9 17 4 12"/></svg>';
          toast('Ответ скопирован в буфер');
          haptic('light');
          setTimeout(() => {
            btn.classList.remove('copied');
            btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
          }, 2000);
        }
      };
    });

    // Code block copy
    document.querySelectorAll('.hermes-code-copy-btn').forEach(btn => {
      btn.onclick = async () => {
        const block = btn.closest('.hermes-code-block');
        const code = block?.dataset?.code || block?.querySelector('code')?.innerText || '';
        await copyText(code);
        const originalText = btn.innerHTML;
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" width="13" height="13"><polyline points="20 6 9 17 4 12"/></svg> Скопировано';
        toast('Код скопирован');
        haptic('light');
        setTimeout(() => {
          btn.innerHTML = originalText;
        }, 2000);
      };
    });
  }

  // --- Status & Backend Connection ---
  function apiUrl(path) {
    return '/proxy.php?_passthrough=1&_p=' + encodeURIComponent('/api/mini/hermes/' + path);
  }

  function requestHeaders(json = false, actionProof = '') {
    const headers = {};
    if (json) headers['Content-Type'] = 'application/json';
    if (X?.initData) headers['X-Telegram-Init-Data'] = X.initData;
    if (actionProof) headers['X-XASS-Action-Proof'] = actionProof;
    return headers;
  }

  function isMockPayload(payload) {
    return Boolean(
      payload?.mock === true ||
      payload?.demo === true ||
      payload?.status?.mock === true ||
      payload?.result?.mock === true
    );
  }

  async function readJson(response) {
    try {
      return await response.json();
    } catch (e) {
      throw new Error('Сервер вернул некорректный ответ');
    }
  }

  async function loadXassAiSessions() {
    const response = await fetch(apiUrl('sessions'), {
      headers: requestHeaders(),
      credentials: 'include'
    });
    const data = await readJson(response);
    const payload = data?.result && typeof data.result === 'object' ? data.result : data;
    if (!response.ok || data?.ok !== true || isMockPayload(data)) {
      throw new Error('Сессии XASS AI недоступны');
    }

    const rawSessions = Array.isArray(payload.sessions) ? payload.sessions : [];
    state.sessions = rawSessions
      .filter(item => item && typeof item === 'object' && item.mock !== true && String(item.id || '').trim())
      .map(item => ({
        ...item,
        id: String(item.id),
        title: String(item.title || 'Сессия XASS AI')
      }));

    const selectedExists = state.sessions.some(item => item.id === state.activeSessionId);
    state.activeSessionId = selectedExists ? state.activeSessionId : (state.sessions[0]?.id || '');
    loadPersistedMessages(state.activeSessionId);
    savePersistedSessions();
    renderSessionSelector();
    renderChatHistory();
    updateControls();
  }

  async function loadHermesStatus() {
    try {
      const response = await fetch(apiUrl('status'), {
        headers: requestHeaders(),
        credentials: 'include'
      });
      const data = await readJson(response);
      const statusData = data?.status && typeof data.status === 'object' ? data.status : {};
      const statusName = String(statusData.status || '').toLowerCase();
      const runtimeOnline = response.ok &&
        data?.ok === true &&
        statusData.online === true &&
        statusData.enabled !== false &&
        statusData.mock !== true &&
        !['offline', 'offline_fallback', 'disabled', 'error'].includes(statusName);

      if (!runtimeOnline) {
        throw new Error('XASS AI не подтвердил готовность');
      }

      state.lastError = '';
      state.interactiveAvailable = statusData.interactive_available === true;
      state.interactiveStatus = String(statusData.interactive_status || 'unsupported_transport');
      if (!state.interactiveAvailable) {
        state.sessions = [];
        state.activeSessionId = '';
        setStatus('runtime', 'Runtime проверен · чат недоступен');
        renderSessionSelector();
        renderChatHistory();
        updateControls();
        return false;
      }
      if (typeof statusData.active_model === 'string' && statusData.active_model) {
        state.model = statusData.active_model;
        const modelSelect = $('hermesModelSelect');
        if (modelSelect && [...modelSelect.options].some(option => option.value === state.model)) {
          modelSelect.value = state.model;
        }
      }
      setStatus('online', 'В сети');
      await loadXassAiSessions();
      return true;
    } catch (e) {
      state.lastError = e instanceof Error ? e.message : 'XASS AI недоступен';
      state.interactiveAvailable = false;
      state.interactiveStatus = 'unavailable';
      state.sessions = [];
      state.activeSessionId = '';
      setStatus('offline', 'Не в сети');
      renderSessionSelector();
      renderChatHistory();
      updateControls();
      return false;
    }
  }

  function setStatus(status, label) {
    state.status = status;
    const pill = $('hermesStatusPill');
    const txt = $('hermesStatusText');
    if (!pill || !txt) return;

    const visualStatus = ['online', 'thinking', 'offline'].includes(status) ? status : 'offline';
    pill.className = `hermes-status-badge ${visualStatus}`;
    txt.textContent = label || (status === 'thinking' ? 'Выполняется…' : status === 'online' ? 'В сети' : 'Не в сети');
    updateControls();
  }

  function updateControls() {
    const sendBtn = $('hermesSendBtn');
    const newSessionBtn = $('hermesNewSessionBtn');
    const textarea = $('hermesPromptInput');
    const ready = state.status === 'online' && state.interactiveAvailable === true;
    if (sendBtn) sendBtn.disabled = !ready || !state.activeSessionId || state.isGenerating;
    if (newSessionBtn) newSessionBtn.disabled = !ready || state.isGenerating;
    if (textarea) textarea.disabled = !ready || !state.activeSessionId || state.isGenerating;
    document.querySelectorAll('.hermes-action-chip').forEach(chip => {
      chip.disabled = !ready || state.isGenerating;
    });
  }

  // --- Sending Message ---
  async function sendHermesMessage(text) {
    text = (text || '').trim();
    if (!text || state.isGenerating) return;
    if (state.status !== 'online' || state.interactiveAvailable !== true || !state.activeSessionId) {
      toast('Чат XASS AI недоступен: подтверждённый интерактивный транспорт не подключён.');
      return;
    }

    const sessionId = state.activeSessionId;
    state.isGenerating = true;
    haptic('medium');

    const userMsg = {
      id: 'msg-' + Date.now(),
      role: 'user',
      content: text,
      timestamp: new Date().toISOString()
    };
    if (!state.messages[sessionId]) state.messages[sessionId] = [];
    state.messages[sessionId].push(userMsg);
    savePersistedSessions();
    renderChatHistory();

    const input = $('hermesPromptInput');
    if (input) {
      input.value = '';
      input.style.height = 'auto';
    }

    setStatus('thinking', 'Выполняется…');
    const typing = $('hermesTypingIndicator');
    const sendBtn = $('hermesSendBtn');
    if (typing) typing.style.display = 'block';
    if (sendBtn) {
      const icon = sendBtn.querySelector('.send-icon');
      const spin = sendBtn.querySelector('.send-spin');
      if (icon) icon.style.display = 'none';
      if (spin) spin.style.display = 'block';
    }
    updateControls();
    scrollChatToBottom();

    try {
      const binding = { session_id: sessionId, prompt: text, model: state.model || null };
      const proof = typeof X?.passkeyAction === 'function'
        ? await X.passkeyAction('xass-ai:prompt', binding)
        : '';
      const response = await fetch(apiUrl('chat'), {
        method: 'POST',
        headers: requestHeaders(true, proof),
        body: JSON.stringify({
          session_id: sessionId,
          prompt: text,
          model: binding.model
        }),
        credentials: 'include'
      });
      const data = await readJson(response);
      const payload = data?.result && typeof data.result === 'object' ? data.result : data;
      const replyText = payload?.response || payload?.reply || payload?.text;

      if (!response.ok || data?.ok !== true || isMockPayload(data) || typeof replyText !== 'string' || !replyText.trim()) {
        throw new Error('Сервер не подтвердил ответ XASS AI');
      }

      const tokenCandidate = Number(payload.tokens_used ?? payload.tokens);
      state.messages[sessionId].push({
        id: 'resp-' + Date.now(),
        role: 'assistant',
        content: replyText.trim(),
        tools: Array.isArray(payload.tool_calls)
          ? payload.tool_calls
          : (Array.isArray(payload.tools) ? payload.tools : []),
        tokens: Number.isFinite(tokenCandidate) && tokenCandidate > 0 ? tokenCandidate : undefined,
        timestamp: new Date().toISOString()
      });
      state.lastError = '';
      setStatus('online', 'В сети');
    } catch (e) {
      state.lastError = e instanceof Error ? e.message : 'Не удалось получить ответ XASS AI';
      state.interactiveAvailable = false;
      state.interactiveStatus = 'request_failed';
      state.messages[sessionId].push({
        id: 'error-' + Date.now(),
        role: 'error',
        content: 'Не удалось получить ответ XASS AI. Проверьте подключение и повторите попытку.',
        timestamp: new Date().toISOString()
      });
      setStatus('error', 'Ошибка подключения');
    } finally {
      if (typing) typing.style.display = 'none';
      if (sendBtn) {
        const icon = sendBtn.querySelector('.send-icon');
        const spin = sendBtn.querySelector('.send-spin');
        if (icon) icon.style.display = 'block';
        if (spin) spin.style.display = 'none';
      }
      state.isGenerating = false;
      savePersistedSessions();
      renderChatHistory();
      updateControls();
    }
  }

  // --- Session Controls ---
  async function createNewSession() {
    if (state.status !== 'online' || state.interactiveAvailable !== true || state.isGenerating) {
      toast('Интерактивный транспорт XASS AI недоступен. Сессия не создана.');
      return;
    }

    state.isGenerating = true;
    updateControls();
    try {
      const binding = { title: null };
      const proof = typeof X?.passkeyAction === 'function'
        ? await X.passkeyAction('xass-ai:session-create', binding)
        : '';
      const response = await fetch(apiUrl('sessions'), {
        method: 'POST',
        headers: requestHeaders(true, proof),
        body: JSON.stringify({}),
        credentials: 'include'
      });
      const data = await readJson(response);
      const payload = data?.result && typeof data.result === 'object' ? data.result : data;
      const session = payload?.session;
      const sessionId = String(session?.id || '').trim();
      if (!response.ok || data?.ok !== true || isMockPayload(data) || session?.mock === true || !sessionId) {
        throw new Error('Сервер не подтвердил создание сессии XASS AI');
      }

      const confirmed = {
        ...session,
        id: sessionId,
        title: String(session.title || 'Сессия XASS AI')
      };
      state.sessions = [confirmed, ...state.sessions.filter(item => item.id !== sessionId)];
      state.activeSessionId = sessionId;
      state.messages[sessionId] = [];
      state.lastError = '';
      savePersistedSessions();
      renderSessionSelector();
      renderChatHistory();
      toast('Сессия XASS AI создана сервером');
      haptic('light');
    } catch (e) {
      state.lastError = e instanceof Error ? e.message : 'Сессия XASS AI не создана';
      toast(state.lastError);
    } finally {
      state.isGenerating = false;
      updateControls();
    }
  }

  function clearCurrentSession() {
    const doClear = () => {
      state.messages[state.activeSessionId] = [];
      savePersistedSessions();
      renderChatHistory();
      toast('Локальная копия истории скрыта');
      haptic('medium');
    };

    if (X?.ask) {
      X.ask('Скрыть локальную копию сообщений этой сессии XASS AI?', ok => {
        if (ok) doClear();
      });
    } else if (confirm('Скрыть локальную копию сообщений этой сессии XASS AI?')) {
      doClear();
    }
  }

  // --- Setup & Event Listeners ---
  function setupEventListeners() {
    // Model Select
    const modelSel = $('hermesModelSelect');
    if (modelSel) {
      modelSel.value = state.model;
      modelSel.onchange = () => {
        state.model = modelSel.value;
        localStorage.setItem(STORAGE_KEY_MODEL, state.model);
        toast(`Модель переключена: ${modelSel.options[modelSel.selectedIndex].text}`);
        haptic('light');
      };
    }

    // Session Select
    const sessionSel = $('hermesSessionSelect');
    if (sessionSel) {
      sessionSel.onchange = () => {
        state.activeSessionId = sessionSel.value;
        loadPersistedMessages(state.activeSessionId);
        savePersistedSessions();
        renderChatHistory();
        haptic('light');
      };
    }

    // New Session Button
    const newSessionBtn = $('hermesNewSessionBtn');
    if (newSessionBtn) {
      newSessionBtn.onclick = createNewSession;
    }

    // Clear Session Button
    const clearBtn = $('hermesClearSessionBtn');
    if (clearBtn) {
      clearBtn.onclick = clearCurrentSession;
    }

    // Quick Action Chips
    document.querySelectorAll('.hermes-action-chip').forEach(chip => {
      chip.onclick = () => {
        const prompt = chip.dataset.hermesPrompt || chip.textContent.trim();
        sendHermesMessage(prompt);
      };
    });

    // Auto-resizing Prompt Textarea
    const textarea = $('hermesPromptInput');
    if (textarea) {
      textarea.addEventListener('input', () => {
        textarea.style.height = 'auto';
        textarea.style.height = Math.min(160, Math.max(48, textarea.scrollHeight)) + 'px';
      });

      textarea.addEventListener('keydown', e => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          sendHermesMessage(textarea.value);
        }
      });
    }

    // Send Button & Form
    const sendBtn = $('hermesSendBtn');
    if (sendBtn) {
      sendBtn.onclick = () => {
        if (textarea) sendHermesMessage(textarea.value);
      };
    }

    const form = $('hermesChatForm');
    if (form) {
      form.onsubmit = e => {
        e.preventDefault();
        if (textarea) sendHermesMessage(textarea.value);
      };
    }

    // Quick Chip on Home View
    const homeChip = $('quickHermes');
    if (homeChip) {
      homeChip.onclick = () => {
        if (X?.showView) X.showView('hermes');
      };
    }

    // Navigation View Change Event
    document.addEventListener('xass:view', e => {
      if (e.detail?.name === 'hermes') {
        scrollChatToBottom(false);
        const ta = $('hermesPromptInput');
        if (ta && window.innerWidth > 600) {
          setTimeout(() => ta.focus(), 150);
        }
      }
    });

    // System Boot Event
    document.addEventListener('xass:boot', () => {
      loadHermesStatus();
    });
  }

  function init() {
    if (state.initialized) return;
    state.initialized = true;

    loadPersistedSessions();
    renderSessionSelector();
    renderChatHistory();
    setupEventListeners();
    loadHermesStatus();
  }

  // Auto-init on DOMContentLoaded or immediately if already loaded
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Export to window
  window.HermesAgent = {
    state,
    sendHermesMessage,
    loadHermesStatus,
    createNewSession,
    clearCurrentSession,
    renderChatHistory
  };

})();
