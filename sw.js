const CACHE = 'xass-shell-v8';
const OFFLINE = '/offline.html';
const SHELL = [
  OFFLINE,
  '/manifest.webmanifest',
  '/assets/miniapp-control-center.css?v=0130',
  '/assets/miniapp-control-center.js?v=0130',
  '/assets/xass-app-icon-96.png',
  '/assets/xass-app-icon-144.png',
  '/assets/xass-app-icon-180.png',
  '/assets/xass-app-icon-192.png',
  '/assets/xass-app-icon-512.png'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(key => key.startsWith('xass-shell-') && key !== CACHE).map(key => caches.delete(key)));
    await self.clients.claim();
    const clients = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    for (const client of clients) client.postMessage({type: 'XASS_SW_UPDATED', cache: CACHE});
  })());
});

self.addEventListener('message', event => {
  if (event.data?.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('push', event => {
  let payload = {};
  try { payload = event.data?.json() || {}; } catch (error) { payload = {body: event.data?.text() || ''}; }
  event.waitUntil(self.registration.showNotification(payload.title || 'XASS', {
    body: payload.body || 'Новое событие XASS',
    icon: '/assets/xass-app-icon-192.png',
    badge: '/assets/xass-app-icon-96.png',
    tag: payload.event_type || 'xass-notification',
    renotify: payload.priority === 'critical',
    data: {url: payload.url || '/miniapp.php?standalone=1#notifications'}
  }));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = new URL(event.notification.data?.url || '/miniapp.php?standalone=1#notifications', self.location.origin).href;
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const existing = windows.find(client => client.url.startsWith(self.location.origin));
    if (existing) {
      await existing.focus();
      existing.navigate(url);
      return;
    }
    await self.clients.openWindow(url);
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/proxy.php' || url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate' && url.pathname === '/miniapp.php') {
    event.respondWith((async () => {
      try {
        return await fetch(request);
      } catch (error) {
        const cache = await caches.open(CACHE);
        return (await cache.match(OFFLINE)) || new Response('XASS сейчас без сети.', {
          status: 503,
          headers: {'Content-Type': 'text/plain; charset=utf-8'}
        });
      }
    })());
    return;
  }

  // Only public shell assets belong in the offline cache. Other PHP pages,
  // private downloads and arbitrary URLs must always reach the server.
  if (!SHELL.includes(url.pathname + url.search)) return;

  event.respondWith((async () => {
    // Stable asset URLs can change during a deployment; cache-first would keep
    // the old JavaScript forever even after the server has been updated.
    try {
      const response = await fetch(request, {cache: 'no-cache'});
      if (response.ok) {
        try {
          const cache = await caches.open(CACHE);
          await cache.put(request, response.clone());
        } catch (error) {
          // Storage quota/private browsing must not break an online response.
        }
      }
      return response;
    } catch (error) {
      const cache = await caches.open(CACHE);
      return (await cache.match(request)) || new Response('', {status: 503, statusText: 'Offline'});
    }
  })());
});
