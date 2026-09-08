<?php
declare(strict_types=1);

// Reverse proxy to FastAPI backend using PHP streams (no curl needed).
// Always returns HTTP 200 so nginx does not intercept the response.
// Real HTTP status is in the _s field of the JSON envelope.

$BACKEND = 'http://127.0.0.1:8000';

$binaryMode = isset($_GET['_binary']) && (string)$_GET['_binary'] === '1';
$mediaMode = $binaryMode && isset($_GET['_media']) && (string)$_GET['_media'] === '1';
$passthroughMode = isset($_GET['_passthrough']) && (string)$_GET['_passthrough'] === '1';
header('Cache-Control: private, no-store');
if (!$binaryMode && !$passthroughMode) {
    header('Content-Type: application/json; charset=utf-8');
}
http_response_code(200);

function proxy_error(int $status, string $detail): void {
    global $binaryMode, $passthroughMode, $mediaMode;
    if ($mediaMode) {
        http_response_code($status);
    }
    if ($binaryMode || $passthroughMode) {
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: private, no-store');
        header('X-XASS-Status: ' . $status);
        echo json_encode(['ok' => false, 'detail' => $detail]);
        exit;
    }
    echo json_encode(['_s' => $status, '_b' => json_encode(['ok' => false, 'detail' => $detail])]);
    exit;
}

$rawPath = isset($_GET['_p']) ? (string)$_GET['_p'] : '';
$rawPath = '/' . ltrim(rawurldecode($rawPath), '/');
// Reject path traversal and NUL before prefix checks. Otherwise `/api/../docs`
// would pass the `/api/` allow-list and reach FastAPI's OpenAPI UI.
if (
    strpos($rawPath, '..') !== false
    || strpos($rawPath, chr(0)) !== false
    || strpos($rawPath, chr(92)) !== false
) {
    proxy_error(400, 'invalid proxy path');
}
$rawPath = preg_replace('#/+#', '/', $rawPath) ?? $rawPath;

if ($rawPath !== '/health' && strpos($rawPath, '/api/') !== 0 && strpos($rawPath, '/agent/') !== 0) {
    proxy_error(400, 'invalid proxy path');
}
// Only audio may opt out of the installer-compatible HTTP-200 envelope.
// Safari requires real 206/416 responses and Content-Range for seeking.
if ($mediaMode && preg_match('#^/(?:api|agent)/music/tracks/[1-9][0-9]*/stream(?:\?[^\r\n]*)?$#D', $rawPath) !== 1) {
    proxy_error(400, 'invalid media path');
}

$method = isset($_SERVER['REQUEST_METHOD']) ? strtoupper((string)$_SERVER['REQUEST_METHOD']) : 'GET';
$url    = $BACKEND . $rawPath;

$body = '';
if (in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
    $body = (string)file_get_contents('php://input');
}

// Collect headers to forward.
$forwardHeaders = [];

// Preserve the public origin for PWA readiness diagnostics. The backend is
// reached over localhost, so without these headers it cannot know that the
// user actually opened the HTTPS domain.
$publicHost = isset($_SERVER['HTTP_HOST']) ? trim((string)$_SERVER['HTTP_HOST']) : '';
if ($publicHost !== '' && preg_match('/^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$/', $publicHost) === 1) {
    $forwardHeaders[] = 'X-Forwarded-Host: ' . $publicHost;
}
$publicProto = (!empty($_SERVER['HTTPS']) && strtolower((string)$_SERVER['HTTPS']) !== 'off') ? 'https' : 'http';
if (!empty($_SERVER['HTTP_X_FORWARDED_PROTO'])) {
    $candidateProto = strtolower(trim(explode(',', (string)$_SERVER['HTTP_X_FORWARDED_PROTO'])[0]));
    if (in_array($candidateProto, ['http', 'https'], true)) {
        $publicProto = $candidateProto;
    }
}
$forwardHeaders[] = 'X-Forwarded-Proto: ' . $publicProto;

if (function_exists('getallheaders')) {
    $allowed = ['content-type', 'x-telegram-init-data', 'x-xass-action-proof', 'x-api-key', 'authorization', 'cookie'];
    if ($mediaMode) {
        $allowed = array_merge($allowed, ['range', 'if-range']);
    }
    foreach (getallheaders() as $name => $val) {
        if (in_array(strtolower((string)$name), $allowed, true)) {
            $forwardHeaders[] = $name . ': ' . $val;
        }
    }
}

// PHP-FPM fallback via $_SERVER.
$serverMap = [
    'HTTP_X_TELEGRAM_INIT_DATA' => 'X-Telegram-Init-Data',
    'HTTP_X_XASS_ACTION_PROOF'  => 'X-XASS-Action-Proof',
    'HTTP_X_API_KEY'            => 'X-Api-Key',
    'HTTP_AUTHORIZATION'        => 'Authorization',
    'HTTP_COOKIE'               => 'Cookie',
    'HTTP_CONTENT_TYPE'         => 'Content-Type',
    'CONTENT_TYPE'              => 'Content-Type',
];
if ($mediaMode) {
    $serverMap['HTTP_RANGE'] = 'Range';
    $serverMap['HTTP_IF_RANGE'] = 'If-Range';
}
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

