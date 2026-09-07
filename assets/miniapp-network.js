/* Shared transport for Telegram and standalone XASS. No authentication in caches. */
(function (root) {
  'use strict';

  function errorMessage(status) {
    if (status === 401) return 'Сеанс истёк. Войдите снова или откройте новую ссылку из Telegram.';
    if (status === 403) return 'Для этого действия нужен аккаунт владельца XASS.';
    if (status === 429) return 'Слишком много запросов. Подождите несколько секунд.';
    if (status >= 500) return 'Сервер XASS временно недоступен. Попробуйте ещё раз — настройки входа менять не нужно.';
    return 'Сервер вернул неожиданный ответ. Повторите запрос.';
  }

  async function json(url, options = {}) {
    const {timeoutMs = 15000, envelope = false, ...requestOptions} = options;
    const controller = new AbortController();
    const external = requestOptions.signal;
    const cancel = () => controller.abort();
    if (external?.aborted) cancel();
    external?.addEventListener('abort', cancel, {once: true});
    const timer = setTimeout(cancel, timeoutMs);
    try {
      const response = await fetch(url, {...requestOptions, signal: controller.signal, cache: 'no-store'});
      const raw = await response.text();
      let parsed;
      try { parsed = JSON.parse(raw); } catch (_) { parsed = null; }
      let status = Number(response.headers.get('X-XASS-Status') || response.status);
      let data = parsed;
      if (envelope && parsed && Object.prototype.hasOwnProperty.call(parsed, '_s')) {
        status = Number(parsed._s) || response.status;
        try { data = JSON.parse(parsed._b || ''); } catch (_) { data = null; }
      }
      if (!data || typeof data !== 'object' || (status >= 500)) {
        data = {ok: false, detail: errorMessage(status)};
      }
      return {status, data, raw: envelope ? parsed?._b || '' : raw};
    } catch (error) {
      if (external?.aborted) throw error;
      if (controller.signal.aborted) throw new Error('Сервер не ответил вовремя. Проверьте связь и повторите запрос.');
      if (error instanceof TypeError) throw new Error('Нет связи с XASS. Проверьте интернет и доступность сервера.');
      throw error;
    } finally {
      clearTimeout(timer);
      external?.removeEventListener('abort', cancel);
    }
  }

  root.XassHttp = {json, errorMessage};
  if (typeof module !== 'undefined' && module.exports) module.exports = root.XassHttp;
})(typeof window !== 'undefined' ? window : globalThis);
