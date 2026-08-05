<?php
declare(strict_types=1);

// Reverse proxy to FastAPI backend using PHP streams (no curl needed).
// Always returns HTTP 200 so nginx does not intercept the response.
// Real HTTP status is in the _s field of the JSON envelope.

$BACKEND = 'http://127.0.0.1:8000';

$binaryMode = isset($_GET['_binary']) && (string)$_GET['_binary'] === '1';
$passthroughMode = isset($_GET['_passthrough']) && (string)$_GET['_passthrough'] === '1';
if (!$binaryMode && !$passthroughMode) {
    header('Content-Type: application/json; charset=utf-8');
}
http_response_code(200);

function proxy_error(int $status, string $detail): void {
    global $binaryMode, $passthroughMode;
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
    $allowed = ['content-type', 'x-telegram-init-data', 'x-api-key', 'authorization', 'cookie'];
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
    'HTTP_COOKIE'               => 'Cookie',
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

// Build stream context for the backend request.
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

// Installers can be tens of megabytes. Stream them chunk-by-chunk so PHP and
// the Telegram WebView do not have to buffer the full executable in memory.
if ($binaryMode) {
    $responseStream = @fopen($url, 'rb', false, $context);
    if ($responseStream === false) {
        proxy_error(502, 'Backend unavailable: could not connect to ' . $url);
    }
    $responseHeaders = $http_response_header ?? [];
    $httpCode = 200;
    $contentType = 'application/octet-stream';
    $contentDisposition = 'attachment; filename="XASS-Setup.exe"';
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
        }
    }
    header('Content-Type: ' . $contentType);
    header('Content-Disposition: ' . $contentDisposition);
    if ($contentLength !== '') {
        header('Content-Length: ' . $contentLength);
    }
    header('Cache-Control: private, no-store');
    header('X-XASS-Status: ' . $httpCode);
    while (!feof($responseStream)) {
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

// $http_response_header is set by file_get_contents after a successful call.
$httpCode = 200;
if (!empty($http_response_header)) {
    // First line: "HTTP/1.1 200 OK"
    if (preg_match('#HTTP/\S+\s+(\d+)#', $http_response_header[0], $m)) {
        $httpCode = (int)$m[1];
    }
}

if ($passthroughMode) {
    $contentType = 'application/json; charset=utf-8';
    foreach ($http_response_header as $headerLine) {
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
