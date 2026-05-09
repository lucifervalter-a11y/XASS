<?php declare(strict_types=1); ?>
<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>VK Auth — XASS</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: 'Manrope', sans-serif;
    background: #05070c;
    color: #e0e6f0;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }

  .card {
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.09);
    border-radius: 20px;
    padding: 44px 40px;
    max-width: 440px;
    width: 100%;
    backdrop-filter: blur(20px);
    -webkit-backdrop-filter: blur(20px);
    box-shadow:
      0 0 0 1px rgba(90,130,255,0.08),
      0 8px 40px rgba(0,0,0,0.55),
      0 0 80px rgba(60,100,255,0.07);
    text-align: center;
  }

  .icon {
    width: 64px;
    height: 64px;
    border-radius: 18px;
    background: rgba(90,130,255,0.12);
    border: 1px solid rgba(90,130,255,0.25);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 30px;
    margin: 0 auto 24px;
  }

  .icon.success { background: rgba(50,200,120,0.10); border-color: rgba(50,200,120,0.25); }
  .icon.error   { background: rgba(220,70,70,0.10);  border-color: rgba(220,70,70,0.25); }
  .icon.loading { background: rgba(90,130,255,0.10); border-color: rgba(90,130,255,0.25); }

  h1 {
    font-size: 22px;
    font-weight: 700;
    letter-spacing: -0.3px;
    margin-bottom: 12px;
    color: #f0f4ff;
  }

  p {
    font-size: 14px;
    line-height: 1.7;
    color: rgba(200,210,230,0.75);
  }

  .spinner {
    display: inline-block;
    width: 20px;
    height: 20px;
    border: 2.5px solid rgba(90,130,255,0.3);
    border-top-color: #6488ff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-bottom: 20px;
  }

  @keyframes spin { to { transform: rotate(360deg); } }

  .back-link {
    display: inline-block;
    margin-top: 28px;
    font-size: 13px;
    color: rgba(150,170,220,0.55);
    text-decoration: none;
    transition: color .2s;
  }
  .back-link:hover { color: rgba(150,170,220,0.9); }
</style>
</head>
<body>
<div class="card" id="card">
  <div class="icon loading" id="icon">
    <div class="spinner"></div>
  </div>
  <h1 id="title">Подключение…</h1>
  <p id="msg">Сохраняем токен ВКонтакте, подождите секунду.</p>
</div>

<script>
(function () {
  var icon  = document.getElementById('icon');
  var title = document.getElementById('title');
  var msg   = document.getElementById('msg');

  function showResult(ok, text) {
    icon.innerHTML = ok ? '✅' : '❌';
    icon.className = 'icon ' + (ok ? 'success' : 'error');
    title.textContent = ok ? 'ВКонтакте подключён!' : 'Ошибка авторизации';
    msg.textContent = text;
  }

  // Extract params from URL hash (VK implicit flow puts them there)
  var hash = (window.location.hash || '').replace(/^#/, '');
  var params = {};
  hash.split('&').forEach(function (part) {
    var kv = part.split('=');
    if (kv.length === 2) params[decodeURIComponent(kv[0])] = decodeURIComponent(kv[1]);
  });

  var accessToken = params['access_token'] || '';
  var userId = parseInt(params['user_id'] || '0', 10);

  // The secret is passed as a query param in the redirect_uri
  var searchParams = new URLSearchParams(window.location.search);
  var secret = searchParams.get('secret') || '';

  if (!accessToken || !userId) {
    showResult(false, 'Токен или user_id не найдены в ответе VK. Попробуйте авторизоваться снова через бота.');
    return;
  }
  if (!secret) {
    showResult(false, 'Параметр secret отсутствует. Используйте ссылку из бота.');
    return;
  }

  fetch('/api/vk/save-token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ access_token: accessToken, user_id: userId, secret: secret })
  })
  .then(function (res) {
    return res.json().then(function (data) {
      return { status: res.status, data: data };
    });
  })
  .then(function (result) {
    if (result.status === 200 && result.data && result.data.ok) {
      showResult(true, '✅ ВКонтакте подключён! Музыка теперь будет обновляться автоматически.');
    } else {
      var detail = (result.data && result.data.detail) ? result.data.detail : 'Неизвестная ошибка сервера.';
      showResult(false, detail);
    }
  })
  .catch(function (err) {
    showResult(false, 'Не удалось связаться с сервером: ' + err.message);
  });
})();
</script>
</body>
</html>
