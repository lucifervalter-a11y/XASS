<?php
declare(strict_types=1);

$defaultProfile = [
    'name' => 'Ваше имя',
    'title' => 'Full-stack разработчик',
    'bio' => 'Коротко о себе',
    'username' => 'username',
    'telegram_url' => 'https://t.me/username',
    'links' => [
        ['label' => 'GitHub', 'url' => 'https://github.com/username'],
    ],
    'stack' => ['Python', 'FastAPI', 'PostgreSQL'],
    'quote' => 'Делаем просто, надежно и без магии.',
    'now_listening_text' => 'Не указано',
    'now_listening_auto_enabled' => true,
    'now_listening_updated_at' => '',
    'weather_text' => 'Не указано',
    'weather_auto_enabled' => true,
    'weather_location_name' => 'Москва',
    'weather_latitude' => 55.7558,
    'weather_longitude' => 37.6176,
    'weather_timezone' => 'Europe/Moscow',
    'weather_refresh_minutes' => 60,
    'weather_updated_at' => '',
    'avatar_url' => '',
];

$profilePath = getenv('PROFILE_JSON_PATH');
if (!$profilePath) {
    $profilePath = __DIR__ . '/data/profile.json';
}

function toStringSafe(mixed $value, string $fallback = ''): string
{
    if (is_string($value)) {
        return trim($value);
    }
    if (is_scalar($value)) {
        return trim((string)$value);
    }
    return $fallback;
}

