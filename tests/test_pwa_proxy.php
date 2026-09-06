<?php
declare(strict_types=1);

// Run in an isolated PHP process. The HTTP stream is a local capture stub;
// no production backend, login or server update is contacted.
$testCase = getenv('XASS_PROXY_TEST_CASE') ?: 'headers';
$_GET = ['_p' => '/api/mini/run-update'];
$_SERVER = ['REQUEST_METHOD' => 'POST', 'HTTP_HOST' => 'xass.example', 'HTTPS' => 'on'];
if ($testCase === 'headers') {
    function getallheaders(): array {
        return [
            'x-xass-action-proof' => 'signed-test-proof',
            'Cookie' => 'xass_pwa=test-session',
            'Content-Type' => 'application/json',
            'X-Unrelated-Header' => 'must-not-be-forwarded',
        ];
    }
    // Also present in $_SERVER: the proxy must not duplicate the header.
    $_SERVER['HTTP_X_XASS_ACTION_PROOF'] = 'signed-test-proof';
} elseif ($testCase === 'fallback') {
    $_SERVER['HTTP_X_XASS_ACTION_PROOF'] = 'signed-test-proof';
    $_SERVER['HTTP_COOKIE'] = 'xass_pwa=test-session';
    $_SERVER['CONTENT_TYPE'] = 'application/json';
} else {
    throw new RuntimeException('Unknown test case');
}

class CaptureBackendStream {
    public $context;
    private string $body = '';
    private int $offset = 0;

    public function stream_open(string $path, string $mode, int $options, ?string &$openedPath): bool {
        $request = stream_context_get_options($this->context)['http'];
        $this->body = json_encode(['url' => $path, 'method' => $request['method'], 'headers' => $request['header']], JSON_THROW_ON_ERROR);
        return true;
    }

    public function stream_read(int $count): string {
        $result = substr($this->body, $this->offset, $count);
        $this->offset += strlen($result);
        return $result;
    }

    public function stream_eof(): bool { return $this->offset >= strlen($this->body); }
    public function stream_stat(): array { return []; }
}

stream_wrapper_unregister('http');
stream_wrapper_register('http', CaptureBackendStream::class);
ob_start();
require dirname(__DIR__) . '/proxy.php';
$envelope = json_decode((string)ob_get_clean(), true, 512, JSON_THROW_ON_ERROR);
$received = json_decode($envelope['_b'], true, 512, JSON_THROW_ON_ERROR);
$headers = strtolower($received['headers']);
if (
    $envelope['_s'] !== 200
    || $received['method'] !== 'POST'
    || $received['url'] !== 'http://127.0.0.1:8000/api/mini/run-update'
    || substr_count($headers, "x-xass-action-proof: signed-test-proof") !== 1
    || strpos($headers, 'cookie: xass_pwa=test-session') === false
    || strpos($headers, 'x-forwarded-host: xass.example') === false
    || strpos($headers, 'x-forwarded-proto: https') === false
    || strpos($headers, 'x-unrelated-header') !== false
) {
    throw new RuntimeException('Proxy did not preserve the authenticated action request');
}
echo "OK: proxy $testCase\n";
