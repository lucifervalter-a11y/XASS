<?php
declare(strict_types=1);

$projectsPath = getenv('PROJECTS_JSON_PATH');
if (!$projectsPath) {
    $projectsPath = __DIR__ . '/data/projects.json';
}

$siteConfigPath = getenv('SITE_CONFIG_JSON_PATH');
if (!$siteConfigPath) {
    $siteConfigPath = __DIR__ . '/data/site_config.json';
}

function esc(mixed $value): string
{
    return htmlspecialchars((string)$value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function text_value(mixed $value, string $fallback = ''): string
{
    if (is_string($value)) {
        $trimmed = trim($value);
        return $trimmed !== '' ? $trimmed : $fallback;
    }
    if (is_scalar($value)) {
        $trimmed = trim((string)$value);
        return $trimmed !== '' ? $trimmed : $fallback;
    }
    return $fallback;
}

function int_value(mixed $value, int $fallback, int $min, int $max): int
{
    if (is_bool($value)) {
        return $fallback;
    }
    if (is_int($value)) {
        $parsed = $value;
    } elseif (is_float($value)) {
        $parsed = (int)$value;
    } elseif (is_string($value) && is_numeric(trim($value))) {
        $parsed = (int)trim($value);
    } else {
        return $fallback;
    }
    if ($parsed < $min) {
        return $min;
    }
    if ($parsed > $max) {
        return $max;
    }
    return $parsed;
}

function bool_value(mixed $value, bool $fallback = false): bool
{
    if (is_bool($value)) {
        return $value;
    }
    if (is_numeric($value)) {
        return (int)$value !== 0;
    }
    if (is_string($value)) {
        $raw = strtolower(trim($value));
        if (in_array($raw, ['1', 'true', 'yes', 'on'], true)) {
            return true;
        }
        if (in_array($raw, ['0', 'false', 'no', 'off'], true)) {
            return false;
        }
    }
    return $fallback;
}

function normalize_external_url(mixed $value): string
{
    $text = text_value($value);
    if ($text === '') {
        return '';
    }
    if (preg_match('~^https?://~i', $text) === 1) {
        return $text;
    }
    return '';
}

function normalize_media_src(mixed $value): string
{
    $text = text_value($value);
    if ($text === '') {
        return '';
    }
    if ($text[0] === '/') {
        return $text;
    }
    if (preg_match('~^https?://~i', $text) === 1) {
        return $text;
    }
    // Backward compatibility for values like "assets/projects/file.jpg"
    return '/' . ltrim($text, '/');
}

function normalize_tags(mixed $value): array
{
    if (is_string($value)) {
        $parts = preg_split('/[,\s;]+/u', $value) ?: [];
    } elseif (is_array($value)) {
        $parts = [];
        foreach ($value as $item) {
            $text = text_value($item);
            if ($text === '') {
                continue;
            }
            $split = preg_split('/[,\s;]+/u', $text) ?: [];
            foreach ($split as $part) {
                $parts[] = $part;
            }
        }
    } else {
        return [];
    }
    $result = [];
    $seen = [];
    foreach ($parts as $item) {
        $tag = text_value($item);
        if ($tag === '') {
            continue;
        }
        $key = strtolower($tag);
        if (isset($seen[$key])) {
            continue;
        }
        $seen[$key] = true;
        $result[] = $tag;
    }
    return $result;
}

function normalize_cover(mixed $value): array
{
    $type = 'image';
    $src = '';
    if (is_array($value)) {
        $rawType = strtolower(text_value($value['type'] ?? 'image', 'image'));
        if (in_array($rawType, ['image', 'video'], true)) {
            $type = $rawType;
        }
        $src = normalize_media_src($value['src'] ?? '');
    }
    return ['type' => $type, 'src' => $src];
}

function normalize_status(mixed $value): string
{
    $status = strtolower(text_value($value, 'dev'));
    $allowed = ['working', 'testing', 'dev', 'unstable', 'archived', 'stable'];
    return in_array($status, $allowed, true) ? $status : 'dev';
}

function normalize_project(array $raw, int $index): array
{
    $years = is_array($raw['years'] ?? null) ? $raw['years'] : [];
    $from = int_value($years['from'] ?? null, (int)date('Y'), 1970, 2100);
    $to = int_value($years['to'] ?? null, $from, 1970, 2100);
    if ($to < $from) {
        $to = $from;
    }

    return [
        'id' => text_value($raw['id'] ?? '', "project-{$index}"),
        'title' => text_value($raw['title'] ?? '', "Project {$index}"),
        'subtitle' => text_value($raw['subtitle'] ?? ''),
        'description' => text_value($raw['description'] ?? ''),
        'url' => normalize_external_url($raw['url'] ?? ''),
        'status' => normalize_status($raw['status'] ?? 'dev'),
        'years' => ['from' => $from, 'to' => $to],
        'tags' => normalize_tags($raw['tags'] ?? []),
        'featured' => bool_value($raw['featured'] ?? false),
        'cover' => normalize_cover($raw['cover'] ?? []),
        'sort' => int_value($raw['sort'] ?? 100, 100, -999999, 999999),
    ];
}

function load_projects(string $path): array
{
    $fallback = [[
        'id' => 'demo-project',
        'title' => 'Demo Project',
        'subtitle' => 'Добавьте проекты через бота',
        'description' => 'Откройте бота и добавьте проекты в разделе "Проекты".',
        'url' => '',
        'status' => 'dev',
        'years' => ['from' => (int)date('Y'), 'to' => (int)date('Y')],
        'tags' => ['python', 'fastapi'],
        'featured' => true,
        'cover' => ['type' => 'image', 'src' => ''],
        'sort' => 100,
    ]];

    if (!is_file($path)) {
        return array_map(static fn(array $item, int $idx): array => normalize_project($item, $idx + 1), $fallback, array_keys($fallback));
    }

    $rawJson = @file_get_contents($path);
    if (!is_string($rawJson) || trim($rawJson) === '') {
        return array_map(static fn(array $item, int $idx): array => normalize_project($item, $idx + 1), $fallback, array_keys($fallback));
    }

    $decoded = json_decode($rawJson, true);
    if (!is_array($decoded)) {
        return array_map(static fn(array $item, int $idx): array => normalize_project($item, $idx + 1), $fallback, array_keys($fallback));
    }

    $projects = [];
    $i = 1;
    foreach ($decoded as $item) {
        if (!is_array($item)) {
            continue;
        }
        $projects[] = normalize_project($item, $i);
        $i++;
    }

    if (count($projects) === 0) {
        return array_map(static fn(array $item, int $idx): array => normalize_project($item, $idx + 1), $fallback, array_keys($fallback));
    }

    usort(
        $projects,
        static fn(array $a, array $b): int => [$a['sort'], strtolower((string)$a['title'])] <=> [$b['sort'], strtolower((string)$b['title'])]
    );
    return $projects;
}

function load_background(string $path): array
{
    $default = ['type' => 'image', 'src' => ''];
    if (!is_file($path)) {
        return $default;
    }
    $rawJson = @file_get_contents($path);
    if (!is_string($rawJson) || trim($rawJson) === '') {
        return $default;
    }
    $decoded = json_decode($rawJson, true);
    if (!is_array($decoded)) {
        return $default;
    }
    $bg = is_array($decoded['projects_background'] ?? null) ? $decoded['projects_background'] : [];
    $type = strtolower(text_value($bg['type'] ?? 'image', 'image'));
    if (!in_array($type, ['image', 'video'], true)) {
        $type = 'image';
    }
    return ['type' => $type, 'src' => normalize_media_src($bg['src'] ?? '')];
}

function status_meta(string $status): array
{
    return match ($status) {
        'working' => ['Рабочий', 'status-working'],
        'testing' => ['Тестирование', 'status-testing'],
        'stable' => ['Стабильно', 'status-stable'],
        'unstable' => ['Нестабильно', 'status-unstable'],
        'archived' => ['Архив', 'status-archived'],
        default => ['Разработка', 'status-dev'],
    };
}

$projects = load_projects($projectsPath);
$background = load_background($siteConfigPath);
$featured = null;
foreach ($projects as $project) {
    if ((bool)$project['featured']) {
        $featured = $project;
        break;
    }
}
if ($featured === null && count($projects) > 0) {
    $featured = $projects[0];
}

$list = [];
foreach ($projects as $item) {
    if ($featured !== null && $item['id'] === $featured['id']) {
        continue;
    }
    $list[] = $item;
}
?>
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Проекты</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;700;800&family=JetBrains+Mono:wght@500;700&display=swap');

        :root {
            --bg: #04060d;
            --panel: rgba(8, 13, 24, 0.88);
            --line: rgba(116, 168, 235, 0.24);
            --text: #f0f6ff;
            --muted: #98abcb;
            --accent: #4ec1ff;
            --accent-strong: #2f8cf3;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            min-height: 100vh;
            color: var(--text);
            font-family: "Manrope", "Segoe UI", Tahoma, sans-serif;
            background:
                radial-gradient(1100px 560px at 12% -14%, rgba(56, 123, 214, 0.34), transparent 60%),
                radial-gradient(920px 460px at 88% 0%, rgba(82, 87, 194, 0.24), transparent 64%),
                var(--bg);
            overflow-x: hidden;
            position: relative;
        }

        .bg-media {
            position: fixed;
            inset: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            z-index: -3;
            opacity: 0.25;
            pointer-events: none;
            filter: saturate(1.05) contrast(1.06);
        }

        .backdrop {
            position: fixed;
            inset: 0;
            z-index: -2;
            background:
                linear-gradient(180deg, rgba(2, 4, 9, 0.28), rgba(2, 4, 9, 0.84)),
                repeating-linear-gradient(0deg, rgba(132, 175, 255, 0.05) 0 1px, transparent 1px 56px),
                repeating-linear-gradient(90deg, rgba(122, 158, 222, 0.04) 0 1px, transparent 1px 56px);
        }

        .wrap {
            width: min(100% - 30px, 1180px);
            margin: 24px auto 38px;
            display: grid;
            gap: 14px;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            border: 1px solid var(--line);
            border-radius: 14px;
            background: rgba(7, 12, 24, 0.84);
            padding: 10px 12px;
            backdrop-filter: blur(8px);
        }

        .back-link {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            text-decoration: none;
            color: #dff3ff;
            border: 1px solid rgba(120, 182, 255, 0.34);
            border-radius: 10px;
            padding: 8px 12px;
            font-weight: 700;
            background: rgba(12, 22, 38, 0.88);
        }

        .back-link:hover {
            border-color: rgba(157, 220, 255, 0.76);
        }

        .title {
            margin: 0;
            font-size: clamp(20px, 2.8vw, 34px);
            letter-spacing: 0.01em;
        }

        .subtitle {
            margin: 4px 0 0;
            color: var(--muted);
            font-size: 14px;
        }

        .card {
            border: 1px solid var(--line);
            border-radius: 16px;
            background: linear-gradient(165deg, rgba(10, 17, 31, 0.9), rgba(6, 11, 21, 0.92));
            overflow: hidden;
            box-shadow: 0 20px 50px rgba(5, 12, 29, 0.34);
        }

        .card-media {
            height: 220px;
            width: 100%;
            object-fit: cover;
            display: block;
            background:
                radial-gradient(300px 110px at 20% 10%, rgba(125, 219, 255, 0.16), transparent 68%),
                linear-gradient(130deg, #122240, #0a1428);
        }

        .card-video {
            width: 100%;
            height: 220px;
            object-fit: cover;
            display: block;
            background: #0a1221;
        }

        .card-body {
            padding: 14px 14px 16px;
        }

        .meta-row {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 10px;
            flex-wrap: wrap;
        }

        .project-title {
            margin: 0;
            font-size: clamp(20px, 2vw, 30px);
            font-weight: 800;
            letter-spacing: 0.01em;
        }

        .project-subtitle {
            margin: 4px 0 0;
            color: #abc0de;
            font-size: 14px;
        }

        .status {
            display: inline-flex;
            align-items: center;
            min-height: 30px;
            border-radius: 999px;
            border: 1px solid transparent;
            padding: 0 10px;
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.02em;
            text-transform: uppercase;
            font-family: "JetBrains Mono", monospace;
        }

        .status-working { border-color: rgba(82, 222, 173, 0.5); background: rgba(20, 74, 53, 0.76); color: #b7ffd9; }
        .status-testing { border-color: rgba(127, 195, 255, 0.5); background: rgba(19, 54, 84, 0.76); color: #cce9ff; }
        .status-dev { border-color: rgba(219, 173, 109, 0.54); background: rgba(90, 56, 20, 0.76); color: #ffe0b2; }
        .status-unstable { border-color: rgba(242, 112, 117, 0.54); background: rgba(89, 24, 31, 0.76); color: #ffd2d5; }
        .status-archived { border-color: rgba(128, 146, 176, 0.5); background: rgba(24, 33, 48, 0.76); color: #cbd6ea; }
        .status-stable { border-color: rgba(95, 205, 255, 0.54); background: rgba(17, 53, 77, 0.76); color: #d2f3ff; }

        .years {
            margin-top: 8px;
            color: #b9cae4;
            font-size: 13px;
            font-family: "JetBrains Mono", monospace;
        }

        .description {
            margin: 12px 0 0;
            color: #e5efff;
            font-size: 15px;
            line-height: 1.52;
        }

        .tags {
            margin-top: 12px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }

        .tag {
            display: inline-flex;
            align-items: center;
            min-height: 26px;
            padding: 0 9px;
            border-radius: 999px;
            border: 1px solid rgba(123, 173, 236, 0.36);
            background: rgba(15, 26, 45, 0.82);
            color: #bdd4f3;
            font-size: 12px;
            font-weight: 700;
        }

        .card-actions {
            margin-top: 14px;
        }

        .open-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 38px;
            border-radius: 11px;
            border: 1px solid rgba(111, 185, 255, 0.42);
            background: linear-gradient(130deg, rgba(31, 114, 190, 0.88), rgba(22, 83, 161, 0.88));
            color: #f6fbff;
            text-decoration: none;
            font-weight: 800;
            padding: 0 14px;
        }

        .open-link:hover {
            border-color: rgba(165, 223, 255, 0.84);
            box-shadow: 0 10px 24px rgba(37, 112, 197, 0.32);
        }

        .open-link.disabled {
            border-color: rgba(118, 136, 167, 0.28);
            background: rgba(15, 24, 38, 0.82);
            color: #9cb0d0;
            pointer-events: none;
        }

        .featured {
            display: grid;
            grid-template-columns: 1.12fr 1fr;
        }

        .featured .card-media,
        .featured .card-video {
            height: 100%;
            min-height: 300px;
        }

        .projects-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 14px;
        }

        .empty {
            border: 1px dashed rgba(122, 167, 226, 0.35);
            border-radius: 14px;
            padding: 18px;
            color: #b2c6e5;
            background: rgba(9, 17, 32, 0.74);
        }

        @media (max-width: 980px) {
            .featured {
                grid-template-columns: 1fr;
            }
            .featured .card-media,
            .featured .card-video {
                min-height: 220px;
            }
            .projects-grid {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
<?php if ($background['type'] === 'image' && $background['src'] !== ''): ?>
    <img class="bg-media" src="<?= esc($background['src']) ?>" alt="" aria-hidden="true">
<?php elseif ($background['type'] === 'video' && $background['src'] !== ''): ?>
    <video class="bg-media" autoplay muted loop playsinline aria-hidden="true">
        <source src="<?= esc($background['src']) ?>">
    </video>
<?php endif; ?>
<div class="backdrop"></div>

<main class="wrap">
    <header class="topbar">
        <a class="back-link" href="/profile.php">← Вернуться назад</a>
        <div>
            <h1 class="title">Проекты</h1>
            <p class="subtitle">Актуальный список из data/projects.json</p>
        </div>
    </header>

    <?php if ($featured !== null): ?>
        <?php [$featuredStatusText, $featuredStatusClass] = status_meta($featured['status']); ?>
        <article class="card featured">
            <?php if ($featured['cover']['src'] !== '' && $featured['cover']['type'] === 'image'): ?>
                <img class="card-media" src="<?= esc($featured['cover']['src']) ?>" alt="<?= esc($featured['title']) ?>">
            <?php elseif ($featured['cover']['src'] !== '' && $featured['cover']['type'] === 'video'): ?>
                <video class="card-video" autoplay muted loop playsinline>
                    <source src="<?= esc($featured['cover']['src']) ?>">
                </video>
            <?php else: ?>
                <div class="card-media" aria-hidden="true"></div>
            <?php endif; ?>

            <div class="card-body">
                <div class="meta-row">
                    <div>
                        <h2 class="project-title"><?= esc($featured['title']) ?></h2>
                        <?php if ($featured['subtitle'] !== ''): ?>
                            <p class="project-subtitle"><?= esc($featured['subtitle']) ?></p>
                        <?php endif; ?>
                    </div>
                    <span class="status <?= esc($featuredStatusClass) ?>"><?= esc($featuredStatusText) ?></span>
                </div>
                <div class="years"><?= esc((string)$featured['years']['from']) ?>-<?= esc((string)$featured['years']['to']) ?></div>
                <p class="description"><?= nl2br(esc($featured['description'] !== '' ? $featured['description'] : 'Описание не указано.')) ?></p>
                <div class="tags">
                    <?php foreach ($featured['tags'] as $tag): ?>
                        <span class="tag"><?= esc($tag) ?></span>
                    <?php endforeach; ?>
                </div>
                <div class="card-actions">
                    <?php if ($featured['url'] !== ''): ?>
                        <a class="open-link" href="<?= esc($featured['url']) ?>" target="_blank" rel="noopener noreferrer">Открыть проект</a>
                    <?php else: ?>
                        <span class="open-link disabled">Ссылка недоступна</span>
                    <?php endif; ?>
                </div>
            </div>
        </article>
    <?php endif; ?>

    <?php if (!empty($list)): ?>
        <section class="projects-grid">
            <?php foreach ($list as $item): ?>
                <?php [$statusText, $statusClass] = status_meta($item['status']); ?>
                <article class="card">
                    <?php if ($item['cover']['src'] !== '' && $item['cover']['type'] === 'image'): ?>
                        <img class="card-media" src="<?= esc($item['cover']['src']) ?>" alt="<?= esc($item['title']) ?>">
                    <?php elseif ($item['cover']['src'] !== '' && $item['cover']['type'] === 'video'): ?>
                        <video class="card-video" muted preload="metadata" controls playsinline>
                            <source src="<?= esc($item['cover']['src']) ?>">
                        </video>
                    <?php else: ?>
                        <div class="card-media" aria-hidden="true"></div>
                    <?php endif; ?>

                    <div class="card-body">
                        <div class="meta-row">
                            <div>
                                <h3 class="project-title"><?= esc($item['title']) ?></h3>
                                <?php if ($item['subtitle'] !== ''): ?>
                                    <p class="project-subtitle"><?= esc($item['subtitle']) ?></p>
                                <?php endif; ?>
                            </div>
                            <span class="status <?= esc($statusClass) ?>"><?= esc($statusText) ?></span>
                        </div>
                        <div class="years"><?= esc((string)$item['years']['from']) ?>-<?= esc((string)$item['years']['to']) ?></div>
                        <p class="description"><?= nl2br(esc($item['description'] !== '' ? $item['description'] : 'Описание не указано.')) ?></p>
                        <div class="tags">
                            <?php foreach ($item['tags'] as $tag): ?>
                                <span class="tag"><?= esc($tag) ?></span>
                            <?php endforeach; ?>
                        </div>
                        <div class="card-actions">
                            <?php if ($item['url'] !== ''): ?>
                                <a class="open-link" href="<?= esc($item['url']) ?>" target="_blank" rel="noopener noreferrer">Открыть проект</a>
                            <?php else: ?>
                                <span class="open-link disabled">Ссылка недоступна</span>
                            <?php endif; ?>
                        </div>
                    </div>
                </article>
            <?php endforeach; ?>
        </section>
    <?php elseif ($featured === null): ?>
        <div class="empty">
            Проекты пока не добавлены. Откройте бота и создайте первый проект в меню "Проекты".
        </div>
    <?php endif; ?>
</main>
</body>
</html>