function toBoolSafe(mixed $value, bool $fallback = false): bool
{
    if (is_bool($value)) {
        return $value;
    }
    if (is_numeric($value)) {
        return ((int)$value) !== 0;
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

function toFloatSafe(mixed $value, float $fallback): float
{
    if (is_bool($value)) {
        return $fallback;
    }
    if (is_int($value) || is_float($value)) {
        return (float)$value;
    }
    if (is_string($value)) {
        $raw = str_replace(',', '.', trim($value));
        if ($raw === '' || !is_numeric($raw)) {
            return $fallback;
        }
        return (float)$raw;
    }
    return $fallback;
}

function toIntSafe(mixed $value, int $fallback, int $minValue, int $maxValue): int
{
    if (is_bool($value)) {
        return $fallback;
    }
    if (is_int($value)) {
        $parsed = $value;
    } elseif (is_float($value)) {
        $parsed = (int)$value;
    } elseif (is_string($value)) {
        $raw = str_replace(',', '.', trim($value));
        if ($raw === '' || !is_numeric($raw)) {
            return $fallback;
        }
        $parsed = (int)$raw;
    } else {
        return $fallback;
    }
    if ($parsed < $minValue) {
        return $minValue;
    }
    if ($parsed > $maxValue) {
        return $maxValue;
    }
    return $parsed;
}

function normalizeLinks(mixed $value): array
{
    if (!is_array($value)) {
        return [];
    }

    $result = [];
    foreach ($value as $item) {
        if (!is_array($item)) {
            continue;
        }
        $label = toStringSafe($item['label'] ?? '');
        $url = toStringSafe($item['url'] ?? '');
        if ($label === '' || $url === '') {
            continue;
        }
        $result[] = ['label' => $label, 'url' => $url];
    }
    return $result;
}

function normalizeStack(mixed $value): array
{
    if (!is_array($value)) {
        return [];
    }

    $result = [];
    foreach ($value as $item) {
        $text = toStringSafe($item);
        if ($text !== '') {
            $result[] = $text;
        }
    }
    return $result;
}

function escapeHtml(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function extractTelegramUsername(string $url): string
{
    $trimmed = trim($url);
    if ($trimmed === '') {
        return '';
    }

    $path = parse_url($trimmed, PHP_URL_PATH);
    if (!is_string($path) || $path === '') {
        return '';
    }

    $candidate = trim($path, "/ \t\n\r\0\x0B");
    if ($candidate === '') {
        return '';
    }
    if (str_contains($candidate, '/')) {
        return '';
    }
    if (str_starts_with(strtolower($candidate), 'joinchat')) {
        return '';
    }
    return ltrim($candidate, '@');
}

function httpGet(string $url, int $timeoutSeconds = 8): ?string
{
    if (function_exists('curl_init')) {
        $ch = curl_init($url);
        if ($ch !== false) {
            curl_setopt_array($ch, [
                CURLOPT_RETURNTRANSFER => true,
                CURLOPT_FOLLOWLOCATION => true,
                CURLOPT_TIMEOUT => $timeoutSeconds,
                CURLOPT_CONNECTTIMEOUT => min(4, $timeoutSeconds),
                CURLOPT_USERAGENT => 'profile-weather/1.0',
            ]);
            $body = curl_exec($ch);
            $code = (int)curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
            curl_close($ch);
            if (is_string($body) && $body !== '' && $code >= 200 && $code < 300) {
                return $body;
            }
        }
    }

    $context = stream_context_create([
        'http' => [
            'method' => 'GET',
            'timeout' => $timeoutSeconds,
            'ignore_errors' => true,
            'header' => "User-Agent: profile-weather/1.0\r\n",
        ],
    ]);
    $body = @file_get_contents($url, false, $context);
    if (is_string($body) && $body !== '') {
        return $body;
    }

    if (DIRECTORY_SEPARATOR === '\\' && function_exists('shell_exec')) {
        $safeUrl = str_replace("'", "''", $url);
        $psScript = "\$ErrorActionPreference = 'Stop'; "
            . "(Invoke-WebRequest -UseBasicParsing -Uri '{$safeUrl}' -TimeoutSec "
            . max(1, $timeoutSeconds)
            . ").Content";
        $psCommand = 'powershell -NoProfile -Command "' . str_replace('"', '\\"', $psScript) . '"';
        $psBody = @shell_exec($psCommand);
        if (is_string($psBody) && trim($psBody) !== '') {
            return trim($psBody);
        }
    }

    return null;
}

function weatherCodeToRu(int $code): string
{
    return match ($code) {
        0 => 'Ясно',
        1 => 'Малооблачно',
        2 => 'Переменная облачность',
        3 => 'Пасмурно',
        45, 48 => 'Туман',
        51, 53, 55 => 'Морось',
        56, 57 => 'Ледяная морось',
        61, 63, 65 => 'Дождь',
        66, 67 => 'Ледяной дождь',
        71, 73, 75 => 'Снег',
        77 => 'Снежная крупа',
        80, 81, 82 => 'Ливень',
        85, 86 => 'Снегопад',
        95 => 'Гроза',
        96, 99 => 'Гроза с градом',
        default => 'Без уточнения',
    };
}

function weatherIconFromText(string $text): string
{
    $normalized = function_exists('mb_strtolower')
        ? mb_strtolower($text, 'UTF-8')
        : strtolower($text);

    if (str_contains($normalized, 'снег') || str_contains($normalized, 'snow')) {
        return '❄️';
    }
    if (
        str_contains($normalized, 'дожд')
        || str_contains($normalized, 'ливен')
        || str_contains($normalized, 'rain')
    ) {
        return '🌧️';
    }
    if (str_contains($normalized, 'гроз') || str_contains($normalized, 'thunder')) {
        return '⛈️';
    }
    if (str_contains($normalized, 'туман') || str_contains($normalized, 'fog')) {
        return '🌫️';
    }
    if (str_contains($normalized, 'ясно') || str_contains($normalized, 'sun')) {
        return '☀️';
    }
    if (str_contains($normalized, 'облач') || str_contains($normalized, 'cloud')) {
        return '☁️';
    }
    return '🌤️';
}

function formatFloatCompact(mixed $value, int $precision = 1): string
{
    if (!is_numeric($value)) {
        return '';
    }
    $formatted = number_format((float)$value, $precision, '.', '');
    return rtrim(rtrim($formatted, '0'), '.');
}

function fetchWeatherTextByLocation(string $locationName, float $latitude, float $longitude, string $timezoneName): ?string
{
    $query = http_build_query([
        'latitude' => $latitude,
        'longitude' => $longitude,
        'current' => 'temperature_2m,apparent_temperature,weather_code,wind_speed_10m',
        'timezone' => $timezoneName,
    ]);
    $url = 'https://api.open-meteo.com/v1/forecast?' . $query;

    $body = httpGet($url, 8);
    if ($body === null) {
        return null;
    }

    $data = json_decode($body, true);
    if (!is_array($data)) {
        return null;
    }

    $current = $data['current'] ?? null;
    if (!is_array($current) || !isset($current['temperature_2m'])) {
        return null;
    }

    $temperature = formatFloatCompact($current['temperature_2m']);
    if ($temperature === '') {
        return null;
    }
    $apparent = formatFloatCompact($current['apparent_temperature'] ?? null);
    $wind = formatFloatCompact($current['wind_speed_10m'] ?? null);
    $weatherCode = is_numeric($current['weather_code'] ?? null) ? (int)$current['weather_code'] : -1;

    $parts = ["{$locationName}: {$temperature}°C", weatherCodeToRu($weatherCode)];
    if ($apparent !== '') {
        $parts[] = "ощущается как {$apparent}°C";
    }
    if ($wind !== '') {
        $parts[] = "ветер {$wind} м/с";
    }

    $result = implode(', ', $parts);
    $time = toStringSafe($current['time'] ?? '');
    if ($time !== '') {
        try {
            $zone = new DateTimeZone($timezoneName !== '' ? $timezoneName : 'UTC');
            $updated = new DateTimeImmutable($time, $zone);
            $updated = $updated->setTimezone($zone);
            $result .= ' (обновлено ' . $updated->format('H:i T') . ')';
        } catch (Throwable) {
            $timestamp = strtotime($time);
            if ($timestamp !== false) {
                $result .= ' (обновлено ' . date('H:i', $timestamp) . ')';
            }
        }
    }

    return $result;
}

function getCachedOrFreshWeather(
    string $cachePath,
    string $locationName,
    float $latitude,
    float $longitude,
    string $timezoneName,
    int $ttlSeconds = 3600
): ?string {
    if (is_readable($cachePath)) {
        $rawCache = file_get_contents($cachePath);
        if (is_string($rawCache) && $rawCache !== '') {
            $cacheData = json_decode($rawCache, true);
            if (is_array($cacheData)) {
                $cachedText = toStringSafe($cacheData['text'] ?? '');
                $cachedTs = is_numeric($cacheData['ts'] ?? null) ? (int)$cacheData['ts'] : 0;
                if (
                    $cachedText !== ''
                    && $cachedTs > 0
                    && toStringSafe($cacheData['location_name'] ?? '') === $locationName
                    && abs(toFloatSafe($cacheData['latitude'] ?? null, $latitude) - $latitude) < 0.000001
                    && abs(toFloatSafe($cacheData['longitude'] ?? null, $longitude) - $longitude) < 0.000001
                    && toStringSafe($cacheData['timezone'] ?? '') === $timezoneName
                    && (time() - $cachedTs) <= $ttlSeconds
                ) {
                    return $cachedText;
                }
            }
        }
    }

    $fresh = fetchWeatherTextByLocation($locationName, $latitude, $longitude, $timezoneName);
    if ($fresh === null) {
        return null;
    }

    $cacheDir = dirname($cachePath);
    if (!is_dir($cacheDir)) {
        @mkdir($cacheDir, 0777, true);
    }

    @file_put_contents(
        $cachePath,
        json_encode(
            [
                'ts' => time(),
                'text' => $fresh,
                'location_name' => $locationName,
                'latitude' => $latitude,
                'longitude' => $longitude,
                'timezone' => $timezoneName,
            ],
            JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT
        )
    );

    return $fresh;
}

$profile = $defaultProfile;
if (is_readable($profilePath)) {
    $raw = file_get_contents($profilePath);
    if (is_string($raw) && $raw !== '') {
        $decoded = json_decode($raw, true);
        if (is_array($decoded)) {
            $profile['name'] = toStringSafe($decoded['name'] ?? $profile['name'], $profile['name']);
            $profile['title'] = toStringSafe($decoded['title'] ?? $profile['title'], $profile['title']);
            $profile['bio'] = toStringSafe($decoded['bio'] ?? $profile['bio'], $profile['bio']);
            $profile['username'] = toStringSafe($decoded['username'] ?? $profile['username'], $profile['username']);
            $profile['telegram_url'] = toStringSafe($decoded['telegram_url'] ?? $profile['telegram_url'], $profile['telegram_url']);
            $profile['quote'] = toStringSafe($decoded['quote'] ?? $profile['quote'], $profile['quote']);
            $profile['now_listening_text'] = toStringSafe($decoded['now_listening_text'] ?? $profile['now_listening_text'], $profile['now_listening_text']);
            $profile['now_listening_auto_enabled'] = toBoolSafe($decoded['now_listening_auto_enabled'] ?? $profile['now_listening_auto_enabled'], $profile['now_listening_auto_enabled']);
            $profile['now_listening_updated_at'] = toStringSafe($decoded['now_listening_updated_at'] ?? $profile['now_listening_updated_at']);
            $profile['weather_text'] = toStringSafe($decoded['weather_text'] ?? $profile['weather_text'], $profile['weather_text']);
            $profile['weather_auto_enabled'] = toBoolSafe($decoded['weather_auto_enabled'] ?? $profile['weather_auto_enabled'], $profile['weather_auto_enabled']);
            $profile['weather_location_name'] = toStringSafe($decoded['weather_location_name'] ?? $profile['weather_location_name'], $profile['weather_location_name']);
            $profile['weather_latitude'] = toFloatSafe($decoded['weather_latitude'] ?? $profile['weather_latitude'], $profile['weather_latitude']);
            $profile['weather_longitude'] = toFloatSafe($decoded['weather_longitude'] ?? $profile['weather_longitude'], $profile['weather_longitude']);
            $profile['weather_timezone'] = toStringSafe($decoded['weather_timezone'] ?? $profile['weather_timezone'], $profile['weather_timezone']);
            $profile['weather_refresh_minutes'] = toIntSafe(
                $decoded['weather_refresh_minutes'] ?? $profile['weather_refresh_minutes'],
                $profile['weather_refresh_minutes'],
                10,
                720
            );
            $profile['weather_updated_at'] = toStringSafe($decoded['weather_updated_at'] ?? $profile['weather_updated_at']);
            $profile['avatar_url'] = toStringSafe($decoded['avatar_url'] ?? $profile['avatar_url']);

            $links = normalizeLinks($decoded['links'] ?? null);
            if ($links) {
                $profile['links'] = $links;
            }

            $stack = normalizeStack($decoded['stack'] ?? null);
            if ($stack) {
                $profile['stack'] = $stack;
            }
        }
    }
}

$weatherFromJson = toStringSafe($profile['weather_text'] ?? '');
$weatherAutoEnabled = toBoolSafe($profile['weather_auto_enabled'] ?? true, true);
$weatherLocationName = toStringSafe($profile['weather_location_name'] ?? 'Москва', 'Москва');
$weatherLatitude = toFloatSafe($profile['weather_latitude'] ?? 55.7558, 55.7558);
$weatherLongitude = toFloatSafe($profile['weather_longitude'] ?? 37.6176, 37.6176);
$weatherTimezone = toStringSafe($profile['weather_timezone'] ?? 'Europe/Moscow', 'Europe/Moscow');
$weatherRefreshMinutes = toIntSafe($profile['weather_refresh_minutes'] ?? 60, 60, 10, 720);
$weatherUpdatedAt = toStringSafe($profile['weather_updated_at'] ?? '');
$weatherUpdatedTs = $weatherUpdatedAt !== '' ? strtotime($weatherUpdatedAt) : false;
$weatherStale = ($weatherUpdatedTs === false) || ((time() - $weatherUpdatedTs) > ($weatherRefreshMinutes * 60));

if (
    $weatherAutoEnabled
    && (
        $weatherFromJson === ''
        || preg_match('/^не указано$/iu', $weatherFromJson) === 1
        || $weatherStale
    )
) {
    $weatherCachePath = __DIR__ . '/data/weather_cache_profile.json';
    $autoWeather = getCachedOrFreshWeather(
        $weatherCachePath,
        $weatherLocationName,
        $weatherLatitude,
        $weatherLongitude,
        $weatherTimezone,
        $weatherRefreshMinutes * 60
    );
    if (is_string($autoWeather) && $autoWeather !== '') {
        $profile['weather_text'] = $autoWeather;
    }
}

$displayUsername = toStringSafe($profile['username'] ?? '');
if ($displayUsername === '' || strcasecmp($displayUsername, 'username') === 0) {
    $fromUrl = extractTelegramUsername(toStringSafe($profile['telegram_url'] ?? ''));
    if ($fromUrl !== '') {
        $displayUsername = $fromUrl;
    }
}

$links = is_array($profile['links'] ?? null) ? $profile['links'] : [];
$mainLinks = array_slice($links, 0, 3);
$moreLinks = array_slice($links, 3);
$nowListeningText = toStringSafe($profile['now_listening_text'] ?? '');
$noTrack = (
    $nowListeningText === ''
    || preg_match('/^(не указано|нет данных|сейчас ничего не играет)$/iu', $nowListeningText) === 1
);
$canSearchTrack = !$noTrack;
$trackQuery = $canSearchTrack ? rawurlencode($nowListeningText) : '';
$trackSearchLinks = [
    'Shazam' => $canSearchTrack ? "https://www.shazam.com/search/{$trackQuery}" : '',
    'Google' => $canSearchTrack ? "https://www.google.com/search?q={$trackQuery}" : '',
    'VK Music' => $canSearchTrack ? "https://vk.com/search?c%5Bq%5D={$trackQuery}&c%5Bsection%5D=audio" : '',
];

$weatherText = toStringSafe($profile['weather_text'] ?? '');
$weatherParts = array_values(array_filter(
    array_map(static fn(string $part): string => trim($part), explode(',', $weatherText)),
    static fn(string $part): bool => $part !== ''
));
$weatherMainLine = $weatherParts[0] ?? ($weatherText !== '' ? $weatherText : 'Погода обновляется...');
$weatherDetails = array_slice($weatherParts, 1, 4);
$weatherIcon = weatherIconFromText($weatherText !== '' ? $weatherText : $weatherMainLine);
$telegramLabel = $displayUsername !== '' ? "t.me/{$displayUsername}" : 'Открыть Telegram';
$weatherLabel = $weatherLocationName !== '' ? "Погода · {$weatherLocationName}" : 'Погода';
$projectsPageUrl = '/projects.php';
?>
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title><?= escapeHtml($profile['name']) ?> — Профиль</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;600&display=swap');

        :root {
            --bg: #04080f;
            --glass: rgba(10, 17, 32, 0.70);
            --glass-2: rgba(14, 22, 42, 0.62);
            --border: rgba(255, 255, 255, 0.07);
            --border-hover: rgba(72, 186, 255, 0.48);
            --glow: 72, 186, 255;
            --accent: #48bafe;
            --accent2: #7c6ffe;
            --pink: #ff6b9d;
            --text: #eef2ff;
            --muted: #7b96c2;
            --muted2: #a8bfe0;
            --mono: 'JetBrains Mono', monospace;
        }

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        html { scroll-behavior: smooth; }

        body {
            font-family: 'Manrope', system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            overflow-x: hidden;
            line-height: 1.5;
        }

        /* ── STAR FIELD ─────────────────────────────────────── */
        body::before {
            content: '';
            position: fixed;
            inset: 0;
            z-index: 0;
            pointer-events: none;
            background-image:
                radial-gradient(1px 1px at 8%  12%, rgba(255,255,255,0.55) 0%, transparent 100%),
                radial-gradient(1px 1px at 22% 44%, rgba(255,255,255,0.40) 0%, transparent 100%),
                radial-gradient(1.5px 1.5px at 37%  7%, rgba(255,255,255,0.50) 0%, transparent 100%),
                radial-gradient(1px 1px at 52% 68%, rgba(255,255,255,0.45) 0%, transparent 100%),
                radial-gradient(1px 1px at 67% 28%, rgba(255,255,255,0.35) 0%, transparent 100%),
                radial-gradient(1.5px 1.5px at 79% 53%, rgba(255,255,255,0.50) 0%, transparent 100%),
                radial-gradient(1px 1px at 91% 83%, rgba(255,255,255,0.40) 0%, transparent 100%),
                radial-gradient(1px 1px at 13% 74%, rgba(255,255,255,0.30) 0%, transparent 100%),
                radial-gradient(1px 1px at 33% 91%, rgba(255,255,255,0.35) 0%, transparent 100%),
                radial-gradient(1.5px 1.5px at 58% 49%, rgba(255,255,255,0.28) 0%, transparent 100%),
                radial-gradient(1px 1px at 86% 11%, rgba(255,255,255,0.45) 0%, transparent 100%),
                radial-gradient(1px 1px at  4% 57%, rgba(255,255,255,0.25) 0%, transparent 100%),
                radial-gradient(1px 1px at 74% 96%, rgba(255,255,255,0.35) 0%, transparent 100%),
                radial-gradient(1px 1px at 46% 34%, rgba(255,255,255,0.20) 0%, transparent 100%),
                radial-gradient(1.5px 1.5px at 18% 21%, rgba(180,210,255,0.40) 0%, transparent 100%),
                radial-gradient(1px 1px at 63% 77%, rgba(180,210,255,0.30) 0%, transparent 100%),
                radial-gradient(1px 1px at 94% 43%, rgba(255,255,255,0.25) 0%, transparent 100%),
                radial-gradient(1px 1px at  2% 89%, rgba(255,255,255,0.30) 0%, transparent 100%),
                radial-gradient(1px 1px at 43% 60%, rgba(255,255,255,0.22) 0%, transparent 100%),
                radial-gradient(1.5px 1.5px at 29% 33%, rgba(200,220,255,0.35) 0%, transparent 100%);
            animation: twinkle 7s ease-in-out infinite alternate;
        }
        @keyframes twinkle { from { opacity: 0.65; } to { opacity: 1; } }

        /* ── ANIMATED ORBS ──────────────────────────────────── */
        .orb {
            position: fixed;
            border-radius: 50%;
            filter: blur(88px);
            pointer-events: none;
            z-index: 0;
            will-change: transform;
        }
        .orb-1 {
            width: 640px; height: 520px;
            background: radial-gradient(ellipse, rgba(48, 122, 236, 0.30), transparent 70%);
            top: -18%; left: -12%;
            animation: orbDrift1 26s ease-in-out infinite;
        }
        .orb-2 {
            width: 520px; height: 460px;
            background: radial-gradient(ellipse, rgba(100, 80, 255, 0.26), transparent 70%);
            top: 2%; right: -10%;
            animation: orbDrift2 30s ease-in-out infinite;
        }
        .orb-3 {
            width: 420px; height: 380px;
            background: radial-gradient(ellipse, rgba(30, 200, 220, 0.16), transparent 70%);
            bottom: 12%; left: 18%;
            animation: orbDrift3 34s ease-in-out infinite;
        }
        .orb-4 {
            width: 360px; height: 320px;
            background: radial-gradient(ellipse, rgba(255, 75, 130, 0.12), transparent 70%);
            bottom: -8%; right: 12%;
            animation: orbDrift4 24s ease-in-out infinite;
        }
        @keyframes orbDrift1 {
            0%,100% { transform: translate(0,0) scale(1); }
            35%  { transform: translate(5%,5%) scale(1.09); }
            68%  { transform: translate(-3%,8%) scale(0.94); }
        }
        @keyframes orbDrift2 {
            0%,100% { transform: translate(0,0) scale(1); }
            42%  { transform: translate(-7%,4%) scale(1.07); }
            72%  { transform: translate(5%,-6%) scale(1.02); }
        }
        @keyframes orbDrift3 {
            0%,100% { transform: translate(0,0) scale(1); }
            50%  { transform: translate(9%,-7%) scale(1.12); }
        }
        @keyframes orbDrift4 {
            0%,100% { transform: translate(0,0) scale(1); }
            47%  { transform: translate(-6%,5%) scale(0.88); }
        }

        /* ── LAYOUT ─────────────────────────────────────────── */
        .page {
            position: relative;
            z-index: 1;
            width: min(1180px, 100% - 32px);
            margin: 38px auto 64px;
            display: grid;
            grid-template-columns: minmax(0, 1fr) 380px;
            gap: 20px;
            align-items: start;
        }
        .main-col { display: flex; flex-direction: column; gap: 16px; }
        .aside    { display: flex; flex-direction: column; gap: 14px; }

        /* ── GLASS CARD BASE ────────────────────────────────── */
        .card {
            background: var(--glass);
            border: 1px solid var(--border);
            border-radius: 24px;
            backdrop-filter: blur(22px) saturate(160%);
            -webkit-backdrop-filter: blur(22px) saturate(160%);
            box-shadow:
                0 4px 8px rgba(0,0,0,0.12),
                0 18px 44px rgba(0,0,0,0.36),
                inset 0 1px 0 rgba(255,255,255,0.06);
            transition: border-color 0.3s ease, box-shadow 0.3s ease;
            overflow: hidden;
        }
        .card:hover {
            border-color: var(--border-hover);
            box-shadow:
                0 4px 8px rgba(0,0,0,0.12),
                0 22px 54px rgba(0,0,0,0.42),
                0 0 0 1px rgba(72,186,255,0.12),
                inset 0 1px 0 rgba(255,255,255,0.09);
        }

        /* ── ENTRANCE ANIMATIONS ─────────────────────────────── */
        @keyframes slideUp {
            from { opacity: 0; transform: translateY(30px); }
            to   { opacity: 1; transform: translateY(0); }
        }
        @keyframes slideLeft {
            from { opacity: 0; transform: translateX(30px); }
            to   { opacity: 1; transform: translateX(0); }
        }
        .main-col > .card:nth-child(1) { animation: slideUp 0.65s cubic-bezier(0.16,1,0.3,1) 0.05s both; }
        .main-col > .card:nth-child(2) { animation: slideUp 0.65s cubic-bezier(0.16,1,0.3,1) 0.18s both; }
        .main-col > .card:nth-child(3) { animation: slideUp 0.65s cubic-bezier(0.16,1,0.3,1) 0.30s both; }
        .aside > .card:nth-child(1) { animation: slideLeft 0.70s cubic-bezier(0.16,1,0.3,1) 0.08s both; }
        .aside > .card:nth-child(2) { animation: slideLeft 0.70s cubic-bezier(0.16,1,0.3,1) 0.20s both; }
        .aside > .card:nth-child(3) { animation: slideLeft 0.70s cubic-bezier(0.16,1,0.3,1) 0.32s both; }

        /* ── HERO CARD ──────────────────────────────────────── */
        .hero { padding: 28px 28px 24px; }

        .hero-tag {
            display: inline-flex;
            align-items: center;
            gap: 7px;
            font-size: 11.5px;
            font-weight: 800;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            color: var(--accent);
            background: rgba(72,186,255,0.09);
            border: 1px solid rgba(72,186,255,0.22);
            border-radius: 999px;
            padding: 5px 13px;
            margin-bottom: 18px;
        }
        .hero-tag-dot {
            width: 6px; height: 6px;
            border-radius: 50%;
            background: var(--accent);
            animation: blink 2.2s ease-in-out infinite;
        }
        @keyframes blink { 0%,100%{ opacity:1; } 50%{ opacity:0.25; } }

        .headline {
            font-size: clamp(27px, 3.6vw, 48px);
            font-weight: 900;
            line-height: 1.06;
            letter-spacing: -0.025em;
        }
        .headline-name {
            background: linear-gradient(130deg, #ffffff 25%, var(--accent) 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .hero-role {
            margin-top: 10px;
            font-size: 17px;
            font-weight: 500;
            color: var(--muted2);
            letter-spacing: 0.01em;
        }
        .hero-bio {
            margin-top: 16px;
            font-size: 17px;
            line-height: 1.68;
            color: rgba(218, 232, 255, 0.88);
            max-width: 68ch;
        }

        /* ── TECH STACK ─────────────────────────────────────── */
        .stack { margin-top: 20px; display: flex; flex-wrap: wrap; gap: 8px; }
        .chip {
            font-size: 12px;
            font-weight: 700;
            font-family: var(--mono);
            padding: 7px 14px;
            border-radius: 999px;
            background: rgba(72,186,255,0.08);
            border: 1px solid rgba(72,186,255,0.20);
            color: var(--accent);
            letter-spacing: 0.04em;
            transition: background 0.22s, border-color 0.22s, transform 0.22s;
            cursor: default;
        }
        .chip:hover {
            background: rgba(72,186,255,0.17);
            border-color: rgba(72,186,255,0.52);
            transform: translateY(-2px);
        }

        /* ── LINKS GRID ─────────────────────────────────────── */
        .links-grid {
            margin-top: 24px;
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
        }
        .link-card {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 14px 16px;
            border-radius: 16px;
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border);
            color: var(--text);
            text-decoration: none;
            font-weight: 600;
            font-size: 15px;
            min-height: 58px;
            transition: all 0.25s cubic-bezier(0.16,1,0.3,1);
        }
        .link-card:hover {
            background: rgba(72,186,255,0.10);
            border-color: rgba(72,186,255,0.46);
            transform: translateY(-3px);
            box-shadow: 0 10px 28px rgba(72,186,255,0.16);
        }
        .link-num {
            width: 32px; height: 32px;
            border-radius: 10px;
            background: rgba(72,186,255,0.11);
            border: 1px solid rgba(72,186,255,0.24);
            display: grid; place-items: center;
            font-family: var(--mono);
            font-size: 13px; font-weight: 700;
            color: var(--accent);
            flex-shrink: 0;
        }
        .link-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

        /* ── PROJECTS BUTTON ────────────────────────────────── */
        .projects-btn {
            margin-top: 10px;
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 12px 20px;
            border-radius: 14px;
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(107,177,246,0.38);
            color: var(--text);
            text-decoration: none;
            font-weight: 700;
            font-size: 15px;
            min-height: 46px;
            transition: all 0.25s ease;
        }
        .projects-btn:hover {
            background: rgba(107,177,246,0.12);
            border-color: rgba(107,177,246,0.68);
            transform: translateY(-2px);
            box-shadow: 0 10px 28px rgba(72,186,255,0.16);
        }

        /* ── EXTRA LINKS ────────────────────────────────────── */
        .more-links { margin-top: 10px; display: flex; flex-wrap: wrap; gap: 8px; }
        .mini-link {
            display: inline-flex; align-items: center; gap: 8px;
            padding: 10px 16px;
            border-radius: 12px;
            background: rgba(255,255,255,0.03);
            border: 1px solid var(--border);
            color: var(--muted2);
            text-decoration: none;
            font-size: 14px; font-weight: 600;
            min-height: 44px;
            transition: all 0.22s ease;
        }
        .mini-link:hover {
            border-color: rgba(72,186,255,0.42);
            color: var(--text);
            background: rgba(72,186,255,0.08);
        }

        /* ── TELEGRAM BUTTON ─────────────────────────────────── */
        .tg-btn {
            margin-top: 16px;
            display: flex;
            align-items: center;
            gap: 14px;
            padding: 14px 20px;
            border-radius: 18px;
            text-decoration: none;
            color: #fff;
            background:
                linear-gradient(135deg, rgba(255,255,255,0.13) 0%, transparent 55%),
                linear-gradient(140deg, #2aabee 0%, #229ed9 50%, #1a87c2 100%);
            border: 1px solid rgba(255,255,255,0.22);
            box-shadow:
                0 8px 32px rgba(34,158,217,0.42),
                inset 0 1px 0 rgba(255,255,255,0.26);
            transition: all 0.30s cubic-bezier(0.16,1,0.3,1);
            position: relative;
            overflow: hidden;
        }
        .tg-btn::after {
            content: '';
            position: absolute;
            inset: 0;
            background: linear-gradient(135deg, rgba(255,255,255,0.16), transparent 55%);
            opacity: 0;
            transition: opacity 0.3s ease;
            pointer-events: none;
        }
        .tg-btn:hover {
            transform: translateY(-3px);
            box-shadow: 0 16px 48px rgba(34,158,217,0.56), inset 0 1px 0 rgba(255,255,255,0.30);
        }
        .tg-btn:hover::after { opacity: 1; }
        .tg-icon {
            width: 48px; height: 48px;
            border-radius: 14px;
            background: rgba(0,0,0,0.24);
            border: 1px solid rgba(255,255,255,0.20);
            display: grid; place-items: center;
            font-size: 26px; line-height: 1;
            flex-shrink: 0;
        }
        .tg-meta { flex: 1; overflow: hidden; }
        .tg-label {
            font-size: 11px; font-weight: 700;
            opacity: 0.75; letter-spacing: 0.08em;
            text-transform: uppercase; margin-bottom: 2px;
        }
        .tg-url {
            font-size: clamp(17px, 2.2vw, 26px);
            font-weight: 800; letter-spacing: 0.01em;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
        }
        .tg-arrow { font-size: 20px; opacity: 0.65; flex-shrink: 0; }

        /* ── QUOTE CARD ─────────────────────────────────────── */
        .quote-card { padding: 0; }
        .quote-hdr {
            padding: 18px 24px 14px;
            font-size: 11px; font-weight: 800;
            letter-spacing: 0.12em; text-transform: uppercase;
            color: var(--muted);
            border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: 10px;
        }
        .quote-hdr-bar {
            width: 4px; height: 14px; border-radius: 2px; flex-shrink: 0;
            background: linear-gradient(180deg, var(--accent), var(--accent2));
        }
        .quote-body {
            padding: 22px 24px 24px;
            font-size: 17px;
            line-height: 1.68;
            color: rgba(218,232,255,0.90);
            font-style: italic;
            position: relative;
        }
        .quote-body::before {
            content: '\201C';
            position: absolute;
            top: 6px; left: 14px;
            font-size: 84px; line-height: 1;
            color: rgba(72,186,255,0.10);
            font-family: Georgia, 'Times New Roman', serif;
            font-style: normal;
            pointer-events: none;
        }

        /* ── PROFILE IDENTITY CARD ───────────────────────────── */
        .profile-card { padding: 0; }
        .profile-banner {
            height: 112px;
            background:
                radial-gradient(ellipse 80% 130% at 28% 50%, rgba(50,118,236,0.45), transparent 65%),
                radial-gradient(ellipse 70% 120% at 82% 38%, rgba(120,100,254,0.42), transparent 65%),
                radial-gradient(ellipse 60% 90%  at 52% 82%, rgba(255,100,150,0.22), transparent 70%),
                linear-gradient(135deg, #0d1a36, #0a1328);
            position: relative; overflow: hidden;
        }
        .profile-banner::after {
            content: '';
            position: absolute;
            bottom: -1px; left: 0; right: 0;
            height: 44px;
            background: linear-gradient(to bottom, transparent, var(--glass));
            pointer-events: none;
        }
        .profile-body { padding: 0 20px 22px; margin-top: -46px; }

        /* ── AVATAR ─────────────────────────────────────────── */
        .avatar-wrap {
            display: inline-block;
            position: relative;
            width: 90px; height: 90px;
        }
        .avatar-ring {
            position: absolute;
            inset: -4px; border-radius: 50%;
            background: conic-gradient(from 0deg, var(--accent), var(--accent2), var(--pink), var(--accent));
            animation: spinRing 5s linear infinite;
            z-index: 0;
        }
        @keyframes spinRing { to { transform: rotate(360deg); } }
        .avatar-inner {
            position: relative; z-index: 1;
            width: 90px; height: 90px;
            border-radius: 50%;
            background: var(--bg);
            padding: 3px;
        }
        .avatar-img, .avatar-placeholder {
            width: 100%; height: 100%;
            border-radius: 50%;
            display: block;
        }
        .avatar-img { object-fit: cover; }
        .avatar-placeholder {
            background: linear-gradient(135deg, #1a3461, #0f2040);
            display: grid; place-items: center;
            font-size: 26px; font-weight: 900;
            color: var(--accent); letter-spacing: -0.02em;
        }

        .user-realname {
            font-family: var(--mono);
            font-size: 11.5px;
            color: rgba(72,186,255,0.65);
            margin-top: 12px;
        }
        .handle {
            font-size: clamp(24px, 3.2vw, 36px);
            font-weight: 900; letter-spacing: -0.01em;
            margin-top: 5px; line-height: 1.1;
        }
        .handle-at { color: var(--muted); font-weight: 400; }
        .user-title { margin-top: 5px; font-size: 14px; color: var(--muted); font-weight: 500; }

        /* ── SECTION LABEL ──────────────────────────────────── */
        .card-label {
            font-size: 11px; font-weight: 800;
            letter-spacing: 0.13em; text-transform: uppercase;
            color: var(--muted);
            margin-bottom: 14px;
            display: flex; align-items: center; gap: 8px;
        }

        /* ── MUSIC CARD ─────────────────────────────────────── */
        .music-card { padding: 20px; }

        .now-playing-bars {
            display: flex; align-items: flex-end; gap: 3px;
            height: 16px;
        }
        .eq-bar {
            width: 3px; border-radius: 2px;
            background: var(--accent);
            transform-origin: bottom;
            animation: eq 0.75s ease-in-out infinite alternate;
        }
        .eq-bar:nth-child(1) { height: 6px;  animation-delay: 0.00s; }
        .eq-bar:nth-child(2) { height: 12px; animation-delay: 0.18s; }
        .eq-bar:nth-child(3) { height: 8px;  animation-delay: 0.36s; }
        @keyframes eq {
            from { transform: scaleY(0.25); opacity: 0.6; }
            to   { transform: scaleY(1);    opacity: 1; }
        }
        .eq-bar.idle {
            animation: none;
            height: 4px; opacity: 0.35;
        }

        .track-name {
            font-size: 15.5px; font-weight: 700;
            line-height: 1.38; color: var(--text);
            margin-bottom: 4px;
        }
        .track-none { color: var(--muted); font-size: 15px; font-weight: 500; }

        .music-btns { margin-top: 13px; display: flex; flex-wrap: wrap; gap: 7px; }
        .music-btn {
            display: inline-flex; align-items: center;
            padding: 6px 12px;
            border-radius: 999px;
            border: 1px solid rgba(72,186,255,0.26);
            background: rgba(72,186,255,0.07);
            color: var(--accent);
            font-size: 12px; font-weight: 700;
            text-decoration: none;
            transition: all 0.2s ease;
            min-height: 30px;
        }
        .music-btn:hover {
            background: rgba(72,186,255,0.17);
            border-color: rgba(72,186,255,0.58);
            transform: translateY(-1px);
        }
        .music-btn.disabled {
            border-color: var(--border);
            color: var(--muted); background: transparent;
            pointer-events: none;
        }

        /* ── WEATHER CARD ───────────────────────────────────── */
        .weather-card {
            padding: 20px;
            background:
                radial-gradient(ellipse 90% 60% at 92% 5%, rgba(48,120,236,0.11), transparent 60%),
                var(--glass);
        }
        .weather-top { display: flex; align-items: center; gap: 14px; }
        .weather-emoji {
            font-size: 46px; line-height: 1;
            filter: drop-shadow(0 0 14px rgba(72,186,255,0.42));
        }
        .weather-temp-block { flex: 1; min-width: 0; }
        .weather-temp {
            font-size: clamp(30px, 4.5vw, 44px);
            font-weight: 900; letter-spacing: -0.025em; line-height: 1;
            background: linear-gradient(130deg, #ffffff 40%, var(--accent) 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        .weather-condition {
            font-size: 14px; font-weight: 600; color: var(--muted2);
            margin-top: 4px; line-height: 1.3;
        }
        .weather-loc {
            font-size: 12px; color: var(--muted);
            font-weight: 500; margin-top: 3px;
        }
        .weather-badges { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 6px; }
        .weather-badge {
            display: inline-flex; align-items: center;
            padding: 5px 11px;
            border-radius: 999px;
            border: 1px solid rgba(72,186,255,0.20);
            background: rgba(72,186,255,0.06);
            color: var(--muted2);
            font-size: 12px; font-weight: 600;
        }

        /* ── RESPONSIVE ─────────────────────────────────────── */
        @media (max-width: 1080px) {
            .page { grid-template-columns: 1fr; }
            .aside { flex-direction: row; flex-wrap: wrap; order: -1; }
            .profile-card { flex: 1 1 320px; }
            .music-card   { flex: 1 1 220px; }
            .weather-card { flex: 1 1 220px; }
            .links-grid { grid-template-columns: repeat(2, 1fr); }
        }
        @media (max-width: 580px) {
            .page { width: calc(100% - 20px); margin: 14px auto 40px; gap: 12px; }
            .hero { padding: 20px 18px 18px; }
            .headline { font-size: 27px; }
            .hero-role { font-size: 15px; }
            .hero-bio { font-size: 15.5px; margin-top: 13px; }
            .links-grid { grid-template-columns: 1fr; }
            .aside { flex-direction: column; }
            .profile-card, .music-card, .weather-card { flex: 1 1 100%; }
            .tg-url { font-size: 18px; }
            .weather-temp { font-size: 30px; }
            .weather-emoji { font-size: 38px; }
            .card { border-radius: 18px; }
            .quote-body { font-size: 15.5px; padding: 18px 18px 20px; }
            .hero-tag { font-size: 10.5px; }
            .tg-btn { padding: 12px 16px; gap: 12px; }
            .tg-icon { width: 42px; height: 42px; font-size: 22px; }
            .profile-banner { height: 96px; }
            .avatar-wrap { width: 78px; height: 78px; }
            .avatar-inner { width: 78px; height: 78px; }
            .avatar-placeholder { font-size: 22px; }
            .handle { font-size: 26px; }
        }
        @media (max-width: 380px) {
            .page { width: calc(100% - 14px); }
            .headline { font-size: 24px; }
        }
    </style>
</head>
<body>
<div class="orb orb-1" aria-hidden="true"></div>
<div class="orb orb-2" aria-hidden="true"></div>
<div class="orb orb-3" aria-hidden="true"></div>
<div class="orb orb-4" aria-hidden="true"></div>

<main class="page">

    <!-- ════ MAIN COLUMN ════ -->
    <div class="main-col">

        <!-- HERO -->
        <article class="card hero">
            <div class="hero-tag">
                <span class="hero-tag-dot" aria-hidden="true"></span>
                Профиль
            </div>
            <h1 class="headline">Привет, я&nbsp;<span class="headline-name"><?= escapeHtml($profile['name']) ?></span></h1>
            <p class="hero-role"><?= escapeHtml($profile['title']) ?></p>
            <p class="hero-bio"><?= nl2br(escapeHtml($profile['bio'])) ?></p>

            <div class="stack">
                <?php foreach ($profile['stack'] as $tech): ?>
                    <span class="chip"><?= escapeHtml($tech) ?></span>
                <?php endforeach; ?>
            </div>

            <div class="links-grid">
                <?php foreach ($mainLinks as $idx => $link): ?>
                    <?php $lu = toStringSafe($link['url'] ?? ''); $ll = toStringSafe($link['label'] ?? 'Ссылка'); ?>
                    <?php if ($lu === '') continue; ?>
                    <a class="link-card" href="<?= escapeHtml($lu) ?>" target="_blank" rel="noopener noreferrer">
                        <span class="link-num"><?= $idx + 1 ?></span>
                        <span class="link-label"><?= escapeHtml($ll) ?></span>
                    </a>
                <?php endforeach; ?>
            </div>

            <a class="projects-btn" href="<?= escapeHtml($projectsPageUrl) ?>">↗&nbsp; Проекты</a>

            <?php if (!empty($moreLinks)): ?>
                <div class="more-links">
                    <?php foreach ($moreLinks as $link): ?>
                        <?php $lu = toStringSafe($link['url'] ?? ''); $ll = toStringSafe($link['label'] ?? 'Ссылка'); ?>
                        <?php if ($lu === '') continue; ?>
                        <a class="mini-link" href="<?= escapeHtml($lu) ?>" target="_blank" rel="noopener noreferrer">
                            ↗ <?= escapeHtml($ll) ?>
                        </a>
                    <?php endforeach; ?>
                </div>
            <?php endif; ?>

            <a class="tg-btn" href="<?= escapeHtml($profile['telegram_url']) ?>" target="_blank" rel="noopener noreferrer">
                <span class="tg-icon" aria-hidden="true">✈</span>
                <span class="tg-meta">
                    <div class="tg-label">Написать в Telegram</div>
                    <div class="tg-url"><?= escapeHtml($telegramLabel) ?></div>
                </span>
                <span class="tg-arrow" aria-hidden="true">→</span>
            </a>
        </article>

        <!-- QUOTE -->
        <article class="card quote-card">
            <header class="quote-hdr">
                <span class="quote-hdr-bar" aria-hidden="true"></span>
                Цитата
            </header>
            <blockquote class="quote-body"><?= nl2br(escapeHtml($profile['quote'])) ?></blockquote>
        </article>

    </div><!-- .main-col -->

    <!-- ════ ASIDE ════ -->
    <aside class="aside">

        <!-- IDENTITY CARD -->
        <article class="card profile-card">
            <div class="profile-banner" aria-hidden="true"></div>
            <div class="profile-body">
                <div class="avatar-wrap">
                    <div class="avatar-ring" aria-hidden="true"></div>
                    <div class="avatar-inner">
                        <?php if ($profile['avatar_url'] !== ''): ?>
                            <img class="avatar-img"
                                 src="<?= escapeHtml($profile['avatar_url']) ?>"
                                 alt="Аватар <?= escapeHtml($profile['name']) ?>">
                        <?php else: ?>
                            <div class="avatar-placeholder" aria-hidden="true">
                                <?= escapeHtml(
                                    function_exists('mb_strtoupper')
                                        ? mb_strtoupper(mb_substr($profile['name'], 0, 2, 'UTF-8'), 'UTF-8')
                                        : strtoupper(substr($profile['name'], 0, 2))
                                ) ?>
                            </div>
                        <?php endif; ?>
                    </div>
                </div>
                <div class="user-realname">«<?= escapeHtml($profile['name']) ?>»</div>
                <h2 class="handle">
                    <span class="handle-at">@</span><?= escapeHtml($displayUsername !== '' ? $displayUsername : 'username') ?>
                </h2>
                <div class="user-title"><?= escapeHtml($profile['title']) ?></div>
            </div>
        </article>

        <!-- MUSIC CARD -->
        <article class="card music-card">
            <div class="card-label">
                <div class="now-playing-bars" aria-hidden="true">
                    <div class="eq-bar<?= $noTrack ? ' idle' : '' ?>"></div>
                    <div class="eq-bar<?= $noTrack ? ' idle' : '' ?>"></div>
                    <div class="eq-bar<?= $noTrack ? ' idle' : '' ?>"></div>
                </div>
                <?= $noTrack ? 'Музыка' : 'Сейчас играет' ?>
            </div>

            <?php if (!$noTrack): ?>
                <div class="track-name"><?= escapeHtml($nowListeningText) ?></div>
            <?php else: ?>
                <div class="track-none">Сейчас ничего не играет</div>
            <?php endif; ?>

            <div class="music-btns">
                <?php if ($canSearchTrack): ?>
                    <?php foreach ($trackSearchLinks as $label => $url): ?>
                        <a class="music-btn"
                           href="<?= escapeHtml($url) ?>"
                           target="_blank" rel="noopener noreferrer">
                            <?= escapeHtml($label) ?>
                        </a>
                    <?php endforeach; ?>
                <?php else: ?>
                    <span class="music-btn disabled">Нет трека</span>
                <?php endif; ?>
            </div>
        </article>

        <!-- WEATHER CARD -->
        <?php
            // Extract just the temperature portion for big display
            $bigTemp = '';
            if (preg_match('/(-?\d+(?:[.,]\d+)?\s*°[CF])/u', $weatherMainLine, $tempMatch)) {
                $bigTemp = $tempMatch[1];
            }
            // Condition = first detail line (e.g. "Ясно")
            $weatherCondition = $weatherDetails[0] ?? '';
            // Remaining details (feels-like, wind, updated)
            $weatherExtras = array_slice($weatherDetails, 1);
        ?>
        <article class="card weather-card">
            <div class="card-label"><?= escapeHtml($weatherLabel) ?></div>
            <div class="weather-top">
                <div class="weather-emoji" aria-hidden="true"><?= escapeHtml($weatherIcon) ?></div>
                <div class="weather-temp-block">
                    <div class="weather-temp"><?= escapeHtml($bigTemp !== '' ? $bigTemp : $weatherMainLine) ?></div>
                    <?php if ($weatherCondition !== ''): ?>
                        <div class="weather-condition"><?= escapeHtml($weatherCondition) ?></div>
                    <?php endif; ?>
                    <div class="weather-loc"><?= escapeHtml($weatherLocationName) ?></div>
                </div>
            </div>
            <?php if (!empty($weatherExtras)): ?>
                <div class="weather-badges">
                    <?php foreach ($weatherExtras as $badge): ?>
                        <span class="weather-badge"><?= escapeHtml($badge) ?></span>
                    <?php endforeach; ?>
                </div>
            <?php endif; ?>
        </article>

    </aside><!-- .aside -->

</main>
</body>
</html>
