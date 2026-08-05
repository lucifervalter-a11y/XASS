<?php
declare(strict_types=1);

function xass_escape(mixed $value): string
{
    return htmlspecialchars(trim((string)$value), ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function xass_json(string $path, mixed $fallback): mixed
{
    if (!is_readable($path)) {
        return $fallback;
    }
    $raw = file_get_contents($path);
    if (!is_string($raw) || trim($raw) === '') {
        return $fallback;
    }
    $value = json_decode($raw, true);
    return json_last_error() === JSON_ERROR_NONE ? $value : $fallback;
}

function xass_text(mixed $value, string $fallback = ''): string
{
    if (!is_scalar($value)) {
        return $fallback;
    }
    $clean = trim((string)$value);
    return $clean !== '' ? $clean : $fallback;
}

function xass_is_music_noise(string $value): bool
{
    if ($value === '') {
        return true;
    }
    return preg_match(
        '/нет (?:свежих )?данных|ничего не играет|не указано|не в сети|github|gitlab|chatgpt|opera gx|google chrome|microsoft edge|mozilla firefox|яндекс браузер|^-?\s*$/iu',
        $value
    ) === 1;
}

function xass_source_label(string $source): string
{
    return match (strtolower(trim($source))) {
        'iphone' => 'iPhone',
        'vk' => 'VK Music',
        'pc_agent' => 'Windows',
        default => $source !== '' ? $source : 'Источник',
    };
}

function xass_icon(string $name, string $class = ''): string
{
    $attrs = 'class="' . xass_escape($class) . '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"';
    return match ($name) {
        'arrow-up-right' => '<svg ' . $attrs . '><path d="M7 17 17 7M8 7h9v9"/></svg>',
        'arrow-right' => '<svg ' . $attrs . '><path d="M5 12h14m-5-5 5 5-5 5"/></svg>',
        'arrow-left' => '<svg ' . $attrs . '><path d="M19 12H5m5 5-5-5 5-5"/></svg>',
        'shuffle' => '<svg ' . $attrs . '><path d="M16 3h5v5M4 20 21 3M21 16v5h-5M15 15l6 6M4 4l5 5"/></svg>',
        'telegram' => '<svg ' . $attrs . '><path d="m21 3-7.4 18-4.2-6.4L3 11.2 21 3Z"/><path d="m9.4 14.6 4.1-3.7"/></svg>',
        'github' => '<svg ' . $attrs . '><path d="M15 22v-4a4.8 4.8 0 0 0-1-3.5c3.2-.4 6.5-1.6 6.5-7A5.4 5.4 0 0 0 19 3.8 5 5 0 0 0 18.9.1S17.7-.3 15 1.6a13.4 13.4 0 0 0-7 0C5.3-.3 4.1.1 4.1.1A5 5 0 0 0 4 3.8a5.4 5.4 0 0 0-1.5 3.7c0 5.4 3.3 6.6 6.5 7A4.8 4.8 0 0 0 8 18v4"/><path d="M8 19c-3 .9-3-1.5-4-2"/></svg>',
        'steam' => '<svg ' . $attrs . '><path d="M8.6 16.8 5 15.3a3 3 0 1 1-1.8 3.4"/><circle cx="15.5" cy="8.5" r="4.5"/><circle cx="5.5" cy="18" r="2.5"/><path d="m7.7 16.6 4.6-4.5"/></svg>',
        'link' => '<svg ' . $attrs . '><path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.1 1.1"/><path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.1-1.1"/></svg>',
        default => '<svg ' . $attrs . '><circle cx="12" cy="12" r="9"/></svg>',
    };
}

$profilePath = getenv('PROFILE_JSON_PATH') ?: __DIR__ . '/data/profile.json';
$projectsPath = getenv('PROJECTS_JSON_PATH') ?: __DIR__ . '/data/projects.json';
$quotesPath = getenv('QUOTES_JSON_PATH') ?: __DIR__ . '/data/quotes.json';

$profile = xass_json($profilePath, []);
$profile = is_array($profile) ? $profile : [];

$projectPayload = xass_json($projectsPath, []);
if (is_array($projectPayload) && isset($projectPayload['projects']) && is_array($projectPayload['projects'])) {
    $projectPayload = $projectPayload['projects'];
}
$projects = [];
if (is_array($projectPayload)) {
    foreach ($projectPayload as $project) {
        if (is_array($project) && xass_text($project['title'] ?? '') !== '') {
            $projects[] = $project;
        }
    }
}

$name = xass_text($profile['name'] ?? '', 'XASS');
$title = xass_text($profile['title'] ?? '', 'Системы, которые остаются живыми.');
$bio = xass_text($profile['bio'] ?? '', 'Собираю интерфейсы, ботов и сервисы — от идеи до работающего продакшена.');
$username = ltrim(xass_text($profile['username'] ?? ''), '@');
$telegramUrl = xass_text($profile['telegram_url'] ?? '');
$avatarUrl = xass_text($profile['avatar_url'] ?? '');
$stack = [];
foreach ((array)($profile['stack'] ?? []) as $item) {
    $item = xass_text($item);
    if ($item !== '' && !in_array($item, $stack, true)) {
        $stack[] = $item;
    }
}

$links = [];
$seenLinks = [];
if ($telegramUrl !== '') {
    $links[] = ['label' => 'Telegram', 'url' => $telegramUrl, 'icon' => 'telegram', 'action' => 'Написать'];
    $seenLinks[strtolower(rtrim($telegramUrl, '/'))] = true;
}
foreach ((array)($profile['links'] ?? []) as $link) {
    if (!is_array($link)) {
        continue;
    }
    $label = xass_text($link['label'] ?? '');
    $url = xass_text($link['url'] ?? '');
    $key = strtolower(rtrim($url, '/'));
    if ($label === '' || $url === '' || isset($seenLinks[$key])) {
        continue;
    }
    $lower = strtolower($label);
    $icon = str_contains($lower, 'github') ? 'github' : (str_contains($lower, 'steam') ? 'steam' : 'link');
    $links[] = ['label' => $label, 'url' => $url, 'icon' => $icon, 'action' => 'Открыть'];
    $seenLinks[$key] = true;
}

$quotePayload = xass_json($quotesPath, []);
if (is_array($quotePayload) && isset($quotePayload['quotes']) && is_array($quotePayload['quotes'])) {
    $quotePayload = $quotePayload['quotes'];
}
$quotes = [];
foreach ((array)$quotePayload as $item) {
    $text = is_array($item) ? xass_text($item['text'] ?? '') : xass_text($item);
    if ($text !== '' && !in_array($text, $quotes, true)) {
        $quotes[] = $text;
    }
}
$profileQuote = xass_text($profile['quote'] ?? '', 'Делаем просто, надежно и без магии.');
if (!$quotes) {
    $quotes[] = $profileQuote;
}
if (count($quotes) === 1 && !is_readable($quotesPath)) {
    foreach (['Меньше слов — больше дела.', 'Хороший код объясняет себя сам.'] as $fallbackQuote) {
        if (!in_array($fallbackQuote, $quotes, true)) {
            $quotes[] = $fallbackQuote;
        }
    }
}
$quotesJson = json_encode($quotes, JSON_UNESCAPED_UNICODE | JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) ?: '[]';

$nowPlaying = xass_text($profile['now_listening_text'] ?? '');
$musicAvailable = !xass_is_music_noise($nowPlaying);
$musicDisplay = $musicAvailable ? $nowPlaying : 'Музыка на паузе';
$musicSource = xass_source_label(xass_text($profile['now_listening_source'] ?? '', 'pc_agent'));
$musicUpdatedAt = xass_text($profile['now_listening_updated_at'] ?? '');
$trackQuery = rawurlencode($musicAvailable ? $nowPlaying : '');
$musicLinks = [
    'Apple Music' => $musicAvailable ? "https://music.apple.com/search?term={$trackQuery}" : 'https://music.apple.com/',
    'Яндекс Музыка' => $musicAvailable ? "https://music.yandex.ru/search?text={$trackQuery}" : 'https://music.yandex.ru/',
    'VK Music' => $musicAvailable ? "https://vk.com/audio?section=search&q={$trackQuery}" : 'https://vk.com/audio',
    'Shazam' => $musicAvailable ? "https://www.shazam.com/search/{$trackQuery}" : 'https://www.shazam.com/',
    'Google' => $musicAvailable ? "https://www.google.com/search?q={$trackQuery}" : 'https://www.google.com/search?q=music',
];

$weatherText = str_replace(['В°C', 'Â°C'], '°C', xass_text($profile['weather_text'] ?? ''));
$weatherLocation = xass_text($profile['weather_location_name'] ?? '', 'Москва');
$weatherParts = array_values(array_filter(array_map('trim', explode(',', $weatherText))));
$weatherMain = $weatherParts[0] ?? 'Сводка обновляется';
$weatherMain = preg_replace('/^' . preg_quote($weatherLocation, '/') . '\s*:\s*/iu', '', $weatherMain) ?: $weatherMain;
$weatherDetails = array_slice($weatherParts, 1);
$weatherUpdatedAt = xass_text($profile['weather_updated_at'] ?? '');

$discordActive = !empty($profile['discord_active']);
$discordUpdatedAt = strtotime(xass_text($profile['discord_updated_at'] ?? ''));
$isOnline = $discordActive && $discordUpdatedAt !== false && time() - $discordUpdatedAt < 300;
$availability = $isOnline ? 'Сейчас онлайн и на связи.' : 'Открыт для диалога и новых задач.';
$identityLabel = $username !== '' ? '@' . $username : $name;
?>
<!doctype html>
<html lang="ru" class="no-js">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <meta name="theme-color" content="#000000">
    <meta name="color-scheme" content="dark">
    <meta name="description" content="<?= xass_escape($bio) ?>">
    <meta property="og:title" content="<?= xass_escape($name) ?> — <?= xass_escape($title) ?>">
    <meta property="og:description" content="<?= xass_escape($bio) ?>">
    <meta property="og:image" content="/assets/xass-hero-glass.png">
    <title><?= xass_escape($name) ?> — <?= xass_escape($title) ?></title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500&family=Manrope:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            color-scheme: dark;
            --bg: #000;
            --surface: #080a0e;
            --surface-strong: #0c1017;
            --line: #20242b;
            --line-bright: #313846;
            --text: #f4f6fa;
            --muted: #8d949f;
            --quiet: #5f6671;
            --blue: #376dff;
            --blue-soft: #8eacff;
            --green: #43d98b;
            --red: #ff6b75;
            --max: 1260px;
            --gutter: clamp(20px, 4vw, 56px);
            --ease: cubic-bezier(.2,.72,.2,1);
        }
        * { box-sizing: border-box; }
        html { scroll-behavior: smooth; background: var(--bg); }
        body { margin: 0; min-width: 320px; overflow-x: hidden; background: var(--bg); color: var(--text); font-family: Manrope, "Segoe UI", sans-serif; line-height: 1.5; -webkit-font-smoothing: antialiased; }
        body::before { content: ""; position: fixed; inset: 0; z-index: -2; pointer-events: none; background: radial-gradient(70% 34% at 73% 13%, rgba(37,77,205,.08), transparent 70%); }
        body.menu-open { overflow: hidden; }
        a, button { color: inherit; }
        button, input { font: inherit; }
        button { cursor: pointer; }
        img { max-width: 100%; }
        ::selection { background: var(--blue); color: #fff; }
        :focus-visible { outline: 2px solid var(--blue-soft); outline-offset: 4px; }
        .skip { position: fixed; left: 18px; top: 18px; z-index: 100; transform: translateY(-160%); background: #fff; color: #000; padding: 10px 14px; text-decoration: none; transition: transform .2s; }
        .skip:focus { transform: none; }
        .shell { width: min(calc(100% - (var(--gutter) * 2)), var(--max)); margin-inline: auto; }
        .progress { position: fixed; inset: 0 auto auto 0; z-index: 90; width: 0; height: 2px; background: var(--blue); box-shadow: 0 0 16px rgba(55,109,255,.6); }
        .site-header { position: sticky; top: 0; z-index: 50; background: rgba(0,0,0,.78); backdrop-filter: blur(18px); border-bottom: 1px solid rgba(32,36,43,.88); }
        .header-inner { min-height: 76px; display: flex; align-items: center; gap: 28px; }
        .brand { display: inline-flex; align-items: center; gap: 11px; text-decoration: none; font-size: 18px; font-weight: 600; letter-spacing: -.04em; }
        .brand img { width: 30px; height: 30px; object-fit: contain; }
        .nav { display: flex; align-items: center; gap: 28px; margin-left: auto; }
        .nav a { position: relative; color: var(--muted); text-decoration: none; font-size: 12px; transition: color .25s var(--ease); }
        .nav a::after { content: ""; position: absolute; left: 0; right: 100%; bottom: -11px; height: 1px; background: var(--blue); transition: right .3s var(--ease); }
        .nav a:hover, .nav a.active { color: #fff; }
        .nav a.active::after { right: 0; }
        .menu-button { display: none; width: 44px; height: 44px; margin-left: auto; padding: 0; border: 1px solid var(--line); background: #050607; align-items: center; justify-content: center; }
        .menu-button svg { width: 20px; height: 20px; }
        .hero { min-height: min(780px, calc(100svh - 76px)); display: grid; grid-template-columns: minmax(0,.88fr) minmax(520px,1.12fr); align-items: center; border-bottom: 1px solid var(--line); overflow: hidden; }
        .hero-copy { position: relative; z-index: 2; padding: 90px 0 76px; }
        .hero h1 { max-width: 720px; margin: 0; font-size: clamp(54px, 6.9vw, 104px); font-weight: 400; line-height: .96; letter-spacing: -.066em; text-wrap: balance; }
        .hero-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 40px; }
        .button { min-height: 50px; display: inline-flex; align-items: center; justify-content: center; gap: 14px; border: 1px solid var(--line-bright); padding: 0 18px; background: transparent; color: #d8dde6; text-decoration: none; font-size: 12px; transition: transform .25s var(--ease), border-color .25s, background .25s, color .25s; }
        .button svg { width: 17px; height: 17px; }
        .button:hover { transform: translateY(-2px); border-color: #64718a; color: #fff; }
        .button.primary { border-color: #315cd0; background: rgba(36,73,176,.1); color: #fff; }
        .button.primary:hover { border-color: var(--blue); background: rgba(55,109,255,.16); }
        .identity { display: flex; align-items: center; gap: 14px; margin-top: 50px; color: var(--muted); }
        .identity-avatar { width: 52px; height: 52px; border-radius: 50%; object-fit: cover; border: 1px solid #303746; background: var(--surface); }
        .identity-placeholder { display: grid; place-items: center; font-size: 13px; color: var(--blue-soft); }
        .identity strong { display: block; color: #dfe3ea; font-size: 13px; font-weight: 500; }
        .identity span { display: block; margin-top: 4px; font: 10px/1.4 "JetBrains Mono", monospace; letter-spacing: .06em; text-transform: uppercase; }
        .availability-dot { width: 7px; height: 7px; margin-left: 5px; border-radius: 50%; background: <?= $isOnline ? 'var(--green)' : 'var(--blue)' ?>; box-shadow: 0 0 0 5px <?= $isOnline ? 'rgba(67,217,139,.08)' : 'rgba(55,109,255,.09)' ?>; }
        .hero-art { position: relative; min-height: 640px; align-self: stretch; display: grid; place-items: center; }
        .hero-art::before { content: ""; position: absolute; inset: 18% 3% 17%; background: radial-gradient(circle, rgba(42,82,229,.15), transparent 62%); filter: blur(24px); }
        .hero-art img { position: relative; width: min(880px, 118%); max-width: none; transform: translateX(4%); filter: saturate(1.05) contrast(1.04); will-change: transform; }
        .section { padding: clamp(82px, 10vw, 132px) 0; border-bottom: 1px solid var(--line); scroll-margin-top: 75px; }
        .section-head { display: grid; grid-template-columns: minmax(230px,.65fr) minmax(0,1.35fr); gap: clamp(36px,7vw,110px); align-items: end; margin-bottom: clamp(48px,7vw,82px); }
        .section h2 { margin: 0; font-size: clamp(44px, 5.5vw, 78px); line-height: .98; font-weight: 400; letter-spacing: -.06em; text-wrap: balance; }
        .section-copy { max-width: 660px; color: #9aa1ac; font-size: clamp(15px,1.3vw,18px); line-height: 1.85; }
        .about .section-head { align-items: start; }
        .about-title em { color: var(--blue-soft); font-style: normal; }
        .tech-rail { display: flex; align-items: center; gap: 0; margin-top: 50px; border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); overflow-x: auto; scrollbar-width: none; }
        .tech-rail::-webkit-scrollbar { display: none; }
        .tech-label, .tech-item { flex: 0 0 auto; padding: 18px 22px; font: 10px/1 "JetBrains Mono", monospace; letter-spacing: .06em; text-transform: uppercase; }
        .tech-label { padding-left: 0; color: var(--blue-soft); }
        .tech-item { position: relative; color: #aeb4be; }
        .tech-item::before { content: ""; position: absolute; left: 0; top: 50%; width: 3px; height: 3px; border-radius: 50%; background: var(--blue); }
        .quotes { overflow: hidden; }
        .quote-meta { display: flex; align-items: center; gap: 10px; margin-bottom: 52px; color: var(--quiet); font: 10px/1 "JetBrains Mono", monospace; letter-spacing: .1em; text-transform: uppercase; }
        .quote-meta strong { color: var(--blue-soft); font-weight: 500; }
        .quote-stage { position: relative; min-height: 240px; display: grid; grid-template-columns: 90px minmax(0,1fr); gap: clamp(18px,4vw,56px); align-items: start; }
        .quote-mark { color: var(--blue); font-size: 96px; font-weight: 400; line-height: .85; letter-spacing: -.12em; opacity: .82; }
        .quote-text { max-width: 980px; margin: 0; font-size: clamp(38px,5.7vw,78px); line-height: 1.14; font-weight: 400; letter-spacing: -.055em; text-wrap: balance; transition: opacity .18s, transform .36s var(--ease); }
        .quote-stage.changing .quote-text { opacity: 0; transform: translateY(12px); }
        .quote-controls { display: flex; align-items: center; gap: 10px; margin-top: 52px; padding-top: 22px; border-top: 1px solid var(--line); }
        .icon-button { width: 46px; height: 46px; display: grid; place-items: center; border: 1px solid transparent; background: transparent; color: var(--muted); transition: color .2s, border-color .2s, transform .2s; }
        .icon-button:hover { color: #fff; border-color: var(--line-bright); transform: translateY(-1px); }
        .icon-button svg { width: 18px; height: 18px; }
        .quote-count { margin-left: auto; color: var(--quiet); font: 11px/1 "JetBrains Mono", monospace; }
        .quote-count strong { color: var(--blue-soft); font-size: 18px; font-weight: 400; }
        .music-layout { display: grid; grid-template-columns: minmax(0,1.05fr) minmax(360px,.95fr); border-top: 1px solid var(--line); }
        .now-playing { min-height: 380px; display: grid; grid-template-columns: minmax(150px,220px) minmax(0,1fr); gap: clamp(24px,4vw,50px); align-items: center; padding: 38px 42px 38px 0; border-right: 1px solid var(--line); }
        .album-art { aspect-ratio: 1; width: 100%; object-fit: cover; border: 1px solid var(--line-bright); background: var(--surface); opacity: .92; transition: opacity .4s, transform .5s var(--ease); }
        .album-art.loaded { opacity: 1; transform: scale(1.015); }
        .eyebrow { color: #737985; font: 10px/1.4 "JetBrains Mono", monospace; letter-spacing: .1em; text-transform: uppercase; }
        .track-name { margin-top: 18px; font-size: clamp(26px,3.1vw,44px); line-height: 1.18; letter-spacing: -.045em; text-wrap: balance; }
        .track-source { margin-top: 18px; color: var(--muted); font-size: 12px; }
        .track-source strong { color: var(--blue-soft); font-weight: 500; }
        .service-list { padding-left: 42px; }
        .row-link { min-height: 68px; display: flex; align-items: center; gap: 18px; border-bottom: 1px solid var(--line); color: #e7e9ed; text-decoration: none; font-size: 17px; transition: padding .28s var(--ease), color .2s, border-color .2s; }
        .row-link:first-child { border-top: 1px solid var(--line); }
        .row-link:hover { padding-left: 10px; color: #fff; border-color: #343b47; }
        .row-action { margin-left: auto; display: flex; align-items: center; gap: 10px; color: #69717e; font: 9px/1 "JetBrains Mono", monospace; text-transform: uppercase; letter-spacing: .07em; }
        .row-action svg { width: 14px; height: 14px; }
        .weather-strip { display: grid; grid-template-columns: minmax(260px,1.1fr) repeat(3,minmax(130px,.63fr)); border-top: 1px solid var(--line); border-bottom: 1px solid var(--line); }
        .weather-cell { min-height: 170px; display: flex; flex-direction: column; justify-content: center; padding: 28px 34px; border-left: 1px solid var(--line); }
        .weather-cell:first-child { padding-left: 0; border-left: 0; }
        .weather-main { display: flex; align-items: end; gap: 16px; margin-top: 12px; }
        .weather-main strong { font-size: clamp(42px,5.4vw,72px); line-height: .95; font-weight: 400; letter-spacing: -.065em; }
        .weather-cell b { margin-top: 12px; color: #e0e4eb; font-size: 17px; font-weight: 500; }
        .weather-cell p { margin: 8px 0 0; color: var(--muted); font-size: 12px; line-height: 1.6; }
        .project-list { border-top: 1px solid var(--line); }
        .project-row { min-height: 116px; display: grid; grid-template-columns: 90px minmax(180px,.7fr) minmax(240px,1.2fr) 120px 38px; gap: 24px; align-items: center; border-bottom: 1px solid var(--line); color: inherit; text-decoration: none; transition: padding .35s var(--ease), background .3s; }
        .project-row:hover { padding-inline: 14px; background: linear-gradient(90deg, rgba(55,109,255,.055), transparent 64%); }
        .project-year, .project-description, .project-status { color: #737985; font-size: 12px; }
        .project-title { font-size: clamp(19px,2vw,27px); letter-spacing: -.035em; }
        .project-status { color: var(--blue-soft); font: 9px/1.4 "JetBrains Mono", monospace; text-transform: uppercase; letter-spacing: .06em; }
        .project-row svg { width: 18px; height: 18px; color: #69717e; transition: transform .3s var(--ease), color .2s; }
        .project-row:hover svg { transform: translate(3px,-3px); color: var(--blue-soft); }
        .contacts-layout { display: grid; grid-template-columns: minmax(230px,.62fr) minmax(0,1.38fr); gap: clamp(40px,8vw,120px); }
        .contact-intro p { max-width: 260px; margin: 30px 0 0; color: var(--muted); font-size: 16px; line-height: 1.7; }
        .contact-list { border-top: 1px solid var(--line); }
        .contact-row { min-height: 86px; display: grid; grid-template-columns: 40px minmax(0,1fr) auto; gap: 18px; align-items: center; border-bottom: 1px solid var(--line); color: #eaedf2; text-decoration: none; font-size: 20px; transition: padding .3s var(--ease), color .2s; }
        .contact-row:hover { padding-left: 10px; color: #fff; }
        .contact-icon { width: 24px; height: 24px; color: var(--blue-soft); }
        .contact-row .row-action { font-size: 9px; }
        .footer { min-height: 130px; display: grid; grid-template-columns: auto 1fr auto; gap: 30px; align-items: center; color: #5f6671; font-size: 11px; }
        .footer-brand { display: flex; align-items: center; gap: 10px; color: #dbe0e8; font-size: 17px; font-weight: 500; }
        .footer-brand img { width: 28px; height: 28px; }
        .footer-line { height: 1px; background: var(--line); }
        .back-top { display: flex; align-items: center; gap: 10px; color: var(--muted); text-decoration: none; font: 9px/1 "JetBrains Mono", monospace; text-transform: uppercase; letter-spacing: .08em; }
        .back-top svg { width: 14px; height: 14px; transform: rotate(-45deg); }
        .js .reveal { opacity: 0; transform: translateY(24px); transition: opacity .72s var(--ease), transform .72s var(--ease); }
        .js .reveal.is-visible { opacity: 1; transform: none; }
        .js .reveal[data-delay="1"] { transition-delay: .08s; }
        .js .reveal[data-delay="2"] { transition-delay: .16s; }
        @media (max-width: 980px) {
            .nav { gap: 18px; }
            .hero { grid-template-columns: 1fr; min-height: auto; }
            .hero-copy { padding: 84px 0 10px; }
            .hero-art { min-height: 480px; }
            .hero-art img { width: min(820px,122%); transform: none; }
            .section-head, .contacts-layout { grid-template-columns: 1fr; gap: 30px; }
            .music-layout { grid-template-columns: 1fr; }
            .now-playing { border-right: 0; border-bottom: 1px solid var(--line); padding-right: 0; }
            .service-list { padding-left: 0; }
            .weather-strip { grid-template-columns: 1fr 1fr; }
            .weather-cell:nth-child(3) { border-left: 0; }
            .weather-cell { border-bottom: 1px solid var(--line); }
            .project-row { grid-template-columns: 70px minmax(170px,.8fr) minmax(220px,1.2fr) 28px; }
            .project-status { display: none; }
        }
        @media (max-width: 720px) {
            .header-inner { min-height: 68px; }
            .menu-button { display: inline-flex; }
            .nav { position: fixed; inset: 68px 0 auto; display: grid; gap: 0; padding: 12px var(--gutter) 28px; background: rgba(0,0,0,.97); border-bottom: 1px solid var(--line); transform: translateY(-120%); visibility: hidden; transition: transform .35s var(--ease), visibility .35s; }
            .nav.open { transform: none; visibility: visible; }
            .nav a { min-height: 48px; display: flex; align-items: center; border-bottom: 1px solid var(--line); font-size: 14px; }
            .nav a::after { display: none; }
            .hero h1 { font-size: clamp(48px,14.5vw,76px); }
            .hero-actions { align-items: stretch; }
            .button { flex: 1 1 100%; }
            .hero-art { min-height: 340px; margin-bottom: -4%; }
            .hero-art img { width: 136%; }
            .identity { margin-top: 38px; }
            .section { padding: 76px 0; }
            .quote-stage { min-height: 220px; grid-template-columns: 1fr; }
            .quote-mark { height: 52px; font-size: 72px; }
            .quote-text { font-size: clamp(34px,10.6vw,54px); }
            .now-playing { min-height: 0; grid-template-columns: 104px minmax(0,1fr); padding-block: 28px; }
            .track-name { margin-top: 10px; font-size: 23px; }
            .weather-strip { grid-template-columns: 1fr; }
            .weather-cell, .weather-cell:nth-child(3) { min-height: 122px; padding: 24px 0; border-left: 0; }
            .project-row { min-height: 126px; grid-template-columns: 50px minmax(0,1fr) 24px; gap: 16px; }
            .project-description { grid-column: 2; margin-top: -20px; }
            .project-row svg { grid-column: 3; grid-row: 1 / span 2; }
            .contact-row { min-height: 78px; font-size: 18px; }
            .footer { grid-template-columns: 1fr auto; gap: 18px; padding-block: 34px; }
            .footer-line { display: none; }
        }
        @media (max-width: 430px) {
            .hero-copy { padding-top: 66px; }
            .identity span { max-width: 220px; }
            .section-head { margin-bottom: 42px; }
            .tech-item { padding-inline: 18px; }
            .now-playing { grid-template-columns: 84px minmax(0,1fr); gap: 18px; }
            .track-source { margin-top: 10px; }
            .row-link { min-height: 62px; font-size: 15px; }
            .row-action { font-size: 8px; }
            .project-row { grid-template-columns: 42px minmax(0,1fr) 22px; }
            .project-description { font-size: 11px; }
            .contact-row { grid-template-columns: 32px minmax(0,1fr) auto; gap: 12px; }
            .contact-row .row-action span { display: none; }
        }
        @media (prefers-reduced-motion: reduce) {
            html { scroll-behavior: auto; }
            *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; }
            .js .reveal { opacity: 1; transform: none; }
        }
    </style>
    <script>document.documentElement.className='js';</script>
</head>
<body>
<a class="skip" href="#content">К содержанию</a>
<div class="progress" id="progress" aria-hidden="true"></div>
<header class="site-header">
    <div class="shell header-inner">
        <a class="brand" href="#top" aria-label="XASS — наверх"><img src="/assets/xass-app-icon-192.png" alt=""><span>XASS</span></a>
        <nav class="nav" id="siteNav" aria-label="Основная навигация">
            <a href="#about">Обо мне</a><a href="#quotes">Цитаты</a><a href="#music">Музыка</a><a href="#weather">Погода</a><a href="#projects">Проекты</a><a href="#contacts">Контакты</a>
        </nav>
        <button class="menu-button" id="menuButton" type="button" aria-label="Открыть меню" aria-expanded="false" aria-controls="siteNav">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M4 8h16M4 16h16"/></svg>
        </button>
    </div>
</header>

<main id="content">
    <section class="hero shell" id="top" aria-labelledby="hero-title">
        <div class="hero-copy reveal">
            <h1 id="hero-title"><?= xass_escape($title) ?></h1>
            <div class="hero-actions">
                <?php if ($telegramUrl !== ''): ?><a class="button primary" href="<?= xass_escape($telegramUrl) ?>" target="_blank" rel="noopener">Написать в Telegram <?= xass_icon('telegram') ?></a><?php endif; ?>
                <a class="button" href="#projects">Смотреть проекты <?= xass_icon('arrow-right') ?></a>
            </div>
            <div class="identity">
                <?php if ($avatarUrl !== ''): ?><img class="identity-avatar" src="<?= xass_escape($avatarUrl) ?>" alt="Аватар <?= xass_escape($name) ?>">
                <?php else: ?><div class="identity-avatar identity-placeholder" aria-hidden="true">X</div><?php endif; ?>
                <div><strong><?= xass_escape($name) ?></strong><span><?= xass_escape($identityLabel) ?> · <?= xass_escape($availability) ?></span></div>
                <span class="availability-dot" aria-hidden="true"></span>
            </div>
        </div>
        <div class="hero-art reveal" data-delay="1"><img id="heroArt" src="/assets/xass-hero-glass.png" alt="Стеклянный знак XASS" fetchpriority="high"></div>
    </section>

    <section class="section about" id="about">
        <div class="shell">
            <div class="section-head reveal"><h2 class="about-title">Делаю сложное <em>спокойным.</em></h2><p class="section-copy"><?= nl2br(xass_escape($bio)) ?></p></div>
            <?php if ($stack): ?><div class="tech-rail reveal" data-delay="1" aria-label="Технологии"><span class="tech-label">Технологии</span><?php foreach ($stack as $item): ?><span class="tech-item"><?= xass_escape($item) ?></span><?php endforeach; ?></div><?php endif; ?>
        </div>
    </section>

    <section class="section quotes" id="quotes" aria-labelledby="quotes-heading">
        <div class="shell">
            <div class="quote-meta reveal"><strong>02</strong><span>/</span><span id="quotes-heading">Цитаты</span></div>
            <div class="quote-stage reveal" id="quoteStage"><div class="quote-mark" aria-hidden="true">“</div><blockquote class="quote-text" id="quoteText"><?= xass_escape($quotes[0] ?? $profileQuote) ?></blockquote></div>
            <div class="quote-controls reveal" data-delay="1">
                <button class="icon-button" id="quotePrev" type="button" aria-label="Предыдущая цитата"><?= xass_icon('arrow-left') ?></button>
                <button class="icon-button" id="quoteShuffle" type="button" aria-label="Случайная цитата"><?= xass_icon('shuffle') ?></button>
                <button class="icon-button" id="quoteNext" type="button" aria-label="Следующая цитата"><?= xass_icon('arrow-right') ?></button>
                <div class="quote-count" aria-live="polite"><strong id="quoteIndex">01</strong> / <span id="quoteTotal"><?= str_pad((string)count($quotes), 2, '0', STR_PAD_LEFT) ?></span></div>
            </div>
        </div>
    </section>

    <section class="section" id="music" aria-labelledby="music-heading">
        <div class="shell">
            <div class="section-head reveal"><h2 id="music-heading">Музыка</h2><p class="section-copy">Трек, который действительно передал выбранный источник. Веб‑страницы и заголовки приложений сюда не попадают.</p></div>
            <div class="music-layout reveal" data-delay="1">
                <div class="now-playing">
                    <img class="album-art" id="albumArt" src="/assets/xass-app-icon-512.png" alt="" data-track="<?= xass_escape($musicAvailable ? $nowPlaying : '') ?>">
                    <div><div class="eyebrow">Сейчас играет</div><div class="track-name"><?= xass_escape($musicDisplay) ?></div><div class="track-source"><strong><?= xass_escape($musicSource) ?></strong><?php if ($musicUpdatedAt !== ''): ?> · <time id="musicTime" datetime="<?= xass_escape($musicUpdatedAt) ?>">обновлено недавно</time><?php endif; ?></div></div>
                </div>
                <div class="service-list"><?php foreach ($musicLinks as $label => $url): ?><a class="row-link" href="<?= xass_escape($url) ?>" target="_blank" rel="noopener"><span><?= xass_escape($label) ?></span><span class="row-action"><span>Открыть</span><?= xass_icon('arrow-up-right') ?></span></a><?php endforeach; ?></div>
            </div>
        </div>
    </section>

    <section class="section" id="weather" aria-labelledby="weather-heading">
        <div class="shell">
            <div class="section-head reveal"><h2 id="weather-heading">Погода</h2><p class="section-copy">Короткая живая сводка — отдельно от контактов, ссылок и статуса связи.</p></div>
            <div class="weather-strip reveal" data-delay="1">
                <div class="weather-cell"><div class="eyebrow"><?= xass_escape($weatherLocation) ?></div><div class="weather-main"><strong><?= xass_escape($weatherMain) ?></strong></div></div>
                <div class="weather-cell"><div class="eyebrow">Условия</div><b><?= xass_escape($weatherDetails[0] ?? 'Нет данных') ?></b><p>Текущая сводка</p></div>
                <div class="weather-cell"><div class="eyebrow">Детали</div><b><?= xass_escape($weatherDetails[1] ?? 'Обновляются') ?></b><p><?= xass_escape($weatherDetails[2] ?? 'Автоматически') ?></p></div>
                <div class="weather-cell"><div class="eyebrow">Обновлено</div><b><?php if ($weatherUpdatedAt !== ''): ?><time id="weatherTime" datetime="<?= xass_escape($weatherUpdatedAt) ?>">недавно</time><?php else: ?>—<?php endif; ?></b><p><?= xass_escape($weatherDetails[3] ?? 'Фоновая синхронизация') ?></p></div>
            </div>
        </div>
    </section>

    <section class="section" id="projects" aria-labelledby="projects-heading">
        <div class="shell">
            <div class="section-head reveal"><h2 id="projects-heading">Проекты</h2><p class="section-copy">Выбранные рабочие системы и эксперименты — с коротким контекстом вместо лишней презентации.</p></div>
            <div class="project-list reveal" data-delay="1">
                <?php if ($projects): foreach (array_slice($projects, 0, 6) as $project):
                    $years = is_array($project['years'] ?? null) ? $project['years'] : [];
                    $from = xass_text($years['from'] ?? '');
                    $to = xass_text($years['to'] ?? '');
                    $year = $from !== '' && $to !== '' && $to !== $from ? $from . '—' . $to : ($from ?: $to);
                    $url = xass_text($project['url'] ?? '', '/projects.php');
                ?><a class="project-row" href="<?= xass_escape($url) ?>"<?= str_starts_with($url, 'http') ? ' target="_blank" rel="noopener"' : '' ?>><span class="project-year"><?= xass_escape($year ?: '—') ?></span><span class="project-title"><?= xass_escape($project['title']) ?></span><span class="project-description"><?= xass_escape($project['description'] ?? $project['subtitle'] ?? '') ?></span><span class="project-status"><?= xass_escape($project['status'] ?? 'В работе') ?></span><?= xass_icon('arrow-up-right') ?></a><?php endforeach; else: ?>
                    <a class="project-row" href="/projects.php"><span class="project-year">—</span><span class="project-title">Архив проектов</span><span class="project-description">Проекты появятся здесь после публикации в панели XASS.</span><span class="project-status">Открыть</span><?= xass_icon('arrow-up-right') ?></a>
                <?php endif; ?>
            </div>
        </div>
    </section>

    <section class="section" id="contacts" aria-labelledby="contacts-heading">
        <div class="shell contacts-layout">
            <div class="contact-intro reveal"><h2 id="contacts-heading">Контакты</h2><p><?= xass_escape($availability) ?></p></div>
            <div class="contact-list reveal" data-delay="1"><?php if ($links): foreach ($links as $link): ?><a class="contact-row" href="<?= xass_escape($link['url']) ?>" target="_blank" rel="noopener"><span><?= xass_icon($link['icon'], 'contact-icon') ?></span><span><?= xass_escape($link['label']) ?></span><span class="row-action"><span><?= xass_escape($link['action']) ?></span><?= xass_icon('arrow-up-right') ?></span></a><?php endforeach; else: ?><a class="contact-row" href="#top"><span><?= xass_icon('link', 'contact-icon') ?></span><span>Контакты скоро появятся</span><span class="row-action"><?= xass_icon('arrow-up-right') ?></span></a><?php endif; ?></div>
        </div>
    </section>
</main>

<footer class="shell footer"><div class="footer-brand"><img src="/assets/xass-app-icon-192.png" alt=""><span>XASS</span></div><div class="footer-line" aria-hidden="true"></div><a class="back-top" href="#top">Наверх <?= xass_icon('arrow-up-right') ?></a></footer>

<script>
(() => {
    const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
    const nav = document.getElementById('siteNav');
    const menu = document.getElementById('menuButton');
    const closeMenu = () => { nav.classList.remove('open'); menu.setAttribute('aria-expanded','false'); document.body.classList.remove('menu-open'); };
    menu.addEventListener('click', () => { const open = !nav.classList.contains('open'); nav.classList.toggle('open', open); menu.setAttribute('aria-expanded', String(open)); document.body.classList.toggle('menu-open', open); });
    nav.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
    addEventListener('keydown', event => { if (event.key === 'Escape') closeMenu(); });

    const reveals = document.querySelectorAll('.reveal');
    if (reduced || !('IntersectionObserver' in window)) reveals.forEach(el => el.classList.add('is-visible'));
    else {
        const observer = new IntersectionObserver(entries => entries.forEach(entry => { if (entry.isIntersecting) { entry.target.classList.add('is-visible'); observer.unobserve(entry.target); } }), { threshold: .12, rootMargin: '0px 0px -7% 0px' });
        reveals.forEach(el => observer.observe(el));
    }

    const links = [...nav.querySelectorAll('a')];
    const sections = links.map(link => document.querySelector(link.getAttribute('href'))).filter(Boolean);
    if ('IntersectionObserver' in window) {
        const sectionObserver = new IntersectionObserver(entries => entries.forEach(entry => { if (!entry.isIntersecting) return; links.forEach(link => link.classList.toggle('active', link.getAttribute('href') === '#' + entry.target.id)); }), { rootMargin: '-25% 0px -65% 0px' });
        sections.forEach(section => sectionObserver.observe(section));
    }

    const progress = document.getElementById('progress');
    let ticking = false;
    const updateScroll = () => { const max = document.documentElement.scrollHeight - innerHeight; progress.style.width = (max > 0 ? scrollY / max * 100 : 0) + '%'; ticking = false; };
    addEventListener('scroll', () => { if (!ticking) { requestAnimationFrame(updateScroll); ticking = true; } }, { passive: true });
    updateScroll();

    const quotes = <?= $quotesJson ?>;
    const quoteStage = document.getElementById('quoteStage');
    const quoteText = document.getElementById('quoteText');
    const quoteIndex = document.getElementById('quoteIndex');
    let currentQuote = 0;
    const showQuote = next => {
        if (!quotes.length) return;
        currentQuote = (next + quotes.length) % quotes.length;
        quoteStage.classList.add('changing');
        setTimeout(() => { quoteText.textContent = quotes[currentQuote]; quoteIndex.textContent = String(currentQuote + 1).padStart(2,'0'); quoteStage.classList.remove('changing'); }, reduced ? 0 : 180);
    };
    document.getElementById('quotePrev').addEventListener('click', () => showQuote(currentQuote - 1));
    document.getElementById('quoteNext').addEventListener('click', () => showQuote(currentQuote + 1));
    document.getElementById('quoteShuffle').addEventListener('click', () => { if (quotes.length < 2) return; let next = currentQuote; while (next === currentQuote) next = Math.floor(Math.random() * quotes.length); showQuote(next); });

    const relativeTime = (id) => { const el = document.getElementById(id); if (!el) return; const value = Date.parse(el.dateTime); if (!Number.isFinite(value)) return; const minutes = Math.max(0, Math.round((Date.now() - value) / 60000)); el.textContent = minutes < 1 ? 'только что' : minutes < 60 ? minutes + ' мин назад' : minutes < 1440 ? Math.round(minutes / 60) + ' ч назад' : new Date(value).toLocaleDateString('ru-RU'); };
    relativeTime('musicTime'); relativeTime('weatherTime');

    const art = document.getElementById('albumArt');
    const track = art?.dataset.track?.trim();
    if (art && track) fetch('https://itunes.apple.com/search?term=' + encodeURIComponent(track) + '&media=music&entity=song&limit=1').then(response => response.ok ? response.json() : null).then(data => { const url = data?.results?.[0]?.artworkUrl100; if (!url) return; art.onload = () => art.classList.add('loaded'); art.src = url.replace('100x100bb','600x600bb'); }).catch(() => {});

    if (!reduced && matchMedia('(pointer:fine)').matches) {
        const hero = document.getElementById('heroArt');
        addEventListener('pointermove', event => { const x = (event.clientX / innerWidth - .5) * 8; const y = (event.clientY / innerHeight - .5) * 6; hero.style.transform = `translate3d(${x}px,${y}px,0)`; }, { passive: true });
    }
})();
</script>
</body>
</html>
