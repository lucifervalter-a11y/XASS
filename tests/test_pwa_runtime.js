'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..');
const workerSource = fs.readFileSync(path.join(root, 'sw.js'), 'utf8');
const pageSource = fs.readFileSync(path.join(root, 'miniapp.php'), 'utf8');
const origin = 'https://xass.example';

function workerHarness() {
  const handlers = new Map(), stores = new Map(), requests = [];
  const key = request => new URL(typeof request === 'string' ? request : request.url, origin).href;
  const harness = {
    offline: false, quotaExceeded: false, requests,
    caches: {
      async open(name) {
        if (!stores.has(name)) stores.set(name, new Map());
        const entries = stores.get(name);
        return {
          async addAll(urls) { for (const url of urls) entries.set(key(url), new Response('cached:' + url)); },
          async match(request) { return entries.get(key(request))?.clone(); },
          async put(request, response) {
            if (harness.quotaExceeded) throw new Error('Quota exceeded');
            entries.set(key(request), response.clone());
          }
        };
      },
      async keys() { return [...stores.keys()]; },
      async delete(name) { return stores.delete(name); },
      async match(request) {
        for (const entries of stores.values()) if (entries.has(key(request))) return entries.get(key(request)).clone();
      }
    },
    async dispatch(url, mode = 'cors') {
      let response;
      handlers.get('fetch')({
        request: {url: new URL(url, origin).href, method: 'GET', mode},
        respondWith(value) { response = Promise.resolve(value); }
      });
      return response ? await response : null;
    },
    async install() {
      let pending;
      handlers.get('install')({waitUntil(value) { pending = value; }});
      await pending;
    }
  };
  const context = vm.createContext({
    URL, Response, console, caches: harness.caches,
    async fetch(request, options) {
      requests.push({url: key(request), options});
      if (harness.offline) throw new Error('Offline');
      return new Response('fresh:' + key(request));
    },
    self: {location: {origin}, addEventListener: (name, handler) => handlers.set(name, handler), skipWaiting() {}}
  });
  vm.runInContext(workerSource, context);
  return harness;
}

test('public pages and private routes never enter the PWA cache', async () => {
  const h = workerHarness();
  await h.install();
  for (const url of ['/profile.php', '/projects.php', '/vk-auth.php?code=example', '/proxy.php?_p=%2Fapi%2Fmini%2Fbootstrap', '/api/pwa/config']) {
    assert.equal(await h.dispatch(url, 'navigate'), null, url);
    assert.equal(await h.caches.match(url), undefined, url);
  }
});

test('offline app opens the offline page even when an old cache contains another page', async () => {
  const h = workerHarness();
  await h.install();
  const legacy = await h.caches.open('xass-shell-v7');
  await legacy.put('/miniapp.php?standalone=1', new Response('WRONG PROFILE PAGE'));
  h.offline = true;
  const response = await h.dispatch('/miniapp.php?standalone=1', 'navigate');
  assert.equal(await response.text(), 'cached:/offline.html');
});

test('online assets refresh stable URLs and remain available offline', async () => {
  const h = workerHarness();
  await h.install();
  const url = '/assets/miniapp-control-center.js?v=0130';
  const fresh = await h.dispatch(url);
  assert.equal(await fresh.text(), 'fresh:' + origin + url);
  assert.equal(h.requests.at(-1).options.cache, 'no-cache');
  h.offline = true;
  const offline = await h.dispatch(url);
  assert.equal(await offline.text(), 'fresh:' + origin + url);
});

test('storage quota errors do not discard a successful network response', async () => {
  const h = workerHarness();
  await h.install();
  h.quotaExceeded = true;
  const response = await h.dispatch('/manifest.webmanifest');
  assert.equal(response.status, 200);
  assert.equal(await response.text(), 'fresh:' + origin + '/manifest.webmanifest');
});

function pageFunction(start, end) {
  const from = pageSource.indexOf(start);
  assert.ok(from >= 0, start);
  const to = pageSource.indexOf(end, from);
  assert.ok(to > from, end);
  return pageSource.slice(from, to);
}

