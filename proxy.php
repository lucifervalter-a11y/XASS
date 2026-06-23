<?php
declare(strict_types=1);

// Transparent PHP reverse proxy to the FastAPI backend.
// miniapp.php calls this instead of /api/mini/* directly,
// so that no nginx config changes are needed.

$BACKEND = 'http://127.0.0.1:8000';

$rawPath = $_GET['_p'] ?? '';
$rawPath = '/' . ltrim(urldecode($rawPath), '/');

// Restrict to /api/ only to prevent SSRF abuse.
if (!str_starts_with($rawPath, '/api/')) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['ok' => false, 'detail' => 'invalid proxy path']);
    exit;
}

$method = $_SERVER['REQUEST_METHOD'] ?? 'GET';
$url = $BACKEND . $rawPath;

$body = null;
if (in_array($method, ['POST', 'PUT', 'PATCH', 'DELETE'], true)) {
    $body = file_get_contents('php://input');
}

$forwardHeaders = [];
foreach (getallheaders() as $name => $val) {
    $lower = strtolower($name);
    if (in_array($lower, ['content-type', 'x-telegram-init-data', 'x-api-key', 'authorization'], true)) {
        $forwardHeaders[] = $name . ': ' . $val;
    }
}

$ch = curl_init($url);
curl_setopt_array($ch, [
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_CUSTOMREQUEST  => $method,
    CURLOPT_HTTPHEADER     => $forwardHeaders,
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
    header('Content-Type: application/json');
    echo json_encode(['ok' => false, 'detail' => 'Бэкенд недоступен: ' . ($curlErr ?: 'нет ответа')]);
    exit;
}

http_response_code($httpCode);
header('Content-Type: application/json');
echo $responseBody;
