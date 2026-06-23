<?php
declare(strict_types=1);

// Reverse proxy to FastAPI backend.
// Always returns HTTP 200 so nginx does not intercept the response.
// The real HTTP status is carried in the _s field of the JSON envelope.

$BACKEND = 'http://127.0.0.1:8000';

// Output headers first — no output before this point.
header('Content-Type: application/json; charset=utf-8');
http_response_code(200);

function proxy_error(int $status, string $detail): void {
    echo json_encode(['_s' => $status, '_b' => json_encode(['ok' => false, 'detail' => $detail])]);
    exit;
}

$rawPath = isset($_GET['_p']) ? (string)$_GET['_p'] : '';
$rawPath = '/' . ltrim(rawurldecode($rawPath), '/');

if (strpos($rawPath, '/api/') !== 0) {
    proxy_error(400, 'invalid proxy path');
}

if (!function_exists('curl_init')) {
    proxy_error(500, 'curl extension not available');
}

$method = isset($_SERVER['REQUEST_METHOD']) ? (string)$_SERVER['REQUEST_METHOD'] : 'GET';
$url    = $BACKEND . $rawPath;

$body = null;
if (in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
    $body = (string)file_get_contents('php://input');
}

// Forward selected headers. Works with both Apache mod_php and PHP-FPM.
$forward = [];

if (function_exists('getallheaders')) {
    $allowed = ['content-type', 'x-telegram-init-data', 'x-api-key', 'authorization'];
    foreach (getallheaders() as $name => $val) {
        if (in_array(strtolower((string)$name), $allowed, true)) {
            $forward[] = $name . ': ' . $val;
        }
    }
}

// PHP-FPM fallback: headers as HTTP_* in $_SERVER.
$headerMap = [
    'HTTP_X_TELEGRAM_INIT_DATA' => 'X-Telegram-Init-Data',
    'HTTP_X_API_KEY'            => 'X-Api-Key',
    'HTTP_AUTHORIZATION'        => 'Authorization',
    'HTTP_CONTENT_TYPE'         => 'Content-Type',
    'CONTENT_TYPE'              => 'Content-Type',
];
foreach ($headerMap as $serverKey => $headerName) {
    if (!empty($_SERVER[$serverKey])) {
        $lower = strtolower($headerName);
        $alreadySet = false;
        foreach ($forward as $h) {
            if (strpos(strtolower($h), $lower . ':') === 0) { $alreadySet = true; break; }
        }
        if (!$alreadySet) {
            $forward[] = $headerName . ': ' . $_SERVER[$serverKey];
        }
    }
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
$httpCode     = (int)curl_getinfo($ch, CURLINFO_HTTP_CODE);
$curlErr      = (string)curl_error($ch);
curl_close($ch);

if ($curlErr || $responseBody === false || $responseBody === '') {
    proxy_error(502, 'Backend unavailable: ' . ($curlErr ?: 'empty response'));
}

// Wrap: actual status in _s, raw body string in _b.
echo json_encode(['_s' => $httpCode, '_b' => (string)$responseBody]);
