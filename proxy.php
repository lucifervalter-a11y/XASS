<?php
declare(strict_types=1);

// Reverse proxy to FastAPI backend using file_get_contents (no curl needed).
// Always returns HTTP 200 so nginx does not intercept the response.
// Real HTTP status is in the _s field of the JSON envelope.

$BACKEND = 'http://127.0.0.1:8000';

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

$method = isset($_SERVER['REQUEST_METHOD']) ? strtoupper((string)$_SERVER['REQUEST_METHOD']) : 'GET';
$url    = $BACKEND . $rawPath;

$body = '';
if (in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
    $body = (string)file_get_contents('php://input');
}

// Collect headers to forward.
$forwardHeaders = [];

if (function_exists('getallheaders')) {
    $allowed = ['content-type', 'x-telegram-init-data', 'x-api-key', 'authorization'];
    foreach (getallheaders() as $name => $val) {
        if (in_array(strtolower((string)$name), $allowed, true)) {
            $forwardHeaders[] = $name . ': ' . $val;
        }
    }
}

// PHP-FPM fallback via $_SERVER.
$serverMap = [
    'HTTP_X_TELEGRAM_INIT_DATA' => 'X-Telegram-Init-Data',
    'HTTP_X_API_KEY'            => 'X-Api-Key',
    'HTTP_AUTHORIZATION'        => 'Authorization',
    'HTTP_CONTENT_TYPE'         => 'Content-Type',
    'CONTENT_TYPE'              => 'Content-Type',
];
foreach ($serverMap as $key => $headerName) {
    if (!empty($_SERVER[$key])) {
        $lower = strtolower($headerName) . ':';
        $already = false;
        foreach ($forwardHeaders as $h) {
            if (strpos(strtolower($h), $lower) === 0) { $already = true; break; }
        }
        if (!$already) {
            $forwardHeaders[] = $headerName . ': ' . $_SERVER[$key];
        }
    }
}

// Build stream context for file_get_contents.
$opts = [
    'http' => [
        'method'        => $method,
        'header'        => implode("\r\n", $forwardHeaders),
        'content'       => $body,
        'timeout'       => 90,
        'ignore_errors' => true,   // return body even on 4xx/5xx
    ],
];

$context      = stream_context_create($opts);
$responseBody = @file_get_contents($url, false, $context);

if ($responseBody === false) {
    proxy_error(502, 'Backend unavailable: could not connect to ' . $url);
}

// $http_response_header is set by file_get_contents after a successful call.
$httpCode = 200;
if (!empty($http_response_header)) {
    // First line: "HTTP/1.1 200 OK"
    if (preg_match('#HTTP/\S+\s+(\d+)#', $http_response_header[0], $m)) {
        $httpCode = (int)$m[1];
    }
}

echo json_encode(['_s' => $httpCode, '_b' => (string)$responseBody]);
