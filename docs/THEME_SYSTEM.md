# Multi-Theme System in XASS

## Обзор

Система **Multi-Theme Engine** предоставляет возможность настройки внешнего вида всех интерфейсов XASS: Web / Telegram Mini App, iOS-приложения и десктопного клиента. Движок включает 6 тем оформления и независимую палитру акцентных цветов с сохранением настроек пользователя в `localStorage` и мгновенным применением без мерцания (Zero-FOUC).

---

## Доступные темы оформления

Система содержит 6 интерфейсных тем, адаптированных под экраны OLED, темные помещения и условия яркого освещения:

| ID | Название | Стиль | Базовый фон | Акцентный цвет | Вторичный акцент | Особенности |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `cyber` | **OLED Cyber Dark** *(по умолчанию)* | OLED Deep Black | `#000000` | `#376dff` (Electric Blue) | `#8b5cf6` (Neon Violet) | Абсолютный черный фон для максимальной экономии батареи на OLED-дисплеях. |
| `tokyo` | **Tokyo Night** | Midnight Navy | `#090d16` | `#ff007f` (Neon Magenta) | `#00f0ff` (Cyber Cyan) | Японский киберпанк с контрастным сочетанием мадженты и неонового циана. |
| `nordic` | **Nordic Frost** | Arctic Slate | `#0c111d` | `#38bdf8` (Glacier Blue) | `#7dd3fc` (Ice Cyan) | Спокойная скандинавская палитра холодных грифельных и ледяных оттенков. |
| `matrix` | **Matrix Terminal** | Obsidian Emerald | `#050a06` | `#10b981` (Emerald Green) | `#34d399` (Mint Green) | Изумрудный хакерский терминал с глубоким зеленым свечением элементов. |
| `sunset` | **Sunset Amber** | Titanium Amber | `#120e0c` | `#f59e0b` (Warm Amber) | `#f43f5e` (Rose Red) | Теплая золотисто-янтарная гамма для комфортного чтения в вечернее время. |
| `glass` | **Liquid Glass** | Glassmorphism | `#060814` | `#818cf8` (Soft Indigo) | `#ec4899` (Glass Pink) | Жидкое полупрозрачное стекло, размытие 30px (`backdrop-filter: blur(30px)`), трехточечный радиальный градиент. |

---

## Независимая настройка акцентного цвета

Поверх любой из 6 базовых тем пользователь может установить собственный акцентный цвет:
- **Предустановленные сватчи**: `#376dff`, `#8b5cf6`, `#ff007f`, `#00f0ff`, `#10b981`, `#f59e0b`, `#f43f5e`.
- **Произвольный цвет**: через встроенный колор-пикер `<input type="color">`.
- **Сброс**: кнопка «По умолчанию» возвращает акцент темы.

При выборе акцента автоматически генерируются связанные оттенки:
- `--user-accent`: основной цвет (например, `#10b981`).
- `--user-accent-soft`: цвет с прозрачностью 80% для ховеров и бейджей.
- `--user-accent-glow`: рассеянное свечение `0 0 24px rgba(...)` для кнопок и ползунков.

---

## Архитектура CSS Custom Variables