// Build stream context for the backend request.
$opts = [
    'http' => [
        'method'        => $method,
        'header'        => implode("\r\n", $forwardHeaders),
        'content'       => $body,
        'timeout'       => 90,
        'ignore_errors' => true,   // return body even on 4xx/5xx
        'follow_location' => 0,
    ],
];

$context      = stream_context_create($opts);

// Installers can be tens of megabytes. Stream them chunk-by-chunk so PHP and
// the Telegram WebView do not have to buffer the full executable in memory.
if ($binaryMode) {
    // Large migration exports may take several minutes. Keep streaming as long
    // as the client remains connected instead of inheriting PHP's short limit.
    @set_time_limit(0);
    ignore_user_abort(!$mediaMode);
    $responseStream = @fopen($url, 'rb', false, $context);
    if ($responseStream === false) {
        proxy_error(502, 'Backend unavailable: could not connect to ' . $url);
    }
    $responseHeaders = (function_exists('http_get_last_response_headers')
        ? http_get_last_response_headers() : (${'http_response_header'} ?? [])) ?: [];
    $httpCode = 200;
    $contentType = 'application/octet-stream';
    $contentDisposition = $mediaMode ? 'inline' : 'attachment; filename="XASS-Setup.exe"';
    $contentLength = '';
    if (!empty($responseHeaders) && preg_match('#HTTP/\S+\s+(\d+)#', $responseHeaders[0], $m)) {
        $httpCode = (int)$m[1];
    }
    foreach ($responseHeaders as $headerLine) {
        if (stripos($headerLine, 'Content-Type:') === 0) {
            $contentType = trim(substr($headerLine, strlen('Content-Type:')));
        } elseif (stripos($headerLine, 'Content-Disposition:') === 0) {
            $contentDisposition = trim(substr($headerLine, strlen('Content-Disposition:')));
        } elseif (stripos($headerLine, 'Content-Length:') === 0) {
            $contentLength = trim(substr($headerLine, strlen('Content-Length:')));
        } elseif ($mediaMode && preg_match('/^(?:Content-Range|Accept-Ranges|ETag|Last-Modified):/i', $headerLine)) {
            header($headerLine);
        }
    }
    if ($mediaMode) {
        http_response_code($httpCode);
        header('X-Accel-Buffering: no');
        header('X-Content-Type-Options: nosniff');
        header('Referrer-Policy: no-referrer');
    }
    header('Content-Type: ' . $contentType);
    header('Content-Disposition: ' . $contentDisposition);
    if ($contentLength !== '') {
        header('Content-Length: ' . $contentLength);
    }
    header('Cache-Control: private, no-store');
    header('X-XASS-Status: ' . $httpCode);
    while ($method !== 'HEAD' && !feof($responseStream)) {
        $chunk = fread($responseStream, 1024 * 1024);
        if ($chunk === false) {
            break;
        }
        echo $chunk;
        if (ob_get_level() > 0) {
            @ob_flush();
        }
        flush();
    }
    fclose($responseStream);
    exit;
}

$responseBody = @file_get_contents($url, false, $context);

if ($responseBody === false) {
    proxy_error(502, 'Backend unavailable: could not connect to ' . $url);
}

// PHP 8.4+ exposes the headers without the deprecated scoped variable.
$responseHeaders = (function_exists('http_get_last_response_headers')
    ? http_get_last_response_headers() : (${'http_response_header'} ?? [])) ?: [];
$httpCode = 200;
if (!empty($responseHeaders)) {
    // First line: "HTTP/1.1 200 OK"
    if (preg_match('#HTTP/\S+\s+(\d+)#', $responseHeaders[0], $m)) {
        $httpCode = (int)$m[1];
    }
}

// Native enrollment uses the ordinary JSON envelope, but its HttpOnly owner
// cookie must still reach the same-origin client. Never put it in JSON/JS.
if (!$passthroughMode) {
    foreach ($responseHeaders as $headerLine) {
        if (preg_match('/^(?i:Set-Cookie):\\s*xass_pwa=/', $headerLine) === 1) {
            header($headerLine, false);
        }
    }
}

if ($passthroughMode) {
    $contentType = 'application/json; charset=utf-8';
    foreach ($responseHeaders as $headerLine) {
        if (stripos($headerLine, 'Content-Type:') === 0) {
            $contentType = trim(substr($headerLine, strlen('Content-Type:')));
        } elseif (stripos($headerLine, 'Set-Cookie:') === 0) {
            header($headerLine, false);
        }
    }
    header('Content-Type: ' . $contentType);
    header('Cache-Control: private, no-store');
    header('X-XASS-Status: ' . $httpCode);
    http_response_code($httpCode);
    echo (string)$responseBody;
    exit;
}

echo json_encode(['_s' => $httpCode, '_b' => (string)$responseBody]);
