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
    <title><?= escapeHtml($profile['name']) ?> - Профиль</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;500;700;800&family=JetBrains+Mono:wght@400;600&display=swap');

        :root {
            --bg-main: #05070c;
            --card: rgba(8, 14, 25, 0.86);
            --card-soft: rgba(11, 18, 33, 0.76);
            --line: rgba(113, 156, 214, 0.22);
            --line-strong: rgba(113, 156, 214, 0.34);
            --text: #f3f7ff;
            --muted: #97a9c8;
            --glow: 72, 186, 255;
        }

        * { box-sizing: border-box; }

        body {
            margin: 0;
            font-family: "Manrope", "Segoe UI", Tahoma, sans-serif;
            background:
                radial-gradient(1100px 580px at 8% -12%, rgba(48, 116, 201, 0.28), transparent 58%),
                radial-gradient(850px 520px at 90% 0%, rgba(63, 87, 180, 0.18), transparent 58%),
                var(--bg-main);
            color: var(--text);
            line-height: 1.45;
            min-height: 100vh;
            overflow-x: hidden;
            position: relative;
            animation: bgShift 16s ease-in-out infinite alternate;
        }

        body::before {
            content: "";
            position: fixed;
            inset: 0;
            z-index: -1;
            pointer-events: none;
            background-image:
                linear-gradient(rgba(255, 255, 255, 0.04) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.04) 1px, transparent 1px);
            background-size: 56px 56px;
            opacity: 0.22;
        }

        body::after {
            content: "";
            position: fixed;
            inset: -28%;
            z-index: -2;
            pointer-events: none;
            background:
                radial-gradient(45% 40% at 18% 24%, rgba(57, 143, 246, 0.20), transparent 72%),
                radial-gradient(35% 38% at 82% 18%, rgba(96, 111, 255, 0.20), transparent 70%),
                radial-gradient(42% 46% at 52% 80%, rgba(43, 180, 242, 0.14), transparent 74%);
            filter: blur(16px) saturate(112%);
            animation: auroraSweep 26s linear infinite;
        }

        @keyframes auroraSweep {
            0% {
                transform: translate3d(-5%, -2%, 0) scale(1);
                filter: blur(16px) saturate(112%) hue-rotate(0deg);
            }
            50% {
                transform: translate3d(6%, 4%, 0) scale(1.08);
                filter: blur(18px) saturate(118%) hue-rotate(16deg);
            }
            100% {
                transform: translate3d(-4%, 7%, 0) scale(1.03);
                filter: blur(16px) saturate(112%) hue-rotate(-10deg);
            }
        }

        @keyframes bgShift {
            0% {
                background-position: 0 0, 0 0, 0 0;
            }
            100% {
                background-position: 4% 0, -5% 3%, 0 0;
            }
        }

        .page {
            width: min(1160px, 100% - 36px);
            margin: 30px auto 44px;
            display: grid;
            grid-template-columns: minmax(0, 1fr) 360px;
            gap: 24px;
            align-items: start;
        }

        .main-column { display: grid; gap: 14px; }

        .panel {
            background: linear-gradient(170deg, var(--card), var(--card-soft));
            border: 1px solid var(--line);
            border-radius: 20px;
            box-shadow:
                0 20px 42px rgba(0, 0, 0, 0.36),
                inset 0 1px 0 rgba(255, 255, 255, 0.04);
            backdrop-filter: blur(6px);
        }

        .hero { padding: 22px 22px 16px; }

        .headline {
            margin: 0;
            font-size: clamp(26px, 3vw, 39px);
            line-height: 1.08;
            font-weight: 800;
        }

        .headline span {
            color: #ff7f8d;
            text-shadow: 0 0 16px rgba(255, 127, 141, 0.45);
        }

        .role { margin: 6px 0 0; color: #b8cae7; font-size: 18px; }

        .bio {
            margin: 14px 0 0;
            color: #d9e6ff;
            font-size: 18px;
            line-height: 1.55;
            max-width: 72ch;
        }

        .stack-row {
            margin-top: 16px;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }

        .chip {
            font-size: 13px;
            line-height: 1;
            padding: 8px 11px;
            border-radius: 999px;
            border: 1px solid var(--line-strong);
            background: rgba(8, 16, 30, 0.75);
            color: #d5e5ff;
            font-weight: 600;
        }

        .cta-grid {
            margin-top: 16px;
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 10px;
        }

        .cta-link {
            display: flex;
            align-items: center;
            gap: 10px;
            min-height: 62px;
            border-radius: 14px;
            padding: 13px 14px;
            background: rgba(5, 10, 20, 0.86);
            border: 1px solid var(--line);
            color: #f2f7ff;
            text-decoration: none;
            transition: border-color 0.2s ease, transform 0.2s ease, background 0.2s ease;
        }

        .cta-link:hover {
            border-color: rgba(var(--glow), 0.52);
            background: rgba(9, 17, 33, 0.98);
            transform: translateY(-1px);
        }

        .cta-icon {
            width: 30px;
            height: 30px;
            border-radius: 8px;
            border: 1px solid var(--line-strong);
            display: grid;
            place-items: center;
            font-size: 13px;
            font-weight: 800;
            color: #b7d8ff;
            font-family: "JetBrains Mono", monospace;
            background: rgba(13, 23, 44, 0.9);
            flex: 0 0 30px;
        }

        .cta-text {
            font-size: clamp(14px, 1.45vw, 28px);
            font-weight: 700;
            line-height: 1.15;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }

        .more-links {
            margin-top: 10px;
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        .mini-link {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            min-height: 47px;
            padding: 10px 13px;
            border-radius: 12px;
            border: 1px solid var(--line);
            background: rgba(6, 12, 23, 0.84);
            color: #eaf2ff;
            text-decoration: none;
            font-size: 15px;
            font-weight: 600;
        }

        .mini-link:hover { border-color: rgba(var(--glow), 0.5); }

        .projects-link {
            margin-top: 10px;
            background: linear-gradient(140deg, rgba(17, 34, 58, 0.92), rgba(7, 15, 28, 0.92));
            border-color: rgba(107, 177, 246, 0.48);
            box-shadow: 0 10px 26px rgba(15, 44, 90, 0.22);
        }

        .projects-link:hover {
            border-color: rgba(150, 217, 255, 0.72);
            box-shadow: 0 12px 30px rgba(26, 75, 146, 0.34);
        }

        .telegram-strip {
            margin-top: 12px;
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 8px 10px;
            border-radius: 14px;
            text-decoration: none;
            color: #f7fcff;
            border: 1px solid rgba(105, 181, 229, 0.52);
            background:
                radial-gradient(440px 140px at 16% 48%, rgba(162, 238, 255, 0.26), transparent 50%),
                linear-gradient(97deg, rgba(42, 164, 231, 0.95), rgba(42, 130, 216, 0.76));
            box-shadow: 0 14px 34px rgba(18, 95, 163, 0.33);
        }

        .telegram-strip .plane {
            width: 46px;
            height: 46px;
            border-radius: 13px;
            background: rgba(0, 0, 0, 0.55);
            border: 1px solid rgba(255, 255, 255, 0.3);
            display: grid;
            place-items: center;
            font-size: 23px;
            line-height: 1;
            flex: 0 0 46px;
        }

        .telegram-strip .tg-url {
            margin-left: auto;
            font-size: clamp(18px, 2vw, 31px);
            font-weight: 800;
            letter-spacing: 0.01em;
        }

        .quote-panel { margin-top: 14px; padding: 0; overflow: hidden; }

        .quote-header {
            padding: 18px 20px 12px;
            font-size: clamp(20px, 2.4vw, 34px);
            font-weight: 800;
            border-bottom: 1px solid rgba(128, 153, 194, 0.15);
        }

        .quote-body {
            margin: 0;
            padding: 18px 20px 20px;
            font-size: 18px;
            color: #d8e7ff;
            line-height: 1.55;
            border-left: 3px solid rgba(72, 200, 255, 0.56);
            background: linear-gradient(145deg, rgba(8, 15, 30, 0.88), rgba(5, 11, 22, 0.92));
        }

        .aside-card { overflow: hidden; }

        .aside-banner {
            height: 120px;
            background:
                linear-gradient(130deg, rgba(71, 142, 229, 0.38), rgba(69, 67, 147, 0.46)),
                radial-gradient(290px 130px at 74% 38%, rgba(209, 119, 205, 0.27), transparent 65%),
                #0c1424;
            border-bottom: 1px solid rgba(153, 179, 224, 0.18);
        }

        .aside-body {
            padding: 0 18px 18px;
            margin-top: -40px;
        }

        .avatar {
            width: 86px;
            height: 86px;
            border-radius: 50%;
            object-fit: cover;
            border: 3px solid rgba(86, 152, 230, 0.72);
            box-shadow: 0 0 0 4px rgba(5, 10, 19, 0.92);
            background: linear-gradient(145deg, #2a4063, #0f1d33);
        }

        .muted-id {
            margin-top: 10px;
            color: #8ea1c2;
            font-size: 14px;
            font-family: "JetBrains Mono", monospace;
        }

        .handle {
            margin: 4px 0 0;
            font-size: clamp(28px, 2.9vw, 44px);
            font-weight: 800;
            letter-spacing: 0.01em;
        }

        .small-title { margin: 6px 0 0; color: #9eb3d4; font-size: 16px; }

        .status-grid {
            margin-top: 14px;
            display: grid;
            gap: 9px;
        }

        .status-row {
            border: 1px solid rgba(133, 160, 202, 0.18);
            border-radius: 11px;
            background: rgba(6, 11, 21, 0.76);
            padding: 10px 12px;
            position: relative;
            overflow: hidden;
        }

        .status-label {
            color: #9fb3d5;
            font-size: 13px;
            margin-bottom: 4px;
        }

        .status-value {
            color: #e7efff;
            font-size: 15px;
            line-height: 1.4;
            word-break: break-word;
        }

        .status-value a,
        .status-value a:visited {
            color: #ccf4ff;
            text-decoration: none;
        }

        .status-value a:hover { text-decoration: underline; }

        .music-actions {
            margin-top: 9px;
            display: flex;
            flex-wrap: wrap;
            gap: 7px;
        }

        .music-action {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 30px;
            padding: 0 10px;
            border-radius: 999px;
            border: 1px solid rgba(122, 181, 236, 0.44);
            background: linear-gradient(150deg, rgba(27, 55, 95, 0.9), rgba(15, 36, 66, 0.9));
            color: #dff0ff;
            text-decoration: none;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.02em;
            transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;
        }

        .music-action:hover {
            transform: translateY(-1px);
            border-color: rgba(154, 220, 255, 0.78);
            box-shadow: 0 7px 18px rgba(53, 132, 228, 0.27);
            text-decoration: none;
        }

        .music-action.disabled {
            border-color: rgba(129, 145, 173, 0.28);
            background: rgba(16, 24, 38, 0.75);
            color: #93a5c3;
            pointer-events: none;
        }

        .weather-row {
            border-color: rgba(91, 178, 250, 0.40);
            background: linear-gradient(150deg, rgba(12, 24, 43, 0.90), rgba(6, 14, 28, 0.90));
        }

        .weather-row::before {
            content: "";
            position: absolute;
            inset: -1px;
            z-index: 0;
            pointer-events: none;
            background:
                radial-gradient(120px 38px at 12% 0, rgba(109, 203, 255, 0.18), transparent 70%),
                radial-gradient(130px 38px at 90% 100%, rgba(136, 170, 255, 0.14), transparent 72%);
        }

        .weather-row > * {
            position: relative;
            z-index: 1;
        }

        .weather-main {
            display: flex;
            align-items: center;
            gap: 9px;
            font-weight: 700;
            line-height: 1.35;
        }

        .weather-icon {
            display: inline-grid;
            place-items: center;
            width: 28px;
            height: 28px;
            border-radius: 9px;
            background: rgba(42, 104, 190, 0.33);
            border: 1px solid rgba(128, 196, 255, 0.36);
            font-size: 15px;
            flex: 0 0 28px;
        }

        .weather-badges {
            margin-top: 9px;
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        }

        .weather-badge {
            display: inline-flex;
            align-items: center;
            min-height: 25px;
            max-width: 100%;
            padding: 4px 8px;
            border-radius: 999px;
            border: 1px solid rgba(125, 174, 232, 0.35);
            background: rgba(15, 28, 48, 0.84);
            color: #bcd4f2;
            font-size: 12px;
            font-weight: 600;
        }

        @media (max-width: 1020px) {
            .page { grid-template-columns: 1fr; }
            .aside-card { order: -1; }
            .aside-body { margin-top: -34px; }
            .cta-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }

        @media (max-width: 720px) {
            .page {
                width: min(100% - 20px, 1160px);
                margin-top: 14px;
                gap: 14px;
            }

            .hero { padding: 16px 14px 14px; }
            .headline { font-size: 30px; }
            .role { font-size: 16px; }
            .bio {
                margin-top: 12px;
                font-size: 16px;
                line-height: 1.48;
            }

            .cta-grid { grid-template-columns: 1fr; }
            .cta-link { min-height: 54px; }
            .mini-link { min-height: 44px; font-size: 14px; }

            .telegram-strip { padding: 8px; gap: 10px; }
            .telegram-strip .plane {
                width: 42px;
                height: 42px;
                font-size: 21px;
                border-radius: 12px;
            }
            .telegram-strip .tg-url { font-size: 18px; }

            .quote-header { padding: 14px 14px 10px; }
            .quote-body {
                padding: 14px;
                font-size: 15px;
            }

            .aside-banner { height: 100px; }
            .avatar {
                width: 76px;
                height: 76px;
            }
            .handle { font-size: 34px; }
            .small-title { font-size: 15px; }
            .status-value { font-size: 14px; }
            .music-action { min-height: 28px; font-size: 11px; padding: 0 8px; }
            .weather-main { gap: 7px; }
            .weather-icon { width: 24px; height: 24px; font-size: 13px; border-radius: 8px; }
            .weather-badge { font-size: 11px; padding: 3px 7px; }
        }
    </style>
</head>
<body>
<main class="page">
    <section class="main-column">
        <article class="panel hero">
            <h1 class="headline">Привет! Я <span><?= escapeHtml($profile['name']) ?></span></h1>
            <p class="role"><?= escapeHtml($profile['title']) ?></p>
            <p class="bio"><?= nl2br(escapeHtml($profile['bio'])) ?></p>

            <div class="stack-row">
                <?php foreach ($profile['stack'] as $tech): ?>
                    <span class="chip"><?= escapeHtml($tech) ?></span>
                <?php endforeach; ?>
            </div>

            <div class="cta-grid">
                <?php foreach ($mainLinks as $idx => $link): ?>
                    <?php $linkUrl = toStringSafe($link['url'] ?? ''); ?>
                    <?php $linkLabel = toStringSafe($link['label'] ?? 'Ссылка'); ?>
                    <?php if ($linkUrl === '') { continue; } ?>
                    <a class="cta-link" href="<?= escapeHtml($linkUrl) ?>" target="_blank" rel="noopener noreferrer">
                        <span class="cta-icon"><?= $idx + 1 ?></span>
                        <span class="cta-text"><?= escapeHtml($linkLabel) ?></span>
                    </a>
                <?php endforeach; ?>
            </div>

            <a class="mini-link projects-link" href="<?= escapeHtml($projectsPageUrl) ?>">
                ↗ Проекты
            </a>

            <?php if (!empty($moreLinks)): ?>
                <div class="more-links">
                    <?php foreach ($moreLinks as $link): ?>
                        <?php $linkUrl = toStringSafe($link['url'] ?? ''); ?>
                        <?php $linkLabel = toStringSafe($link['label'] ?? 'Ссылка'); ?>
                        <?php if ($linkUrl === '') { continue; } ?>
                        <a class="mini-link" href="<?= escapeHtml($linkUrl) ?>" target="_blank" rel="noopener noreferrer">
                            ↗ <?= escapeHtml($linkLabel) ?>
                        </a>
                    <?php endforeach; ?>
                </div>
            <?php endif; ?>

            <a class="telegram-strip" href="<?= escapeHtml($profile['telegram_url']) ?>" target="_blank" rel="noopener noreferrer">
                <span class="plane">✈</span>
                <strong class="tg-url"><?= escapeHtml($telegramLabel) ?></strong>
            </a>

            <section class="panel quote-panel">
                <header class="quote-header">✦ Цитата</header>
                <blockquote class="quote-body"><?= nl2br(escapeHtml($profile['quote'])) ?></blockquote>
            </section>
        </article>
    </section>

    <aside class="panel aside-card">
        <div class="aside-banner"></div>
        <div class="aside-body">
            <?php if ($profile['avatar_url'] !== ''): ?>
                <img class="avatar" src="<?= escapeHtml($profile['avatar_url']) ?>" alt="avatar">
            <?php else: ?>
                <div class="avatar" aria-hidden="true"></div>
            <?php endif; ?>

            <div class="muted-id">«<?= escapeHtml($profile['name']) ?>»</div>
            <h2 class="handle">@<?= escapeHtml($displayUsername !== '' ? $displayUsername : 'username') ?></h2>
            <div class="small-title"><?= escapeHtml($profile['title']) ?></div>

            <div class="status-grid">
                <section class="status-row">
                    <div class="status-label">Telegram</div>
                    <div class="status-value">
                        <a href="<?= escapeHtml($profile['telegram_url']) ?>" target="_blank" rel="noopener noreferrer">
                            <?= escapeHtml($profile['telegram_url']) ?>
                        </a>
                    </div>
                </section>
                <section class="status-row">
                    <div class="status-label">Сейчас играет</div>
                    <div class="status-value"><?= escapeHtml($nowListeningText !== '' ? $nowListeningText : 'Сейчас ничего не играет') ?></div>
                    <div class="music-actions">
                        <?php if ($canSearchTrack): ?>
                            <?php foreach ($trackSearchLinks as $label => $url): ?>
                                <a class="music-action" href="<?= escapeHtml($url) ?>" target="_blank" rel="noopener noreferrer">
                                    <?= escapeHtml($label) ?>
                                </a>
                            <?php endforeach; ?>
                        <?php else: ?>
                            <span class="music-action disabled">Трек не найден</span>
                        <?php endif; ?>
                    </div>
                </section>
                <section class="status-row weather-row">
                    <div class="status-label"><?= escapeHtml($weatherLabel) ?></div>
                    <div class="status-value weather-main">
                        <span class="weather-icon"><?= escapeHtml($weatherIcon) ?></span>
                        <span><?= escapeHtml($weatherMainLine) ?></span>
                    </div>
                    <?php if (!empty($weatherDetails)): ?>
                        <div class="weather-badges">
                            <?php foreach ($weatherDetails as $detail): ?>
                                <span class="weather-badge"><?= escapeHtml($detail) ?></span>
                            <?php endforeach; ?>
                        </div>
                    <?php endif; ?>
                </section>
            </div>
        </div>
    </aside>
</main>
</body>
</html>