Темы оформлены в файле [`assets/miniapp-themes.css`](file:///C:/Users/Lucif/.gemini/antigravity/scratch/XASS_analysis/assets/miniapp-themes.css). Архитектура состоит из трех уровней:

### 1. Переменные уровня темы (`[data-theme="<id>"]`)

Каждая тема объявляет собственный набор переменных с префиксом `--theme-*`:

```css
[data-theme="tokyo"],
html[data-theme="tokyo"] {
  --theme-bg: #090d16;
  --theme-bg-gradient: radial-gradient(...);
  --theme-surface: #0e1524;
  --theme-surface2: #162035;
  --theme-card: #111a2d;
  --theme-card-hover: #192742;
  --theme-card-bg: linear-gradient(145deg, rgba(25, 39, 66, 0.85), rgba(12, 18, 32, 0.95));
  --theme-card-border: rgba(255, 0, 127, 0.22);
  --theme-card-blur: 16px;
  --theme-line: #1e2c47;
  --theme-text: #f0f4fc;
  --theme-text-muted: #8b9bb4;
  --theme-blue-bright: #00f0ff;
  --theme-accent: #ff007f;
  --theme-accent-soft: #ff4d9e;
  --theme-accent-glow: 0 0 24px rgba(255, 0, 127, 0.5);
  --theme-tab-bg: rgba(12, 18, 32, 0.92);
  --theme-tab-border: rgba(255, 0, 127, 0.25);
  --theme-head-bg: rgba(9, 13, 22, 0.88);
  --theme-btn-bg: #152037;
  --theme-btn-border: #233455;
  --theme-btn-primary-bg: linear-gradient(135deg, #d6006b, #ff007f);
  --input-bg: #0b111c;
  --input-border: #1d2b45;
  --theme-focus-ring: rgba(255, 0, 127, 0.3);
}
```

### 2. Глобальные отображения в `:root`

Блок `:root` маппит переменные приложения на переменные текущей темы с приоритетом пользовательского акцента:

```css
:root {
  --accent: var(--user-accent, var(--theme-accent, #376dff));
  --accent-soft: var(--user-accent-soft, var(--theme-accent-soft, #8bb0ff));
  --accent-glow: var(--user-accent-glow, var(--theme-accent-glow, 0 0 22px rgba(55, 109, 255, 0.45)));
  --bg: var(--theme-bg, #000000);
  --surface: var(--theme-surface, #0d1117);
  --surface2: var(--theme-surface2, #161b22);
  --card: var(--theme-card, #0f141c);
  --card-hover: var(--theme-card-hover, #161d28);
  --card-bg: var(--theme-card-bg, #0f141c);
  --card-border: var(--theme-card-border, #1f2733);
  --card-blur: var(--theme-card-blur, 16px);
  --line: var(--theme-line, #1c232e);
  --text: var(--theme-text, #ffffff);
  --text-muted: var(--theme-text-muted, #7d8590);
  --blue-bright: var(--theme-blue-bright, #8bb0ff);
  /* Поддержка музыкального плеера и центра управления */
  --cc-card: var(--card);
  --cc-line: var(--line);
  --xm-bg: var(--bg);
  --xm-surface: var(--surface);
  --xm-line: var(--line);
}
```

### 3. Инлайн-инициализация без FOUC

В шапку [`miniapp.php`](file:///C:/Users/Lucif/.gemini/antigravity/scratch/XASS_analysis/miniapp.php) встроен синхронный скрипт, гарантирующий установку темы до первой отрисовки DOM:

```html
<script>
(function(){
  try {
    var t = localStorage.getItem('xass_theme') || 'cyber';
    document.documentElement.setAttribute('data-theme', t);
    var a = localStorage.getItem('xass_accent');
    if (a && /^#[0-9a-f]{6}$/i.test(a)) {
      document.documentElement.style.setProperty('--user-accent', a);
      document.documentElement.style.setProperty('--user-accent-soft', a + 'd0');
      document.documentElement.style.setProperty('--user-accent-glow', '0 0 24px ' + a + '66');
    }
  } catch (e) {}
})();
</script>
```

---

## JavaScript API (`window.XASSTheme`)

Модуль [`assets/miniapp-themes.js`](file:///C:/Users/Lucif/.gemini/antigravity/scratch/XASS_analysis/assets/miniapp-themes.js) предоставляет глобальный API:

- `XASSTheme.applyTheme(themeId)` — применить тему по ID (`cyber`, `tokyo`, `nordic`, `matrix`, `sunset`, `glass`).
- `XASSTheme.applyAccent(hexColor)` — применить пользовательский акцентный цвет.
- `XASSTheme.resetAccent()` — сбросить акцент к значению темы по умолчанию.
- `XASSTheme.openThemeModal()` — открыть диалог выбора оформления.
- `XASSTheme.closeThemeModal()` — закрыть диалог.
- `XASSTheme.getActiveThemeId()` — получить текущий ID темы.
- `XASSTheme.getActiveAccent()` — получить текущий акцент (или `null`).

---

## Команда в Telegram-боте

Переключение темы доступно через команду:

```
/theme <название>
```

Примеры:
- `/theme tokyo` — переключить на Tokyo Night
- `/theme matrix` — переключить на Matrix Emerald
- `/theme glass` — переключить на Liquid Glass
- `/theme cyber` — вернуть тему OLED по умолчанию
- `/theme` — показать список тем и текущее состояние

---

## Руководство по добавлению новой темы

1. Откройте `assets/miniapp-themes.css`.
2. Добавьте блок селектора:
   ```css
   [data-theme="mytheme"],
   html[data-theme="mytheme"] {
     --theme-bg: #...;
     --theme-surface: #...;
     --theme-surface2: #...;
     --theme-card: #...;
     --theme-card-hover: #...;
     --theme-card-bg: ...;
     --theme-card-border: ...;
     --theme-card-blur: 16px;
     --theme-line: #...;
     --theme-text: #...;
     --theme-text-muted: #...;
     --theme-blue-bright: #...;
     --theme-accent: #...;
     --theme-accent-soft: #...;
     --theme-accent-glow: ...;
     --theme-tab-bg: ...;
     --theme-tab-border: ...;
     --theme-head-bg: ...;
     --theme-btn-bg: ...;
     --theme-btn-border: ...;
     --theme-btn-primary-bg: ...;
     --input-bg: ...;
     --input-border: ...;
     --theme-focus-ring: ...;
   }
   ```
3. Зарегистрируйте тему в массиве `THEMES` в `assets/miniapp-themes.js`.
4. Добавьте тему в `VALID_THEMES` в `tests/test_theme_system.py` и запустите тесты.