function elements() {
  const nodes = new Map();
  return id => {
    if (!nodes.has(id)) nodes.set(id, {style: {}, after() {}, disabled: false, hidden: false, value: ''});
    return nodes.get(id);
  };
}

test('update status shows Git and authorization failures instead of success', async () => {
  for (const response of [
    {status: 200, data: {ok: true, has_updates: false, errors: ['git fetch failed']}},
    {status: 401, data: {detail: 'Session expired'}}
  ]) {
    const $ = elements();
    const context = vm.createContext({$, api: async () => response});
    vm.runInContext(pageFunction('async function loadUpdateStatus()', "\n$('checkUpdateBtn').onclick"), context);
    await context.loadUpdateStatus();
    assert.match($('updateResult').textContent, /Не удалось проверить обновление/);
    assert.equal($('runUpdateBtn').disabled, true);
    assert.equal($('checkUpdateBtn').disabled, false);
  }
});

test('valid update status enables installation only when an update exists', async () => {
  for (const has_updates of [false, true]) {
    const $ = elements();
    const context = vm.createContext({$, api: async () => ({status: 200, data: {ok: true, has_updates, errors: [], commits: [{}]}})});
    vm.runInContext(pageFunction('async function loadUpdateStatus()', "\n$('checkUpdateBtn').onclick"), context);
    await context.loadUpdateStatus();
    assert.equal($('runUpdateBtn').disabled, !has_updates);
    assert.doesNotMatch($('updateResult').textContent, /Не удалось/);
  }
});

test('worker activation never reloads a login or unsaved form automatically', async () => {
  for (const controlled of [false, true]) {
    const $ = elements(), messages = [], handlers = {};
    let reloads = 0, options;
    const context = vm.createContext({
      $, console, state: {profileDirty: true}, toast: message => messages.push(message),
      document: {createElement: () => $('reloadPwaBtn')},
      location: {reload() { reloads++; }},
      ask(_message, callback) { callback(false); },
      setInterval() {}, setTimeout(callback) { callback(); },
      navigator: {serviceWorker: {
        controller: controlled ? {} : null,
        addEventListener(name, handler) { handlers[name] = handler; },
        async register(_url, opts) { options = opts; return {update: async () => {}}; }
      }}
    });
    vm.runInContext(pageFunction('function setupServiceWorker()', '\nsetupServiceWorker();'), context);
    context.setupServiceWorker();
    handlers.message({data: {type: 'XASS_SW_UPDATED', cache: 'new-cache'}});
    assert.equal(reloads, 0);
    assert.equal($('reloadPwaBtn').hidden, !controlled);
    assert.equal(options.updateViaCache, 'none');
    $('reloadPwaBtn').onclick();
    assert.equal(reloads, 0, 'cancelled confirmation preserves unsaved changes');
  }
});

test('cancelled passkey registration gives feedback and does not submit an empty key', async () => {
  const $ = elements(), messages = [], requests = [];
  const context = vm.createContext({
    $, toast: message => messages.push(message),
    window: {isSecureContext: true, PublicKeyCredential: {}},
    navigator: {credentials: {async create() { const error = new Error(); error.name = 'NotAllowedError'; throw error; }}},
    prepareCredentialOptions: options => options,
    async pwaApi(route) { requests.push(route); return {status: 200, data: {ok: true, options: {}}}; }
  });
  vm.runInContext(pageFunction('function passkeyErrorMessage(', "\n$('registerPasskeyBtn').onclick"), context);
  await context.registerPasskey();
  assert.match(messages.at(-1), /Проверка отменена/);
  assert.deepEqual(requests, ['passkeys/register/options']);
  assert.equal($('registerPasskeyBtn').disabled, false);
});

test('inline and external application scripts are syntactically valid', () => {
  for (const match of pageSource.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
  new vm.Script(fs.readFileSync(path.join(root, 'assets/miniapp-control-center.js'), 'utf8'));
});
