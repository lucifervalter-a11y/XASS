<?php
declare(strict_types=1);

// Transparent PHP reverse proxy to the FastAPI backend.
// Compatible with PHP 7.1+. No nginx config changes needed.

$BACKEND = 'http://127.0.0.1:8000';

header('Content-Type: application/json');

$rawPath = isset($_GET['_p']) ? $_GET['_p'] : '';
$rawPath = '/' . ltrim(rawurldecode($rawPath), '/');

// Restrict to /api/ only to prevent SSRF abuse.
if (strpos($rawPath, '/api/') !== 0) {
    http_response_code(400);
    echo json_encode(['ok' => false, 'detail' => 'invalid proxy path']);
    exit;
}

$method = isset($_SERVER['REQUEST_METHOD']) ? $_SERVER['REQUEST_METHOD'] : 'GET';
$url = $BACKEND . $rawPath;

$body = null;
if (in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
    $body = file_get_contents('php://input');
}

// Collect headers to forward — works with both Apache mod_php and PHP-FPM.
$forward = [];
$allowed = ['content-type', 'x-telegram-init-data', 'x-api-key', 'authorization'];

if (function_exists('getallheaders')) {
    foreach (getallheaders() as $name => $val) {
        if (in_array(strtolower($name), $allowed, true)) {
            $forward[] = $name . ': ' . $val;
        }
    }
} else {
    // PHP-FPM fallback: headers arrive as HTTP_* in $_SERVER.
    $map = [
        'HTTP_CONTENT_TYPE'          => 'Content-Type',
        'HTTP_X_TELEGRAM_INIT_DATA'  => 'X-Telegram-Init-Data',
        'HTTP_X_API_KEY'             => 'X-Api-Key',
        'HTTP_AUTHORIZATION'         => 'Authorization',
    ];
    foreach ($map as $key => $headerName) {
        if (!empty($_SERVER[$key])) {
            $forward[] = $headerName . ': ' . $_SERVER[$key];
        }
    }
}

// CONTENT_TYPE is set directly in PHP-FPM, not as HTTP_CONTENT_TYPE
if (empty($forward) || !array_filter($forward, function($h) { return strpos(strtolower($h), 'content-type:') === 0; })) {
    if (!empty($_SERVER['CONTENT_TYPE'])) {
        $forward[] = 'Content-Type: ' . $_SERVER['CONTENT_TYPE'];
    }
}

// Also grab X-Telegram-Init-Data from $_SERVER directly if missed
$tgHeader = '';
if (!empty($_SERVER['HTTP_X_TELEGRAM_INIT_DATA'])) {
    $tgHeader = $_SERVER['HTTP_X_TELEGRAM_INIT_DATA'];
} elseif (!empty($_SERVER['HTTP_X-TELEGRAM-INIT-DATA'])) {
    $tgHeader = $_SERVER['HTTP_X-TELEGRAM-INIT-DATA'];
}
if ($tgHeader && !array_filter($forward, function($h) { return stripos($h, 'x-telegram-init-data:') === 0; })) {
    $forward[] = 'X-Telegram-Init-Data: ' . $tgHeader;
}

if (!function_exists('curl_init')) {
    http_response_code(500);
    echo json_encode(['ok' => false, 'detail' => 'curl extension not available on server']);
    exit;
}

$ch = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_CUSTOMREQUEST  => $method,
    CURLOPT_HTTPHEADER     => $forward,
    CURLOPT_TIMEOUT        => 90,
    CURLOPT_CONNECTTIMEOUT => 5,
]);
if ($body !== null && $body !== '') {
    curl_setopt($ch, CURLOPT_POSTFIELDS, $body);
}

$responseBody = curl_exec($ch);
$httpCode = (int) curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curlErr  = curl_error($ch);
curl_close($ch);

if ($curlErr || $responseBody === false) {
    http_response_code(502);
    echo json_encode(['ok' => false, 'detail' => 'Backend unavailable: ' . ($curlErr ?: 'no response')]);
    exit;
}

http_response_code($httpCode);
echo $responseBody;
