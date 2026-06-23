<?php declare(strict_types=1); ?>
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, maximum-scale=1">
<title>XASS</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

:root {
  --bg: #04080f;
  --glass: rgba(12, 19, 36, 0.72);
  --glass-2: rgba(16, 24, 46, 0.6);
  --border: rgba(255,255,255,0.08);
  --border-hover: rgba(72,186,255,0.45);
  --accent: #48bafe;
  --accent2: #7c6ffe;
  --pink: #ff6b9d;
  --green: #43d39e;
  --red: #ff647c;
  --text: #eef2ff;
  --muted: #8aa0c8;
  --muted2: #aebfe0;
  --mono: 'JetBrains Mono', monospace;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
html { scroll-behavior: smooth; }
body {
  font-family: 'Manrope', system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  overflow-x: hidden;
  line-height: 1.5;
  padding-bottom: 88px;
}
/* orbs */
.orb { position: fixed; border-radius: 50%; filter: blur(80px); pointer-events: none; z-index: 0; }
.orb-1 { width: 420px; height: 360px; background: radial-gradient(ellipse, rgba(48,122,236,.30), transparent 70%); top:-16%; left:-22%; }
.orb-2 { width: 360px; height: 320px; background: radial-gradient(ellipse, rgba(124,111,254,.26), transparent 70%); top:6%; right:-26%; }
.orb-3 { width: 320px; height: 280px; background: radial-gradient(ellipse, rgba(30,200,220,.14), transparent 70%); bottom:8%; left:6%; }

.wrap { position: relative; z-index: 1; width: min(640px, 100%); margin: 0 auto; padding: 16px 14px 0; }

/* header */
.app-head { display:flex; align-items:center; gap:12px; padding: 6px 4px 16px; }
.app-ava { width:48px; height:48px; border-radius:14px; object-fit:cover; border:1px solid var(--border); background:linear-gradient(135deg,var(--accent),var(--accent2)); display:flex; align-items:center; justify-content:center; font-weight:800; font-size:20px; color:#fff; flex:0 0 auto; }
.app-head-meta { min-width:0; }
.app-head-name { font-weight:800; font-size:18px; letter-spacing:-.3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.app-head-sub { font-size:12.5px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.app-head-badge { margin-left:auto; flex:0 0 auto; font-size:11px; font-weight:700; padding:5px 10px; border-radius:999px; background:rgba(72,186,255,.12); color:var(--accent); border:1px solid rgba(72,186,255,.25); }

/* cards */
.card {
  background: var(--glass);
  border: 1px solid var(--border);
  border-radius: 20px;
  backdrop-filter: blur(20px) saturate(160%);
  -webkit-backdrop-filter: blur(20px) saturate(160%);
  box-shadow: 0 4px 8px rgba(0,0,0,.12), 0 16px 38px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,255,255,.05);
  padding: 16px;
  margin-bottom: 12px;
  transition: border-color .25s ease, transform .25s ease;
  animation: rise .4s ease both;
}
@keyframes rise { from { opacity:0; transform: translateY(14px); } to { opacity:1; transform:none; } }
.card-label { font-size:11px; font-weight:700; letter-spacing:1.4px; text-transform:uppercase; color:var(--muted); margin-bottom:12px; display:flex; align-items:center; gap:8px; }
.card-label .dot { width:7px; height:7px; border-radius:50%; background:var(--accent); box-shadow:0 0 10px var(--accent); }

/* now playing */
.np { display:flex; gap:14px; align-items:center; }
.np-art { width:64px; height:64px; border-radius:13px; object-fit:cover; flex:0 0 auto; background:rgba(255,255,255,.05); border:1px solid var(--border); box-shadow:0 6px 18px rgba(0,0,0,.4); }
.np-art.empty { display:flex; align-items:center; justify-content:center; font-size:26px; }
.np-meta { min-width:0; flex:1; }
.np-title { font-weight:700; font-size:16px; line-height:1.3; word-break:break-word; }
.np-sub { font-size:13px; color:var(--muted); margin-top:2px; }
.np-source { display:inline-flex; align-items:center; gap:5px; margin-top:7px; font-size:11px; font-weight:700; padding:3px 9px; border-radius:999px; background:rgba(124,111,254,.13); color:#b9b1ff; border:1px solid rgba(124,111,254,.25); }

.row { display:flex; align-items:center; gap:10px; }
.row + .row { margin-top:12px; }
.row-ico { font-size:20px; width:30px; text-align:center; flex:0 0 auto; }
.row-main { min-width:0; flex:1; }
.row-t { font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.6px; }
.row-v { font-size:14.5px; font-weight:600; word-break:break-word; }

/* source switch */
.seg { display:flex; gap:6px; margin-top:14px; background:rgba(255,255,255,.03); padding:5px; border-radius:13px; border:1px solid var(--border); }
.seg button { flex:1; border:none; background:transparent; color:var(--muted2); font-family:inherit; font-weight:700; font-size:13px; padding:9px 0; border-radius:9px; cursor:pointer; transition:all .2s; }
.seg button.on { background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff; box-shadow:0 4px 14px rgba(72,186,255,.3); }

/* metrics */
.metric { margin-bottom:14px; }
.metric:last-child { margin-bottom:0; }
.metric-top { display:flex; justify-content:space-between; font-size:13px; margin-bottom:7px; }
.metric-top .k { color:var(--muted2); font-weight:600; }
.metric-top .v { font-family:var(--mono); font-weight:600; }
.bar { height:9px; border-radius:99px; background:rgba(255,255,255,.06); overflow:hidden; }
.bar > span { display:block; height:100%; border-radius:99px; background:linear-gradient(90deg,var(--accent),var(--accent2)); transition:width .5s ease; }
.bar.warn > span { background:linear-gradient(90deg,#ffb454,#ff647c); }
.spark { width:100%; height:42px; display:block; margin-top:8px; }

.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.stat { background:rgba(255,255,255,.03); border:1px solid var(--border); border-radius:14px; padding:12px; }
.stat .k { font-size:11px; color:var(--muted); font-weight:600; text-transform:uppercase; letter-spacing:.5px; }
.stat .v { font-size:18px; font-weight:800; margin-top:3px; font-family:var(--mono); }

/* services + sources */
.chips { display:flex; flex-wrap:wrap; gap:7px; }
.chip { font-size:12px; font-weight:600; padding:6px 11px; border-radius:10px; background:rgba(255,255,255,.04); border:1px solid var(--border); display:inline-flex; align-items:center; gap:6px; }
.chip .d { width:7px; height:7px; border-radius:50%; }
.d.ok { background:var(--green); box-shadow:0 0 8px var(--green); }
.d.bad { background:var(--red); box-shadow:0 0 8px var(--red); }
.d.idle { background:var(--muted); }

.src { display:flex; align-items:center; gap:10px; padding:10px 0; border-bottom:1px solid var(--border); }
.src:last-child { border-bottom:none; }
.src-name { font-weight:700; font-size:14px; }
.src-type { font-size:11.5px; color:var(--muted); }
.src-st { margin-left:auto; font-size:11px; font-weight:700; padding:4px 10px; border-radius:999px; }
.src-st.on { background:rgba(67,211,158,.13); color:var(--green); border:1px solid rgba(67,211,158,.25); }
.src-st.off { background:rgba(255,100,124,.12); color:var(--red); border:1px solid rgba(255,100,124,.25); }

/* buttons / inputs */
.btn { width:100%; border:none; font-family:inherit; font-weight:700; font-size:15px; padding:14px; border-radius:14px; cursor:pointer; transition:transform .15s, box-shadow .2s; display:flex; align-items:center; justify-content:center; gap:9px; }
.btn:active { transform:scale(.98); }
.btn-primary { background:linear-gradient(135deg,var(--accent),var(--accent2)); color:#fff; box-shadow:0 6px 20px rgba(72,186,255,.3); }
.btn-vk { background:linear-gradient(135deg,#5181b8,#4a76a8); color:#fff; box-shadow:0 6px 20px rgba(74,118,168,.35); }
.btn-ghost { background:rgba(255,255,255,.05); color:var(--text); border:1px solid var(--border); }
.btn-row { display:flex; gap:9px; }
.btn-sm { font-size:13px; padding:10px 14px; border-radius:11px; width:auto; }

.field { display:flex; gap:8px; }
.input { flex:1; background:rgba(255,255,255,.04); border:1px solid var(--border); border-radius:13px; padding:13px 15px; color:var(--text); font-family:inherit; font-size:15px; outline:none; transition:border-color .2s; }
.input:focus { border-color:var(--border-hover); }
textarea.input { resize:vertical; min-height:54px; }

.links { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; }
.link-pill { font-size:12.5px; font-weight:700; padding:9px 13px; border-radius:11px; background:rgba(72,186,255,.09); color:var(--accent); border:1px solid rgba(72,186,255,.2); text-decoration:none; }

/* settings rows */
.set { display:flex; align-items:center; gap:12px; padding:13px 0; border-bottom:1px solid var(--border); }
.set:last-child { border-bottom:none; }
.set-main { flex:1; min-width:0; }
.set-t { font-weight:700; font-size:14.5px; }
.set-d { font-size:12px; color:var(--muted); margin-top:2px; }
.set-ctrl { flex:0 0 auto; }
.toggle { width:48px; height:28px; border-radius:99px; background:rgba(255,255,255,.1); border:1px solid var(--border); position:relative; cursor:pointer; transition:background .25s; flex:0 0 auto; }
.toggle::after { content:''; position:absolute; top:2px; left:2px; width:22px; height:22px; border-radius:50%; background:#fff; transition:transform .25s; }
.toggle.on { background:linear-gradient(135deg,var(--accent),var(--accent2)); }
.toggle.on::after { transform:translateX(20px); }
.pill-btn { font-size:13px; font-weight:700; padding:8px 13px; border-radius:10px; background:rgba(255,255,255,.05); border:1px solid var(--border); color:var(--text); font-family:inherit; cursor:pointer; }

/* quotes */
.quote-item { background:rgba(255,255,255,.03); border:1px solid var(--border); border-radius:14px; padding:13px 14px; margin-bottom:9px; display:flex; gap:10px; align-items:flex-start; }
.quote-text { flex:1; font-size:14px; line-height:1.5; }
.quote-id { font-family:var(--mono); font-size:10px; color:var(--muted); margin-top:5px; }
.quote-del { flex:0 0 auto; background:rgba(255,100,124,.1); border:1px solid rgba(255,100,124,.25); color:var(--red); width:32px; height:32px; border-radius:9px; cursor:pointer; font-size:15px; }

/* logs */
.log { padding:11px 0; border-bottom:1px solid var(--border); }
.log:last-child { border-bottom:none; }
.log-top { display:flex; align-items:center; gap:8px; font-size:12px; }
.log-chat { font-weight:700; }
.log-dir { font-size:10px; padding:2px 7px; border-radius:6px; background:rgba(255,255,255,.06); color:var(--muted2); }
.log-time { margin-left:auto; font-size:11px; color:var(--muted); font-family:var(--mono); }
.log-text { font-size:13.5px; margin-top:4px; color:var(--muted2); word-break:break-word; }
.log-flag { font-size:10px; font-weight:700; padding:2px 7px; border-radius:6px; }
.log-flag.del { background:rgba(255,100,124,.12); color:var(--red); }
.log-flag.ed { background:rgba(255,180,84,.12); color:#ffb454; }

.muted { color:var(--muted); font-size:13.5px; }
.center { text-align:center; }
.hint { font-size:12px; color:var(--muted); margin-top:10px; line-height:1.55; }

/* tabs */
.tabs { position:fixed; bottom:0; left:0; right:0; z-index:20; display:flex; gap:2px; padding:8px 8px calc(8px + env(safe-area-inset-bottom)); background:rgba(8,13,26,.85); backdrop-filter:blur(22px); -webkit-backdrop-filter:blur(22px); border-top:1px solid var(--border); }
.tab { flex:1; background:none; border:none; color:var(--muted); font-family:inherit; font-size:10px; font-weight:600; padding:6px 2px; cursor:pointer; display:flex; flex-direction:column; align-items:center; gap:3px; border-radius:12px; transition:color .2s, background .2s; }
.tab .ti { font-size:19px; }
.tab.on { color:var(--accent); background:rgba(72,186,255,.08); }

.view { display:none; }
.view.on { display:block; animation:rise .35s ease both; }

.skel { background:linear-gradient(90deg,rgba(255,255,255,.04),rgba(255,255,255,.09),rgba(255,255,255,.04)); background-size:200% 100%; animation:shimmer 1.3s infinite; border-radius:12px; }
@keyframes shimmer { from{background-position:200% 0;} to{background-position:-200% 0;} }
.spinner { width:22px; height:22px; border:2.5px solid rgba(72,186,255,.25); border-top-color:var(--accent); border-radius:50%; animation:spin .8s linear infinite; }
@keyframes spin { to { transform:rotate(360deg); } }
.loading-screen { position:fixed; inset:0; z-index:50; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:16px; background:var(--bg); }
.toast { position:fixed; left:50%; bottom:96px; transform:translateX(-50%) translateY(20px); z-index:60; background:rgba(20,28,50,.96); border:1px solid var(--border-hover); color:var(--text); font-size:13.5px; font-weight:600; padding:11px 18px; border-radius:13px; opacity:0; pointer-events:none; transition:opacity .25s, transform .25s; box-shadow:0 10px 30px rgba(0,0,0,.5); max-width:90%; }
.toast.show { opacity:1; transform:translateX(-50%) translateY(0); }
.deny { padding:60px 24px; text-align:center; }
.deny-ico { font-size:48px; margin-bottom:16px; }

/* update tab */
.commit-item { padding:10px 0; border-bottom:1px solid var(--border); }
.commit-item:last-child { border-bottom:none; }
.commit-hash { font-family:var(--mono); font-size:11px; color:var(--accent); }
.commit-msg { font-size:13.5px; font-weight:600; margin-top:2px; }
.commit-meta { font-size:11px; color:var(--muted); margin-top:2px; }
.badge-update { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:700; padding:5px 11px; border-radius:999px; background:rgba(72,186,255,.12); color:var(--accent); border:1px solid rgba(72,186,255,.25); margin-bottom:12px; }
.badge-ok { display:inline-flex; align-items:center; gap:6px; font-size:12px; font-weight:700; padding:5px 11px; border-radius:999px; background:rgba(67,211,158,.11); color:var(--green); border:1px solid rgba(67,211,158,.25); margin-bottom:12px; }
.upd-log { font-family:var(--mono); font-size:12px; color:var(--muted2); background:rgba(0,0,0,.3); border:1px solid var(--border); border-radius:12px; padding:12px; margin-top:12px; white-space:pre-wrap; word-break:break-word; max-height:200px; overflow-y:auto; }
</style>
</head>
<body>
<div class="orb orb-1"></div>
<div class="orb orb-2"></div>
<div class="orb orb-3"></div>

<div class="loading-screen" id="loading">
  <div class="spinner"></div>
  <div class="muted">Загрузка XASS…</div>
</div>

<div class="toast" id="toast"></div>

<div class="wrap" id="app" style="display:none">
  <header class="app-head">
    <div class="app-ava" id="ava">X</div>
    <div class="app-head-meta">
      <div class="app-head-name" id="hName">XASS</div>
      <div class="app-head-sub" id="hSub">панель управления</div>
    </div>
    <div class="app-head-badge" id="hBadge">—</div>
  </header>

  <!-- HOME -->
  <section class="view on" id="v-home">
    <div class="card">
      <div class="card-label"><span class="dot"></span> Сейчас слушаю</div>
      <div class="np">
        <div class="np-art empty" id="npArt">🎵</div>
        <div class="np-meta">
          <div class="np-title" id="npTitle">—</div>
          <div class="np-sub" id="npSub"></div>
          <span class="np-source" id="npSource" style="display:none"></span>
        </div>
      </div>
      <div class="seg" id="srcSeg">
        <button data-src="pc_agent">ПК</button>
        <button data-src="iphone">iPhone</button>
        <button data-src="vk">VK</button>
      </div>
    </div>

    <div class="card">
      <div class="card-label"><span class="dot"></span> Состояние</div>
      <div class="row">
        <div class="row-ico" id="wIco">🌤️</div>
        <div class="row-main"><div class="row-t">Погода</div><div class="row-v" id="wVal">—</div></div>
      </div>
      <div class="row">
        <div class="row-ico">🎮</div>
        <div class="row-main"><div class="row-t">Discord</div><div class="row-v" id="dcVal">—</div></div>
      </div>
      <div class="row">
        <div class="row-ico">🛰️</div>
        <div class="row-main"><div class="row-t">Агенты онлайн</div><div class="row-v" id="onVal">—</div></div>
      </div>
    </div>

    <div class="card" id="vkMini">
      <div class="card-label"><span class="dot"></span> ВКонтакте</div>
      <div class="row-v" id="vkMiniStatus" style="margin-bottom:12px">—</div>
      <button class="btn btn-vk" id="vkMiniBtn">Войти через ВКонтакте</button>
    </div>
  </section>

  <!-- MUSIC -->
  <section class="view" id="v-music">
    <div class="card">
      <div class="card-label"><span class="dot"></span> Поиск трека</div>
      <div class="field">
        <input class="input" id="muzInput" placeholder="Исполнитель — Трек">
        <button class="btn btn-primary btn-sm" id="muzGo">Найти</button>
      </div>
      <div class="hint">Пусто = текущий трек из статуса. Команда в боте: <b>.muz</b></div>
    </div>
    <div id="muzResult"></div>
  </section>

  <!-- SERVER -->
  <section class="view" id="v-server">
    <div class="card">
      <div class="card-label"><span class="dot"></span> Нагрузка сервера</div>
      <div class="metric">
        <div class="metric-top"><span class="k">CPU</span><span class="v" id="cpuV">—</span></div>
        <div class="bar" id="cpuBar"><span style="width:0"></span></div>
        <canvas class="spark" id="cpuSpark"></canvas>
      </div>
      <div class="metric">
        <div class="metric-top"><span class="k">RAM</span><span class="v" id="ramV">—</span></div>
        <div class="bar" id="ramBar"><span style="width:0"></span></div>
        <canvas class="spark" id="ramSpark"></canvas>
      </div>
      <div class="metric">
        <div class="metric-top"><span class="k">Диск</span><span class="v" id="diskV">—</span></div>
        <div class="bar" id="diskBar"><span style="width:0"></span></div>
      </div>
    </div>
    <div class="card">
      <div class="card-label"><span class="dot"></span> Сводка</div>
      <div class="grid2">
        <div class="stat"><div class="k">Аптайм</div><div class="v" id="upV">—</div></div>
        <div class="stat"><div class="k">Сеть ↓ / ↑</div><div class="v" id="netV" style="font-size:14px">—</div></div>
      </div>
    </div>
    <div class="card" id="svcCard">
      <div class="card-label"><span class="dot"></span> Сервисы</div>
      <div class="chips" id="svcChips"></div>
    </div>
    <div class="card">
      <div class="card-label"><span class="dot"></span> Источники</div>
      <div id="srcList"></div>
    </div>
  </section>

  <!-- SETTINGS -->
  <section class="view" id="v-settings">
    <div class="card" id="setCard">
      <div class="card-label"><span class="dot"></span> Настройки бота</div>
      <div class="set">
        <div class="set-main"><div class="set-t">Режим сохранения</div><div class="set-d" id="smDesc">—</div></div>
        <button class="pill-btn set-ctrl" id="smBtn">Сменить</button>
      </div>
      <div class="set">
        <div class="set-main"><div class="set-t">Таймаут связи</div><div class="set-d">Когда считать агент offline</div></div>
        <button class="pill-btn set-ctrl" id="toBtn">— мин</button>
      </div>
      <div class="set">
        <div class="set-main"><div class="set-t">Тихие часы</div><div class="set-d" id="qhDesc">—</div></div>
        <div class="toggle set-ctrl" id="qhTog"></div>
      </div>
      <div class="set">
        <div class="set-main"><div class="set-t">Не в сети (авто-ответ)</div><div class="set-d" id="awDesc">—</div></div>
        <div class="toggle set-ctrl" id="awTog"></div>
      </div>
    </div>
    <div class="card" id="awMsgCard">
      <div class="card-label"><span class="dot"></span> Текст авто-ответа</div>
      <textarea class="input" id="awMsg" rows="3"></textarea>
      <button class="btn btn-ghost btn-sm" id="awMsgSave" style="margin-top:10px">Сохранить текст</button>
    </div>
    <div class="card">
      <div class="card-label"><span class="dot"></span> Диагностика</div>
      <div class="hint" style="margin-top:0">Сводка состояния + последние логи в JSON — на случай если что-то не так.</div>
      <button class="btn btn-ghost btn-sm" id="diagBtn" style="margin-top:12px">📋 Скопировать диагностику</button>
    </div>
  </section>

  <!-- LOGS -->
  <section class="view" id="v-logs">
    <div class="card">
      <div class="card-label"><span class="dot"></span> Последние сообщения</div>
      <div id="logList"><div class="muted">Загрузка…</div></div>
      <button class="btn btn-ghost btn-sm" id="logRefresh" style="margin-top:12px">Обновить</button>
    </div>
  </section>

  <!-- QUOTES -->
  <section class="view" id="v-quotes">
    <div class="card" id="qAddCard">
      <div class="card-label"><span class="dot"></span> Новая цитата</div>
      <div class="field">
        <input class="input" id="qInput" placeholder="Текст цитаты для сайта">
        <button class="btn btn-primary btn-sm" id="qAdd">Добавить</button>
      </div>
      <div class="hint">Цитаты показываются на сайте в случайном порядке при каждом заходе.</div>
    </div>
    <div class="card">
      <div class="card-label"><span class="dot"></span> Все цитаты</div>
      <div id="qList"><div class="muted">Загрузка…</div></div>
    </div>
  </section>

  <!-- UPDATE -->
  <section class="view" id="v-update">
    <div class="card">
      <div class="card-label"><span class="dot"></span> Текущая версия</div>
      <div id="updCurrent"><div class="muted" style="font-size:13px">Нажмите «Проверить» для загрузки информации.</div></div>
    </div>
    <div class="card" id="updNewCard" style="display:none">
      <div class="card-label"><span class="dot"></span> Доступные обновления</div>
      <div id="updCommits"></div>
    </div>
    <div id="updResultCard" style="display:none">
      <div class="card">
        <div class="card-label"><span class="dot"></span> Результат обновления</div>
        <div id="updResultBody"></div>
      </div>
    </div>
    <div class="btn-row" style="margin-bottom:10px">
      <button class="btn btn-ghost" id="updRefreshBtn">🔄 Проверить</button>
      <button class="btn btn-primary" id="updGoBtn" style="display:none">⬆️ Обновить</button>
    </div>
    <div class="card">
      <div class="card-label"><span class="dot"></span> Быстрые команды</div>
      <div class="muted" style="font-size:13px; line-height:1.7">/update — панель обновления в боте<br>/start — главная панель<br>/status — статус heartbeat</div>
    </div>
  </section>

  <nav class="tabs">
    <button class="tab on" data-view="home"><span class="ti">🏠</span>Главная</button>
    <button class="tab" data-view="music"><span class="ti">🎵</span>Музыка</button>
    <button class="tab" data-view="server"><span class="ti">📊</span>Сервер</button>
    <button class="tab" data-view="settings"><span class="ti">⚙️</span>Настройки</button>
    <button class="tab" data-view="logs"><span class="ti">📝</span>Логи</button>
    <button class="tab" data-view="quotes"><span class="ti">💬</span>Цитаты</button>
    <button class="tab" data-view="update"><span class="ti">🔄</span>Апдейт</button>
  </nav>
</div>

<div class="deny" id="deny" style="display:none">
  <div class="deny-ico">🔒</div>
  <h2 style="margin-bottom:10px">Доступ ограничен</h2>
  <p class="muted" id="denyMsg">Откройте мини-приложение через кнопку в боте XASS.</p>
</div>

<script>
(function(){
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) { try { tg.ready(); tg.expand(); tg.setHeaderColor && tg.setHeaderColor('#04080f'); tg.setBackgroundColor && tg.setBackgroundColor('#04080f'); } catch(e){} }
  var initData = tg ? tg.initData : '';

  var $ = function(id){ return document.getElementById(id); };
  var state = { isOwner:false, settings:{}, status:{}, cpuHist:[], ramHist:[] };

  function haptic(type){ try { tg && tg.HapticFeedback && tg.HapticFeedback.impactOccurred(type||'light'); } catch(e){} }
  var toastT;
  function toast(msg){ var t=$('toast'); t.textContent=msg; t.classList.add('show'); clearTimeout(toastT); toastT=setTimeout(function(){t.classList.remove('show');},2200); }

  function api(path, opts){
    opts = opts || {};
    opts.headers = opts.headers || {};
    opts.headers['X-Telegram-Init-Data'] = initData;
    if (opts.body && typeof opts.body !== 'string') { opts.headers['Content-Type']='application/json'; opts.body=JSON.stringify(opts.body); }
    return fetch('/api/mini/' + path, opts).then(function(r){
      return r.json().then(function(d){ return { status:r.status, data:d }; });
    });
  }

  function esc(s){ return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];}); }
  function fmtUptime(sec){ sec=Number(sec)||0; var d=Math.floor(sec/86400), h=Math.floor(sec%86400/3600), m=Math.floor(sec%3600/60); if(d>0) return d+'д '+h+'ч'; if(h>0) return h+'ч '+m+'м'; return m+'м'; }
  function minToHHMM(v){ if(v==null) return '--:--'; v=((v%1440)+1440)%1440; return ('0'+Math.floor(v/60)).slice(-2)+':'+('0'+(v%60)).slice(-2); }

  // ---- tabs
  document.querySelectorAll('.tab').forEach(function(t){
    t.addEventListener('click', function(){
      haptic('light');
      document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('on');});
      document.querySelectorAll('.view').forEach(function(x){x.classList.remove('on');});
      t.classList.add('on');
      var v = t.getAttribute('data-view');
      $('v-'+v).classList.add('on');
      if (v==='logs') loadLogs();
      if (v==='quotes') loadQuotes();
      if (v==='update') loadUpdateStatus();
      window.scrollTo({top:0,behavior:'smooth'});
    });
  });

  // ---- render home / status
  function renderStatus(s){
    state.status = s;
    var noTrack = !s.now_listening || /^(не указано|нет данных|сейчас ничего не играет|vk: нет данных)/i.test(s.now_listening);
    if (noTrack){
      $('npTitle').textContent='Сейчас ничего не играет';
      $('npSub').textContent='';
      $('npArt').className='np-art empty'; $('npArt').textContent='🎵';
      $('npSource').style.display='none';
    } else {
      var parts = s.now_listening.split(' - ');
      $('npTitle').textContent = parts.length>1 ? parts.slice(1).join(' - ') : s.now_listening;
      $('npSub').textContent = parts.length>1 ? parts[0] : '';
      $('npSource').style.display='inline-flex';
      $('npSource').textContent = '♪ ' + (s.now_source_label||'');
      loadArt(s.now_listening);
    }
    // source segment
    document.querySelectorAll('#srcSeg button').forEach(function(b){
      b.classList.toggle('on', b.getAttribute('data-src')===s.now_source);
    });
    $('wVal').textContent = s.weather || 'Не указано';
    $('dcVal').textContent = s.discord_active ? ('Играет в ' + (s.discord_game||'—')) : 'Не в игре';
    // vk
    if (s.vk_connected){
      $('vkMiniStatus').innerHTML = '🟢 Подключён' + (s.vk_user_id?(' · id '+s.vk_user_id):'');
      $('vkMiniBtn').textContent = 'Переподключить ВКонтакте';
    } else {
      $('vkMiniStatus').innerHTML = '🔴 Не подключён';
      $('vkMiniBtn').textContent = 'Войти через ВКонтакте';
    }
  }

  var artCache = {};
  function loadArt(track){
    if (artCache[track]){ setArt(artCache[track]); return; }
    fetch('https://itunes.apple.com/search?term='+encodeURIComponent(track)+'&media=music&limit=1&entity=song')
      .then(function(r){return r.json();})
      .then(function(d){
        if(d.results && d.results.length){ var u=d.results[0].artworkUrl100.replace('100x100bb','300x300bb'); artCache[track]=u; setArt(u); }
      }).catch(function(){});
  }
  function setArt(url){ var el=$('npArt'); el.className='np-art'; el.innerHTML=''; el.style.backgroundImage='url('+url+')'; el.style.backgroundSize='cover'; el.style.backgroundPosition='center'; }

  function renderSettings(st){
    state.settings = st;
    var smMap = { SAVE_OFF:'Выключено', SAVE_BASIC:'Базовый', SAVE_FULL:'Полный (+медиа)', SAVE_PRIVATE_ONLY:'Только личные', SAVE_GROUPS_ONLY:'Только группы' };
    $('smDesc').textContent = smMap[st.save_mode] || st.save_mode;
    $('toBtn').textContent = st.timeout_minutes + ' мин';
    $('qhTog').classList.toggle('on', !!st.quiet_enabled);
    $('qhDesc').textContent = st.quiet_enabled ? ('Вкл · '+minToHHMM(st.quiet_start)+'–'+minToHHMM(st.quiet_end)) : 'Выключены';
    var awayActive = st.away_enabled || (st.away_until_at && new Date(st.away_until_at) > new Date());
    $('awTog').classList.toggle('on', !!awayActive);
    $('awDesc').textContent = awayActive ? (st.away_until_at && !st.away_enabled ? ('До '+new Date(st.away_until_at).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})) : 'Включён') : 'Выключен';
    $('awMsg').value = st.away_message || '';
  }

  function renderServer(b){
    var m = b.metrics || {};
    var cpu = Math.round(m.cpu_percent||0);
    var ramPct = m.ram_total_gb ? Math.round(m.ram_used_gb/m.ram_total_gb*100) : 0;
    var diskPct = m.disk_total_gb ? Math.round(m.disk_used_gb/m.disk_total_gb*100) : 0;
    setBar('cpu', cpu, cpu+'%');
    setBar('ram', ramPct, (m.ram_used_gb||0)+' / '+(m.ram_total_gb||0)+' ГБ');
    setBar('disk', diskPct, (m.disk_used_gb||0)+' / '+(m.disk_total_gb||0)+' ГБ');
    $('upV').textContent = fmtUptime(m.uptime_seconds);
    $('netV').textContent = (m.net_rx_mb||0)+' / '+(m.net_tx_mb||0)+' МБ';
    state.cpuHist.push(cpu); if(state.cpuHist.length>40) state.cpuHist.shift();
    state.ramHist.push(ramPct); if(state.ramHist.length>40) state.ramHist.shift();
    drawSpark('cpuSpark', state.cpuHist, '#48bafe');
    drawSpark('ramSpark', state.ramHist, '#7c6ffe');
    // services
    var svc = b.services || {};
    var keys = Object.keys(svc);
    if (!keys.length){ $('svcCard').style.display='none'; }
    else {
      $('svcCard').style.display='';
      $('svcChips').innerHTML = keys.map(function(k){
        var ok = svc[k]==='active';
        return '<span class="chip"><span class="d '+(ok?'ok':'bad')+'"></span>'+esc(k)+'</span>';
      }).join('');
    }
    // sources
    var src = b.sources || [];
    $('onVal').textContent = src.filter(function(x){return x.is_online;}).length + ' из ' + src.length;
    $('srcList').innerHTML = src.length ? src.map(function(x){
      return '<div class="src"><div><div class="src-name">'+esc(x.source_name)+'</div><div class="src-type">'+esc(x.source_type)+'</div></div>'+
        '<span class="src-st '+(x.is_online?'on':'off')+'">'+(x.is_online?'В СЕТИ':'OFFLINE')+'</span></div>';
    }).join('') : '<div class="muted">Источников пока нет.</div>';
  }

  function setBar(id, pct, label){
    pct = Math.max(0, Math.min(100, pct));
    $(id+'V').textContent = label;
    var bar = $(id+'Bar'); bar.firstElementChild.style.width = pct+'%';
    bar.classList.toggle('warn', pct>=85);
  }

  function drawSpark(id, data, color){
    var c = $(id); if(!c) return;
    var dpr = window.devicePixelRatio||1, w=c.clientWidth, h=c.clientHeight||42;
    c.width=w*dpr; c.height=h*dpr; var ctx=c.getContext('2d'); ctx.scale(dpr,dpr); ctx.clearRect(0,0,w,h);
    if(data.length<2) return;
    var max=100, step=w/(data.length-1);
    ctx.beginPath();
    data.forEach(function(v,i){ var x=i*step, y=h-(v/max)*(h-4)-2; i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
    ctx.lineTo(w,h); ctx.lineTo(0,h); ctx.closePath();
    var g=ctx.createLinearGradient(0,0,0,h); g.addColorStop(0,color+'55'); g.addColorStop(1,color+'00'); ctx.fillStyle=g; ctx.fill();
    ctx.beginPath();
    data.forEach(function(v,i){ var x=i*step, y=h-(v/max)*(h-4)-2; i?ctx.lineTo(x,y):ctx.moveTo(x,y); });
    ctx.strokeStyle=color; ctx.lineWidth=1.8; ctx.lineJoin='round'; ctx.stroke();
  }

  // ---- music
  function renderMusic(d){
    if (!d || !d.ok){ $('muzResult').innerHTML='<div class="card center muted">Ничего не найдено.</div>'; return; }
    var links = d.links || {};
    var linksHtml = Object.keys(links).map(function(k){
      return '<a class="link-pill" data-href="'+esc(links[k])+'" href="'+esc(links[k])+'">'+esc(k)+'</a>';
    }).join('');
    var art = d.artwork_url ? '<img class="np-art" src="'+esc(d.artwork_url)+'" style="width:84px;height:84px">' : '<div class="np-art empty" style="width:84px;height:84px;font-size:34px">🎵</div>';
    $('muzResult').innerHTML =
      '<div class="card"><div class="np" style="align-items:flex-start">'+art+
      '<div class="np-meta"><div class="np-title">'+esc(d.title||d.query)+'</div>'+
      (d.artist?'<div class="np-sub">'+esc(d.artist)+'</div>':'')+
      (d.album?'<div class="np-sub" style="font-size:12px">'+esc(d.album)+'</div>':'')+'</div></div>'+
      '<div class="links">'+linksHtml+'</div></div>';
    bindLinks();
  }
  function bindLinks(){
    document.querySelectorAll('#muzResult .link-pill, #vkMini a').forEach(function(a){
      a.addEventListener('click', function(e){ e.preventDefault(); var u=a.getAttribute('data-href')||a.href; openLink(u); });
    });
  }
  function openLink(u){ try { if(tg && tg.openLink){ tg.openLink(u); return; } } catch(e){} window.open(u,'_blank'); }

  function doMusic(){
    var q = $('muzInput').value.trim();
    $('muzResult').innerHTML='<div class="card center"><div class="spinner" style="margin:0 auto"></div></div>';
    api('music?q='+encodeURIComponent(q)).then(function(r){ renderMusic(r.data); }).catch(function(){ toast('Ошибка поиска'); });
  }
  $('muzGo').addEventListener('click', doMusic);
  $('muzInput').addEventListener('keydown', function(e){ if(e.key==='Enter') doMusic(); });

  // ---- logs
  function loadLogs(){
    $('logList').innerHTML='<div class="muted">Загрузка…</div>';
    api('logs?limit=30').then(function(r){
      var logs=(r.data&&r.data.logs)||[];
      if(!logs.length){ $('logList').innerHTML='<div class="muted">Архив пуст.</div>'; return; }
      $('logList').innerHTML = logs.map(function(l){
        var t = l.date ? new Date(l.date).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : '';
        var flags = (l.deleted?'<span class="log-flag del">удалено</span>':'')+(l.edited?'<span class="log-flag ed">правка</span>':'');
        return '<div class="log"><div class="log-top"><span class="log-chat">'+esc(l.chat_title||l.from_username||'—')+'</span>'+
          '<span class="log-dir">'+esc(l.direction)+'</span>'+flags+'<span class="log-time">'+t+'</span></div>'+
          (l.text?'<div class="log-text">'+esc(l.text)+'</div>':'')+'</div>';
      }).join('');
    }).catch(function(){ $('logList').innerHTML='<div class="muted">Ошибка загрузки.</div>'; });
  }
  $('logRefresh').addEventListener('click', function(){ haptic(); loadLogs(); });

  // ---- quotes
  function loadQuotes(){
    api('quotes').then(function(r){
      var q=(r.data&&r.data.quotes)||[]; renderQuotes(q);
    }).catch(function(){ $('qList').innerHTML='<div class="muted">Ошибка загрузки.</div>'; });
  }
  function renderQuotes(q){
    if(!q.length){ $('qList').innerHTML='<div class="muted">Цитат пока нет.</div>'; return; }
    $('qList').innerHTML = q.map(function(it){
      var del = state.isOwner ? '<button class="quote-del" data-id="'+esc(it.id)+'">✕</button>' : '';
      return '<div class="quote-item"><div class="quote-text">«'+esc(it.text)+'»<div class="quote-id">'+esc(it.id)+'</div></div>'+del+'</div>';
    }).join('');
    document.querySelectorAll('.quote-del').forEach(function(b){
      b.addEventListener('click', function(){
        haptic('medium');
        api('quotes/'+b.getAttribute('data-id'), {method:'DELETE'}).then(function(r){
          if(r.data&&r.data.ok){ toast('Цитата удалена'); renderQuotes(r.data.quotes); } else toast('Ошибка');
        });
      });
    });
  }
  $('qAdd').addEventListener('click', function(){
    var t=$('qInput').value.trim(); if(!t){ toast('Введите текст'); return; }
    api('quotes',{method:'POST',body:{text:t}}).then(function(r){
      if(r.data&&r.data.ok){ $('qInput').value=''; toast('Цитата добавлена'); renderQuotes(r.data.quotes); } else toast(r.data.detail||'Ошибка');
    });
  });

  // ---- settings actions (owner)
  function setOwnerUI(){
    if(state.isOwner) return;
    ['setCard','awMsgCard','qAddCard'].forEach(function(id){ var el=$(id); if(el) el.style.opacity='.55'; });
  }
  function pushSetting(key, value, cb){
    if(!state.isOwner){ toast('Только для владельца'); return; }
    haptic('medium');
    api('setting',{method:'POST',body:{key:key,value:value}}).then(function(r){
      if(r.status===200 && r.data && r.data.ok){ renderSettings(r.data.settings); if(r.data.status) renderStatus(r.data.status); cb&&cb(); }
      else toast((r.data&&r.data.detail)||'Ошибка');
    }).catch(function(){ toast('Ошибка сети'); });
  }
  $('smBtn').addEventListener('click', function(){ pushSetting('save_mode_cycle',null); });
  $('toBtn').addEventListener('click', function(){
    var opts=[5,10,30,60], cur=state.settings.timeout_minutes, next=opts[(opts.indexOf(cur)+1)%opts.length]||5;
    pushSetting('timeout', next);
  });
  $('qhTog').addEventListener('click', function(){ pushSetting('quiet_toggle',null); });
  $('awTog').addEventListener('click', function(){ pushSetting('away_toggle',null); });
  $('awMsgSave').addEventListener('click', function(){ pushSetting('away_message', $('awMsg').value, function(){ toast('Текст сохранён'); }); });
  document.querySelectorAll('#srcSeg button').forEach(function(b){
    b.addEventListener('click', function(){ pushSetting('now_source', b.getAttribute('data-src'), function(){ toast('Источник: '+b.textContent); }); });
  });

  // ---- VK
  function vkLogin(){
    var chatId = (tg && tg.initDataUnsafe && tg.initDataUnsafe.user) ? tg.initDataUnsafe.user.id : '';
    api('vk-url'+(chatId?('?chat_id='+chatId):'')).then(function(r){
      if(r.data && r.data.ok && r.data.url){ openLink(r.data.url); }
      else toast((r.data&&r.data.detail)||'VK недоступен');
    });
  }
  $('vkMiniBtn').addEventListener('click', vkLogin);

  // ---- update tab
  var updState = { hasUpdates: false, loading: false };

  function renderUpdateStatus(d) {
    if (!d || !d.ok) { $('updCurrent').innerHTML = '<div class="muted">Ошибка загрузки.</div>'; return; }
    updState.hasUpdates = !!d.has_updates;
    var cur = d.current;
    if (cur) {
      $('updCurrent').innerHTML =
        '<div class="badge-'+(d.has_updates?'update':'ok')+'">' +
        (d.has_updates ? '⬆️ Доступно обновление' : '✅ Версия актуальна') + '</div>' +
        '<div class="commit-item">' +
        '<div class="commit-hash">' + esc(cur.short_hash) + ' · ' + esc(d.branch) + '</div>' +
        '<div class="commit-msg">' + esc(cur.subject) + '</div>' +
        '<div class="commit-meta">' + esc(cur.author) + ' · ' + (cur.date ? cur.date.slice(0,10) : '') + '</div>' +
        '</div>';
    } else {
      $('updCurrent').innerHTML = '<div class="muted">Нет данных о текущем коммите.</div>';
    }
    if (d.has_updates && d.commits && d.commits.length) {
      $('updNewCard').style.display = '';
      $('updCommits').innerHTML = d.commits.map(function(c) {
        return '<div class="commit-item"><div class="commit-hash">' + esc(c.short_hash) + '</div>' +
          '<div class="commit-msg">' + esc(c.subject) + '</div>' +
          '<div class="commit-meta">' + esc(c.author) + ' · ' + (c.date ? c.date.slice(0,10) : '') + '</div></div>';
      }).join('');
    } else {
      $('updNewCard').style.display = 'none';
    }
    if (d.errors && d.errors.length) {
      toast('⚠️ ' + d.errors[0]);
    }
    $('updGoBtn').style.display = (d.has_updates && state.isOwner) ? '' : 'none';
  }

  function loadUpdateStatus() {
    if (updState.loading) return;
    updState.loading = true;
    $('updCurrent').innerHTML = '<div class="muted">Загрузка…</div>';
    $('updNewCard').style.display = 'none';
    $('updGoBtn').style.display = 'none';
    api('update-status').then(function(r) {
      updState.loading = false;
      if (r.status === 403 || r.status === 401) { toast('Только для владельца'); return; }
      renderUpdateStatus(r.data);
    }).catch(function() { updState.loading = false; toast('Ошибка проверки обновлений'); });
  }

  function doUpdate() {
    if (!state.isOwner) { toast('Только для владельца'); return; }
    if (updState.loading) return;
    haptic('medium');
    updState.loading = true;
    $('updGoBtn').style.display = 'none';
    $('updResultCard').style.display = 'none';
    $('updCurrent').innerHTML = '<div class="muted" style="display:flex;align-items:center;gap:10px"><div class="spinner"></div> Обновляю…</div>';
    api('run-update', {method:'POST'}).then(function(r) {
      updState.loading = false;
      var d = r.data || {};
      if (d.ok) {
        var lines = ['✅ Обновление успешно'];
        if (d.after) lines.push('Версия: ' + d.after.short_hash + ' — ' + d.after.subject);
        if (d.steps && d.steps.length) lines.push('Шаги: ' + d.steps.join(', '));
        if (d.restart_performed) lines.push('♻️ Сервис перезапущен');
        $('updResultBody').innerHTML = '<div class="upd-log">' + esc(lines.join('\n')) + '</div>';
      } else {
        $('updResultBody').innerHTML = '<div class="upd-log" style="color:var(--red)">❌ ' + esc(d.error || 'Ошибка') + '</div>';
      }
      $('updResultCard').style.display = '';
      loadUpdateStatus();
      toast(d.ok ? '✅ Обновлено!' : '❌ Ошибка обновления');
    }).catch(function() {
      updState.loading = false;
      $('updResultBody').innerHTML = '<div class="upd-log" style="color:var(--red)">❌ Нет ответа от сервера.</div>';
      $('updResultCard').style.display = '';
      toast('Ошибка сети');
    });
  }

  $('updRefreshBtn').addEventListener('click', function() { haptic(); loadUpdateStatus(); });
  $('updGoBtn').addEventListener('click', doUpdate);

  // ---- diagnostics
  $('diagBtn').addEventListener('click', function(){
    api('logs?limit=20').then(function(r){
      var diag = { generated_at:new Date().toISOString(), status:state.status, settings:state.settings, logs:(r.data&&r.data.logs)||[] };
      var text = JSON.stringify(diag, null, 2);
      try { navigator.clipboard.writeText(text); toast('Диагностика скопирована'); }
      catch(e){ if(tg&&tg.showPopup){ tg.showPopup({title:'Диагностика',message:text.slice(0,500)}); } else toast('Не удалось скопировать'); }
    });
  });

  // ---- boot
  function boot(){
    api('bootstrap').then(function(r){
      if(r.status===401 || r.status===403){
        $('loading').style.display='none';
        $('deny').style.display='block';
        if(r.status===403) $('denyMsg').textContent='Этот аккаунт не является владельцем XASS.';
        return;
      }
      if(!r.data || !r.data.ok){ $('loading').style.display='none'; $('deny').style.display='block'; return; }
      var b=r.data;
      state.isOwner = b.user && b.user.is_owner;
      $('loading').style.display='none';
      $('app').style.display='block';
      // header
      var nm = (b.status && b.status.name) || (b.user && b.user.first_name) || 'XASS';
      $('hName').textContent = nm;
      $('hSub').textContent = (b.status && b.status.title) || 'панель управления';
      $('hBadge').textContent = state.isOwner ? '👑 Владелец' : 'Гость';
      if (b.status && b.status.avatar_url){ var a=$('ava'); a.innerHTML=''; a.style.backgroundImage='url('+b.status.avatar_url+')'; a.style.backgroundSize='cover'; }
      else { $('ava').textContent = (nm[0]||'X').toUpperCase(); }
      renderStatus(b.status||{});
      renderSettings(b.settings||{});
      renderServer(b);
      setOwnerUI();
      bindLinks();
      startPolling();
    }).catch(function(){
      $('loading').style.display='none'; $('deny').style.display='block';
      $('denyMsg').textContent='Не удалось связаться с сервером.';
    });
  }

  var pollT;
  function startPolling(){
    clearInterval(pollT);
    pollT = setInterval(function(){
      api('bootstrap').then(function(r){
        if(r.data&&r.data.ok){ renderStatus(r.data.status||{}); renderServer(r.data); if(!document.activeElement || document.activeElement.tagName!=='TEXTAREA') renderSettings(r.data.settings||{}); }
      }).catch(function(){});
    }, 8000);
  }

  if(!tg || !initData){
    // Allow viewing layout in a normal browser only as denied.
    $('loading').style.display='none';
    $('deny').style.display='block';
    $('denyMsg').textContent='Откройте мини-приложение через кнопку в боте XASS (Telegram).';
    return;
  }
  boot();
})();
</script>
</body>
</html>
