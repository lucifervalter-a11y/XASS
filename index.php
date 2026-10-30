<?php
declare(strict_types=1);

// nginx serves the public PHP site and falls back to this front controller.
// Keep the agent API on the same HTTPS origin without exposing FastAPI's port
// or duplicating credentials in a second URL. Binary update routes are streamed
// by proxy.php, while JSON endpoints preserve their real HTTP status/body.
$requestPath = parse_url((string)($_SERVER['REQUEST_URI'] ?? '/'), PHP_URL_PATH);
$requestPath = is_string($requestPath) ? rawurldecode($requestPath) : '/';
// Discord must see the exact JSON response and real 401, not the Mini App
// envelope or profile HTML. Body bytes are left untouched for Ed25519.
if ($requestPath === '/api/discord/interactions' ||
    preg_match('#^/api/discord/artwork/[1-9][0-9]*/[a-f0-9]{64}\.jpg$#D', $requestPath) === 1) {
    $_GET['_p'] = $requestPath;
    unset($_GET['_binary'], $_GET['_media']);
    $_GET['_passthrough'] = '1';
    require __DIR__ . '/proxy.php';
    exit;
}
if ($requestPath === '/health' || str_starts_with($requestPath, '/agent/')) {
    $_GET['_p'] = $requestPath;
    if (preg_match('#^/agent/music/tracks/[1-9][0-9]*/stream$#D', $requestPath) === 1) {
        $_GET['_p'] .= '?' . http_build_query(['ticket' => is_string($_GET['ticket'] ?? null) ? $_GET['ticket'] : '']);
        $_GET['_binary'] = '1';
        $_GET['_media'] = '1';
    } elseif (
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
