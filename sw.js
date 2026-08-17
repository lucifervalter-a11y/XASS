const CACHE = 'xass-shell-v3';
const OFFLINE = '/offline.html';
const SHELL = [
  OFFLINE,
  '/miniapp.php?standalone=1',
  '/manifest.webmanifest',
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

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response.ok) {
          const cache = await caches.open(CACHE);
          await cache.put('/miniapp.php?standalone=1', response.clone());
        }
        return response;
      } catch (error) {
        return (await caches.match('/miniapp.php?standalone=1')) || (await caches.match(OFFLINE));
      }
    })());
    return;
  }

  event.respondWith((async () => {
    const cached = await caches.match(request);
    if (cached) return cached;
    try {
      const response = await fetch(request);
      if (response.ok) {
        const cache = await caches.open(CACHE);
        await cache.put(request, response.clone());
      }
      return response;
    } catch (error) {
      return new Response('', {status: 503, statusText: 'Offline'});
    }
  })());
});
