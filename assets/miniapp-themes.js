(() => {
  'use strict';

  const THEMES = [
    {
      id: 'cyber',
      name: 'OLED Cyber Dark',
      subtitle: 'OLED Deep Black',
      desc: 'Глубокий черный, электрический синий #376dff и неоновый фиолетовый',
      accent: '#376dff',
      secondary: '#8b5cf6',
      colors: ['#000000', '#376dff', '#8b5cf6'],
      gradient: 'linear-gradient(135deg, #000000 0%, #0c1424 60%, #16102b 100%)'
    },
    {
      id: 'tokyo',
      name: 'Tokyo Night',
      subtitle: 'Midnight Navy',
      desc: 'Полуночный индиго #090d16, неоновая маджента #ff007f и кибер-циан',
      accent: '#ff007f',
      secondary: '#00f0ff',
      colors: ['#090d16', '#ff007f', '#00f0ff'],
      gradient: 'linear-gradient(135deg, #090d16 0%, #20122e 60%, #0d2335 100%)'
    },
    {
      id: 'nordic',
      name: 'Nordic Frost',
      subtitle: 'Arctic Slate',
      desc: 'Арктический сланец #0c111d, ледниковый лед и циан #38bdf8',
      accent: '#38bdf8',
      secondary: '#7dd3fc',
      colors: ['#0c111d', '#38bdf8', '#7dd3fc'],
      gradient: 'linear-gradient(135deg, #0c111d 0%, #14253d 60%, #0f2c42 100%)'
    },
    {
      id: 'matrix',
      name: 'Matrix Terminal',
      subtitle: 'Obsidian Emerald',
      desc: 'Обсидиановый темный #050a06, изумруд #10b981 и фосфорный мятный',
      accent: '#10b981',
      secondary: '#34d399',
      colors: ['#050a06', '#10b981', '#34d399'],
      gradient: 'linear-gradient(135deg, #050a06 0%, #0d2817 60%, #07381b 100%)'
    },
    {
      id: 'sunset',
      name: 'Sunset Amber',
      subtitle: 'Titanium Amber',
      desc: 'Темный титан #120e0c, теплый янтарь #f59e0b и багровый закат',
      accent: '#f59e0b',
      secondary: '#f43f5e',
      colors: ['#120e0c', '#f59e0b', '#f43f5e'],
      gradient: 'linear-gradient(135deg, #120e0c 0%, #2b1b13 60%, #30141b 100%)'
    },
    {
      id: 'glass',
      name: 'Liquid Glassmorphism',
      subtitle: '30px Frost Blur',
      desc: 'Матовое стекло 30px, полупрозрачные карточки и переливы',
      accent: '#818cf8',
      secondary: '#ec4899',
      colors: ['#060814', '#818cf8', '#ec4899'],
      gradient: 'linear-gradient(135deg, rgba(99,102,241,0.3) 0%, rgba(236,72,153,0.2) 50%, rgba(56,189,248,0.25) 100%)'
    }
  ];

  const SWATCHES = [
    '#376dff', '#ff007f', '#00f0ff', '#38bdf8',
    '#10b981', '#f59e0b', '#f43f5e', '#8b5cf6'
  ];

  const $ = id => document.getElementById(id);
  const esc = val => String(val ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));

  function toast(text) {
    if (window.XASS && typeof window.XASS.toast === 'function') {
      window.XASS.toast(text);
      return;
    }
    const t = $('toast');
    if (t) {
      t.textContent = text;
      t.classList.add('show');
      clearTimeout(toast.t);
      toast.t = setTimeout(() => t.classList.remove('show'), 2400);
    }
  }

  function haptic(kind = 'light') {
    try {
      window.Telegram?.WebApp?.HapticFeedback?.impactOccurred(kind);
    } catch (e) {}
  }

  function getActiveThemeId() {
    return localStorage.getItem('xass_theme') || 'cyber';
  }

  function getActiveAccent() {
    return localStorage.getItem('xass_accent') || '';
  }

  function getTheme(id) {
    return THEMES.find(t => t.id === id) || THEMES[0];
  }

  function applyTheme(themeId, notify = true) {
    const theme = getTheme(themeId);
    document.documentElement.setAttribute('data-theme', theme.id);
    localStorage.setItem('xass_theme', theme.id);

    // Update note / badge
    const note = $('currentThemeNote');
    if (note) note.textContent = theme.id.toUpperCase();

    // Update active cards
    document.querySelectorAll('[data-theme-choice]').forEach(card => {
      card.classList.toggle('active', card.dataset.themeChoice === theme.id);
    });

    // Update dot and accent pickers
    const customAccent = getActiveAccent();
    const effectiveAccent = customAccent || theme.accent;
    const dot = $('headThemeDot');
    if (dot) dot.style.background = effectiveAccent;

    ['toolsThemeAccentPicker', 'modalThemeAccentPicker'].forEach(id => {
      const el = $(id);
      if (el) el.value = effectiveAccent;
    });

    updateSwatchSelection(effectiveAccent, !customAccent);

    if (notify) {
      toast('Тема: ' + theme.name);
      haptic('light');
    }

    document.dispatchEvent(new CustomEvent('xass:themechange', {
      detail: { theme: theme.id, accent: effectiveAccent }
    }));
  }

  function applyAccent(color, notify = true) {
    if (!color || !/^#[0-9a-f]{6}$/i.test(color)) return;
    localStorage.setItem('xass_accent', color);

    document.documentElement.style.setProperty('--user-accent', color);
    document.documentElement.style.setProperty('--user-accent-soft', color + 'd0');
    document.documentElement.style.setProperty('--user-accent-glow', '0 0 24px ' + color + '66');

    const dot = $('headThemeDot');
    if (dot) dot.style.background = color;

    ['toolsThemeAccentPicker', 'modalThemeAccentPicker'].forEach(id => {
      const el = $(id);
      if (el) el.value = color;
    });

    updateSwatchSelection(color, false);

    if (notify) {
      toast('Акцент интерфейса обновлён');
      haptic('light');
    }

    document.dispatchEvent(new CustomEvent('xass:accentchange', {
      detail: { accent: color }
    }));
  }

  function resetAccent(notify = true) {
    localStorage.removeItem('xass_accent');
    document.documentElement.style.removeProperty('--user-accent');
    document.documentElement.style.removeProperty('--user-accent-soft');
    document.documentElement.style.removeProperty('--user-accent-glow');

    const currentTheme = getTheme(getActiveThemeId());
    const dot = $('headThemeDot');
    if (dot) dot.style.background = currentTheme.accent;

    ['toolsThemeAccentPicker', 'modalThemeAccentPicker'].forEach(id => {
      const el = $(id);
      if (el) el.value = currentTheme.accent;
    });

    updateSwatchSelection(currentTheme.accent, true);

    if (notify) {
      toast('Акцент сброшен к теме (' + currentTheme.name + ')');
      haptic('light');
    }

    document.dispatchEvent(new CustomEvent('xass:accentchange', {
      detail: { accent: currentTheme.accent, reset: true }
    }));
  }

  function updateSwatchSelection(color, isDefault) {
    document.querySelectorAll('.theme-accent-swatch').forEach(sw => {
      sw.classList.toggle('active', !isDefault && sw.dataset.color.toLowerCase() === color.toLowerCase());
    });
  }

  function openThemeModal() {
    const modal = $('themeModal');
    if (!modal) return;
    modal.classList.add('show');
    haptic('light');
  }

  function closeThemeModal() {
    const modal = $('themeModal');
    if (modal) modal.classList.remove('show');
  }

  function renderThemeGrid(containerId) {
    const container = $(containerId);
    if (!container) return;
    const current = getActiveThemeId();
    container.innerHTML = THEMES.map(t => `
      <div class="theme-card-option ${t.id === current ? 'active' : ''}" data-theme-choice="${t.id}" tabindex="0" role="button" aria-label="${esc(t.name)}">
        <div class="theme-card-head">
          <span class="theme-card-title">
            <span class="theme-swatch-dot glow" style="background:${t.accent}"></span>
            ${esc(t.name)}
          </span>
          <span class="theme-card-badge">Активна</span>
        </div>
        <div class="theme-card-desc">${esc(t.desc)}</div>
        <div class="theme-preview-palette" style="background:${t.gradient}">
          ${t.colors.map(c => `<span class="theme-swatch-dot" style="background:${c}"></span>`).join('')}
          <span class="theme-preview-name">${t.id}</span>
        </div>
      </div>
    `).join('');

    container.querySelectorAll('[data-theme-choice]').forEach(card => {
      card.addEventListener('click', () => applyTheme(card.dataset.themeChoice, true));
      card.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          applyTheme(card.dataset.themeChoice, true);
        }
      });
    });
  }

  function renderSwatches(containerId) {
    const container = $(containerId);
    if (!container) return;
    const currentAccent = getActiveAccent();
    container.innerHTML = SWATCHES.map(color => `
      <button class="theme-accent-swatch ${currentAccent && currentAccent.toLowerCase() === color.toLowerCase() ? 'active' : ''}"
        data-color="${color}" type="button" aria-label="Цвет ${color}" style="background:${color}"></button>
    `).join('');

    container.querySelectorAll('[data-color]').forEach(btn => {
      btn.addEventListener('click', () => applyAccent(btn.dataset.color, true));
    });
  }

  function init() {
    // 1. Initial State
    const activeTheme = getActiveThemeId();
    const customAccent = getActiveAccent();

    document.documentElement.setAttribute('data-theme', activeTheme);
    if (customAccent && /^#[0-9a-f]{6}$/i.test(customAccent)) {
      document.documentElement.style.setProperty('--user-accent', customAccent);
      document.documentElement.style.setProperty('--user-accent-soft', customAccent + 'd0');
      document.documentElement.style.setProperty('--user-accent-glow', '0 0 24px ' + customAccent + '66');
    }

    // 2. Render grids and swatches
    renderThemeGrid('toolsThemeGrid');
    renderThemeGrid('themeModalGrid');
    renderSwatches('toolsThemeSwatches');
    renderSwatches('modalThemeSwatches');

    // 3. Sync initial picker values
    const currentTheme = getTheme(activeTheme);
    const effectiveAccent = customAccent || currentTheme.accent;
    const dot = $('headThemeDot');
    if (dot) dot.style.background = effectiveAccent;

    ['toolsThemeAccentPicker', 'modalThemeAccentPicker'].forEach(id => {
      const el = $(id);
      if (el) {
        el.value = effectiveAccent;
        el.addEventListener('input', () => applyAccent(el.value, false));
        el.addEventListener('change', () => applyAccent(el.value, true));
      }
    });

    const note = $('currentThemeNote');
    if (note) note.textContent = activeTheme.toUpperCase();

    // 4. Reset Accent buttons
    const resetTools = $('toolsResetAccentBtn');
    if (resetTools) resetTools.onclick = () => resetAccent(true);
    const resetModal = $('modalResetAccentBtn');
    if (resetModal) resetModal.onclick = () => resetAccent(true);

    // 5. Open/Close Modal buttons
    const quickBtn = $('themeQuickBtn');
    if (quickBtn) quickBtn.onclick = openThemeModal;
    const chipBtn = $('quickThemeBtn');
    if (chipBtn) chipBtn.onclick = openThemeModal;
    const closeBtn = $('themeModalClose');
    if (closeBtn) closeBtn.onclick = closeThemeModal;

    const modal = $('themeModal');
    if (modal) {
      modal.onclick = event => {
        if (event.target === modal) closeThemeModal();
      };
    }

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && modal?.classList.contains('show')) {
        closeThemeModal();
      }
    });
  }

  // Expose API
  window.XASSTheme = {
    themes: THEMES,
    swatches: SWATCHES,
    applyTheme,
    applyAccent,
    resetAccent,
    openThemeModal,
    closeThemeModal,
    getActiveThemeId,
    getActiveAccent
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  document.addEventListener('xass:boot', () => {
    // Re-verify UI elements when XASS boots
    const activeTheme = getActiveThemeId();
    const note = $('currentThemeNote');
    if (note) note.textContent = activeTheme.toUpperCase();
    const dot = $('headThemeDot');
    if (dot) dot.style.background = getActiveAccent() || getTheme(activeTheme).accent;
    renderThemeGrid('toolsThemeGrid');
    renderThemeGrid('themeModalGrid');
    renderSwatches('toolsThemeSwatches');
    renderSwatches('modalThemeSwatches');
  });
})();
