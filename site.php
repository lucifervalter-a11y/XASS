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
    if (!is_string($raw) || $raw === '') {
        return $fallback;
    }
    $value = json_decode($raw, true);
    return $value ?? $fallback;
}

$profilePath = getenv('PROFILE_JSON_PATH') ?: __DIR__ . '/data/profile.json';
$projectsPath = getenv('PROJECTS_JSON_PATH') ?: __DIR__ . '/data/projects.json';
$profile = xass_json($profilePath, []);
$projects = xass_json($projectsPath, []);
if (!is_array($profile)) {
    $profile = [];
}
if (!is_array($projects)) {
    $projects = [];
}

$name = trim((string)($profile['name'] ?? 'XASS')) ?: 'XASS';
$storedTitle = trim((string)($profile['title'] ?? ''));
$headline = $storedTitle !== '' && $storedTitle !== 'Full-stack разработчик'
    ? $storedTitle
    : 'Системы, которые остаются живыми.';
$storedBio = trim((string)($profile['bio'] ?? ''));
$bio = $storedBio !== '' && $storedBio !== 'Коротко о себе'
    ? $storedBio
    : 'Собираю ботов, сервисы и интерфейсы — от идеи до работающего продакшена.';
$telegramUrl = trim((string)($profile['telegram_url'] ?? ''));
$links = is_array($profile['links'] ?? null) ? $profile['links'] : [];
$stack = is_array($profile['stack'] ?? null) ? $profile['stack'] : [];
$nowPlaying = trim((string)($profile['now_listening_text'] ?? ''));
$weather = trim((string)($profile['weather_text'] ?? ''));
$available = !empty($profile['discord_active']);
$quote = trim((string)($profile['quote'] ?? ''));
?>
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
    <meta name="theme-color" content="#000000">
    <meta name="description" content="<?= xass_escape($bio) ?>">
    <title><?= xass_escape($name) ?> — systems that stay alive</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;600;700&display=swap');
        :root{--bg:#000;--surface:#080b0f;--line:#252c35;--line-soft:#161b22;--text:#f5f7fb;--muted:#929aa8;--blue:#2878ff;--violet:#7457ff;--green:#45d982;--container:1440px}
        *{box-sizing:border-box}html{scroll-behavior:smooth;background:var(--bg)}body{margin:0;background:var(--bg);color:var(--text);font-family:Manrope,"Segoe UI",sans-serif;line-height:1.5;overflow-x:hidden}a{color:inherit}button,a{-webkit-tap-highlight-color:transparent}.shell{width:min(calc(100% - 64px),var(--container));margin:0 auto}.site-head{height:92px;display:flex;align-items:center;border-bottom:1px solid var(--line);position:relative;z-index:5}.logo{text-decoration:none;font-size:36px;font-weight:500;letter-spacing:-2.2px}.site-nav{margin-left:auto;display:flex;align-items:center}.site-nav a{text-decoration:none;font-size:14px;color:#dfe3e9;padding:8px 28px;border-left:1px solid var(--line)}
        .hero{min-height:760px;display:grid;grid-template-columns:minmax(430px,.9fr) minmax(520px,1.1fr);align-items:center;border-bottom:1px solid var(--line);position:relative}.hero-copy{position:relative;z-index:2;padding:88px 0 68px}.hero h1{font-size:clamp(62px,7vw,116px);font-weight:400;letter-spacing:-.065em;line-height:.91;max-width:790px;margin:0}.hero-sub{font-size:clamp(17px,1.45vw,23px);color:#aeb4be;max-width:610px;margin:34px 0 0;line-height:1.55}.hero-actions{display:flex;gap:14px;margin-top:40px}.action{min-height:58px;border:1px solid #505862;text-decoration:none;display:inline-flex;align-items:center;justify-content:space-between;gap:34px;padding:0 22px;font-size:14px;transition:.2s;background:#020304}.action:hover{border-color:var(--blue);color:#fff;transform:translateY(-2px)}.action.primary{border-color:var(--blue);min-width:230px}.action svg{width:20px;height:20px}.status-rail{display:flex;align-items:center;gap:18px;border-top:1px solid var(--line);margin-top:42px;padding-top:24px;color:var(--muted);font-size:12px;max-width:680px}.signal{width:36px;height:18px;color:var(--blue)}.status-rail strong{color:var(--text);font-weight:500}.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 0 5px rgba(69,217,130,.08)}.hero-art{height:100%;min-height:660px;display:flex;align-items:center;justify-content:center;position:relative}.hero-art img{display:block;width:min(940px,115%);max-width:none;transform:translateX(4%);filter:saturate(.92) contrast(1.05);animation:float 8s ease-in-out infinite}.hero-art:after{content:"";position:absolute;left:4%;right:4%;bottom:9%;height:1px;background:linear-gradient(90deg,transparent,var(--blue),transparent);opacity:.4}@keyframes float{50%{transform:translate(4%,-9px) scale(1.01)}}
        .section{border-bottom:1px solid var(--line);padding:92px 0}.section-grid{display:grid;grid-template-columns:.9fr 1.1fr;gap:72px}.section h2{font-size:clamp(45px,5vw,76px);line-height:1;margin:0;font-weight:400;letter-spacing:-.055em}.manifesto h3{font-size:clamp(28px,3vw,46px);font-weight:400;line-height:1.15;margin:0 0 26px;letter-spacing:-.035em}.manifesto h3 span{color:var(--blue)}.manifesto p{margin:0;color:#adb5c1;max-width:650px;font-size:16px;line-height:1.7}.stack{display:flex;flex-wrap:wrap;gap:0;margin-top:36px;border-top:1px solid var(--line)}.stack span{padding:14px 22px 14px 0;margin-right:22px;color:#c8ced7;font-size:12px;border-bottom:1px solid var(--line-soft)}.quote{font-size:18px;color:#dce2ea;margin-top:34px;padding-left:20px;border-left:2px solid var(--blue)}
        .projects-head{display:flex;align-items:end;justify-content:space-between;margin-bottom:52px}.projects-count{font-family:"Cascadia Mono",Consolas,monospace;color:var(--muted);font-size:12px}.project-list{border-top:1px solid var(--line)}.project{display:grid;grid-template-columns:minmax(260px,.8fr) minmax(340px,1.2fr);gap:34px;padding:34px 0;border-bottom:1px solid var(--line);text-decoration:none;transition:.25s}.project:hover{padding-left:12px}.project-media{height:230px;border:1px solid #303945;background:radial-gradient(circle at 70% 35%,rgba(40,120,255,.22),transparent 40%),linear-gradient(145deg,#0d1219,#030405);display:flex;align-items:flex-end;padding:22px;overflow:hidden;position:relative}.project-media img,.project-media video{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}.project-mark{font-size:12px;letter-spacing:.28em;color:#9dbdff;position:relative;z-index:1}.project-body{display:flex;flex-direction:column;justify-content:center}.project-top{display:flex;justify-content:space-between;gap:24px;align-items:start}.project h3{font-size:clamp(28px,3vw,44px);font-weight:400;letter-spacing:-.045em;margin:0}.project-status{font-size:11px;color:var(--blue);text-transform:uppercase;letter-spacing:.08em;padding-top:10px}.project p{color:#a9b1bd;margin:16px 0 28px;max-width:670px}.project-meta{display:flex;flex-wrap:wrap;gap:20px;padding-top:18px;border-top:1px solid var(--line-soft);color:#8f98a7;font-size:11px}
        .contact-grid{display:grid;grid-template-columns:.7fr 1.3fr;gap:70px;align-items:start}.contact-links{border-top:1px solid var(--line)}.contact-link{display:flex;align-items:center;justify-content:space-between;min-height:90px;border-bottom:1px solid var(--line);text-decoration:none;font-size:22px;transition:.2s}.contact-link:hover{color:var(--blue);padding-left:10px}.contact-link small{font-size:12px;color:var(--muted)}.site-foot{min-height:120px;display:flex;align-items:center;gap:30px;color:var(--muted);font-size:12px}.foot-logo{color:var(--blue);font-size:18px;letter-spacing:.35em}.foot-line{height:1px;background:var(--line);flex:1}
        .js .reveal{opacity:0;transform:translateY(18px);transition:opacity .7s ease,transform .7s ease}.js .reveal.visible{opacity:1;transform:none}@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important;animation:none!important;transition:none!important}.js .reveal{opacity:1;transform:none}}
        @media(max-width:980px){.shell{width:min(calc(100% - 36px),var(--container))}.hero{grid-template-columns:1fr;min-height:auto}.hero-copy{padding:72px 0 20px}.hero-art{min-height:430px}.hero-art img{width:min(760px,118%);transform:none}.section-grid,.contact-grid{grid-template-columns:1fr;gap:42px}.project{grid-template-columns:1fr}.project-media{height:310px}}
        @media(max-width:620px){.shell{width:calc(100% - 32px)}.site-head{height:72px}.logo{font-size:28px}.site-nav a{font-size:12px;padding:7px 10px}.site-nav a:first-child{display:none}.hero-copy{padding-top:56px}.hero h1{font-size:clamp(48px,15vw,72px)}.hero-sub{font-size:16px;margin-top:24px}.hero-actions{flex-direction:column}.action,.action.primary{width:100%;min-width:0}.status-rail{flex-wrap:wrap;gap:12px;margin-top:30px}.hero-art{min-height:330px}.hero-art img{width:128%}.section{padding:68px 0}.projects-head{align-items:start;gap:20px}.project-media{height:220px}.project-top{display:block}.project-status{display:block}.site-foot{gap:16px}.foot-logo{font-size:15px}}
    </style>
</head>
<body>
<header class="site-head shell">
    <a class="logo" href="#top" aria-label="XASS">XASS</a>
    <nav class="site-nav" aria-label="Основная навигация">
        <a href="#about">Обо мне</a><a href="#projects">Проекты</a><a href="#contacts">Контакты</a>
    </nav>
</header>
<main id="top">
    <section class="hero shell">
        <div class="hero-copy reveal">
            <h1><?= xass_escape($headline) ?></h1>
            <p class="hero-sub"><?= xass_escape($bio) ?></p>
            <div class="hero-actions">
                <a class="action primary" href="#projects">Смотреть проекты <span>→</span></a>
                <?php if ($telegramUrl !== ''): ?><a class="action" href="<?= xass_escape($telegramUrl) ?>" target="_blank" rel="noopener">Написать в Telegram <span>↗</span></a><?php endif; ?>
            </div>
            <div class="status-rail">
                <svg class="signal" viewBox="0 0 36 18" fill="none" stroke="currentColor"><path d="M1 9h3l2-5 3 11 3-9 3 6 3-11 3 16 3-12 3 7 3-5 3 2h3"/></svg>
                <span>Сейчас в наушниках:</span><strong><?= xass_escape($nowPlaying !== '' ? $nowPlaying : 'тишина') ?></strong><span>•</span><span class="status-dot"></span><span><?= $available ? 'Сейчас онлайн' : 'Доступен для новых задач' ?></span>
            </div>
        </div>
        <div class="hero-art reveal"><img src="/assets/xass-hero-glass.png" alt="Абстрактная стеклянная форма X"></div>
    </section>

    <section class="section" id="about"><div class="shell section-grid reveal">
        <h2>Обо мне</h2>
        <div class="manifesto"><h3>Делаю сложное <span>спокойным.</span></h3><p><?= xass_escape($bio) ?></p>
            <?php if ($stack): ?><div class="stack"><?php foreach ($stack as $item): ?><span><?= xass_escape($item) ?></span><?php endforeach; ?></div><?php endif; ?>
            <?php if ($quote !== ''): ?><div class="quote">«<?= xass_escape($quote) ?>»</div><?php endif; ?>
        </div>
    </div></section>

    <section class="section" id="projects"><div class="shell reveal">
        <div class="projects-head"><h2>Избранные проекты</h2><span class="projects-count"><?= count($projects) ?> / PROJECTS</span></div>
        <div class="project-list">
        <?php foreach ($projects as $index => $project):
            if (!is_array($project)) continue;
            $cover = is_array($project['cover'] ?? null) ? $project['cover'] : [];
            $url = trim((string)($project['url'] ?? ''));
        ?>
            <article class="project">
                <div class="project-media">
                    <?php if (($cover['src'] ?? '') !== '' && ($cover['type'] ?? 'image') === 'video'): ?><video autoplay muted loop playsinline><source src="<?= xass_escape($cover['src']) ?>"></video>
                    <?php elseif (($cover['src'] ?? '') !== ''): ?><img src="<?= xass_escape($cover['src']) ?>" alt="">
                    <?php endif; ?><span class="project-mark">XASS / <?= str_pad((string)($index + 1), 2, '0', STR_PAD_LEFT) ?></span>
                </div>
                <div class="project-body"><div class="project-top"><h3><?= xass_escape($project['title'] ?? 'Проект') ?></h3><span class="project-status">● <?= xass_escape($project['status'] ?? 'dev') ?></span></div><p><?= xass_escape($project['description'] ?? '') ?></p>
                    <div class="project-meta"><?php foreach ((array)($project['tags'] ?? []) as $tag): ?><span><?= xass_escape($tag) ?></span><?php endforeach; ?><span><?= xass_escape(($project['years']['from'] ?? '') . '—' . ($project['years']['to'] ?? '')) ?></span><?php if ($url !== ''): ?><a href="<?= xass_escape($url) ?>" target="_blank" rel="noopener">Открыть ↗</a><?php endif; ?></div>
                </div>
            </article>
        <?php endforeach; ?>
        </div>
    </div></section>

    <section class="section" id="contacts"><div class="shell contact-grid reveal">
        <h2>Контакты</h2><div class="contact-links">
            <?php if ($telegramUrl !== ''): ?><a class="contact-link" href="<?= xass_escape($telegramUrl) ?>" target="_blank" rel="noopener"><span>Telegram</span><small>НАПИСАТЬ ↗</small></a><?php endif; ?>
            <?php foreach ($links as $link): if (!is_array($link) || empty($link['url']) || empty($link['label'])) continue; ?><a class="contact-link" href="<?= xass_escape($link['url']) ?>" target="_blank" rel="noopener"><span><?= xass_escape($link['label']) ?></span><small>ОТКРЫТЬ ↗</small></a><?php endforeach; ?>
            <?php if ($weather !== ''): ?><div class="contact-link"><span><?= xass_escape($weather) ?></span><small>СЕЙЧАС</small></div><?php endif; ?>
        </div>
    </div></section>
</main>
<footer class="site-foot shell"><span class="foot-logo">XASS</span><span class="foot-line"></span><span>XASS — systems that stay alive.</span></footer>
<script>document.documentElement.classList.add('js');const io=new IntersectionObserver(entries=>entries.forEach(e=>{if(e.isIntersecting){e.target.classList.add('visible');io.unobserve(e.target)}}),{threshold:.12});document.querySelectorAll('.reveal').forEach(el=>io.observe(el));</script>
</body>
</html>
