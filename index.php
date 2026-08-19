<?php
declare(strict_types=1);

// nginx serves the public PHP site and falls back to this front controller.
// Keep the agent API on the same HTTPS origin without exposing FastAPI's port
// or duplicating credentials in a second URL. Binary update routes are streamed
// by proxy.php, while JSON endpoints preserve their real HTTP status/body.
$requestPath = parse_url((string)($_SERVER['REQUEST_URI'] ?? '/'), PHP_URL_PATH);
$requestPath = is_string($requestPath) ? rawurldecode($requestPath) : '/';
if ($requestPath === '/health' || str_starts_with($requestPath, '/agent/')) {
    $_GET['_p'] = $requestPath;
    if (
        str_starts_with($requestPath, '/agent/update/package') ||
        str_starts_with($requestPath, '/agent/installer/') ||
        str_starts_with($requestPath, '/agent/migration/export/')
    ) {
        $_GET['_binary'] = '1';
    } else {
        $_GET['_passthrough'] = '1';
    }
    require __DIR__ . '/proxy.php';
    exit;
}

require __DIR__ . '/profile.php';
