'use strict';

const assert = require('node:assert/strict');
const {webcrypto} = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'assets', 'xass-e2e.js'), 'utf8');
const STORAGE = 'xass-e2e-owner-v1';
const PASSWORD = 'distinct test password 2026';
const encoder = new TextEncoder();

function browser(entries = []) {
  const values = new Map(entries), counts = {generateKey: 0, deriveKey: 0, writes: 0, fetch: 0};
  const storage = {
    failWrites: false,
    getItem(key) { return values.has(key) ? values.get(key) : null; },
    setItem(key, value) {
      if (storage.failWrites) throw new Error('Storage quota exceeded');
      counts.writes += 1;
      values.set(key, String(value));
    },
    removeItem(key) { values.delete(key); }
  };
  const subtle = new Proxy(webcrypto.subtle, {
    get(target, name) {
      if (typeof target[name] !== 'function') return target[name];
      return (...args) => {
        if (name in counts) counts[name] += 1;
        return target[name](...args);
      };
    }
  });
  const window = {XASS: {state: {boot: {sources: []}}}};
  const context = vm.createContext({
    crypto: {subtle, getRandomValues: bytes => webcrypto.getRandomValues(bytes)},
    TextEncoder, TextDecoder, Uint8Array, ArrayBuffer, atob, btoa, localStorage: storage, window,
    fetch() { counts.fetch += 1; throw new Error('Key operations must stay local'); }
  });
  vm.runInContext(source, context);
  return {api: vm.runInContext('XassE2E', context), values, counts, storage, window};
}

function snapshot(browser) {
  return JSON.stringify([...browser.values.entries()].sort(([left], [right]) => left.localeCompare(right)));
}

async function forgedKeyFile(file, password, mutate) {
  const envelope = JSON.parse(file);
  const bytes = value => Buffer.from(value, 'base64url');
  const base = await webcrypto.subtle.importKey('raw', encoder.encode(password), 'PBKDF2', false, ['deriveKey']);
  const key = await webcrypto.subtle.deriveKey(
    {name: 'PBKDF2', hash: 'SHA-256', salt: bytes(envelope.salt), iterations: 210000},
    base, {name: 'AES-GCM', length: 256}, false, ['encrypt', 'decrypt']
  );
  const parameters = {name: 'AES-GCM', iv: bytes(envelope.nonce), additionalData: encoder.encode('xass-owner-key-export-v1')};
  const plain = await webcrypto.subtle.decrypt(parameters, key, bytes(envelope.data));
  const payload = JSON.parse(new TextDecoder().decode(plain));
  mutate(payload);
  parameters.iv = webcrypto.getRandomValues(new Uint8Array(12));
  envelope.nonce = Buffer.from(parameters.iv).toString('base64url');
  envelope.data = Buffer.from(await webcrypto.subtle.encrypt(parameters, key, encoder.encode(JSON.stringify(payload)))).toString('base64url');
  return JSON.stringify(envelope);
}

test('concurrent key requests share one generation and one atomic storage record', async () => {
  const b = browser();
  const first = b.api.ensureOwnerKeys();
  const second = b.api.ensureOwnerKeys();
  assert.equal(first, second);
  const pairs = await Promise.all([first, second, ...Array.from({length: 8}, () => b.api.ensureOwnerKeys())]);
  assert(pairs.every(pair => pair.privateJwk.d === pairs[0].privateJwk.d));
  assert.equal(b.counts.generateKey, 1);
  assert.equal(b.counts.writes, 1);
  assert.deepEqual([...b.values.keys()], [STORAGE]);
  assert.equal(b.counts.fetch, 0);
});

test('encrypted export/import lets a second browser decrypt existing PC ciphertext', async () => {
  const owner = browser(), iphone = browser(), agent = browser();
  const ownerKeys = await owner.api.ensureOwnerKeys();
  const agentKeys = await agent.api.ensureOwnerKeys();
  const ciphertext = await agent.api.sealText('private clipboard', ownerKeys.publicJwk);
  await assert.rejects(iphone.api.unsealText(ciphertext, agentKeys.publicJwk), /Импортируйте ключ/);
  const file = await owner.api.exportOwnerKey(PASSWORD);
  assert(!file.includes(ownerKeys.privateJwk.d));
  const envelope = JSON.parse(file);
  assert.equal(envelope.iterations, 210000);
  assert.equal(Buffer.from(envelope.salt, 'base64url').length, 16);
  assert.equal(Buffer.from(envelope.nonce, 'base64url').length, 12);
  const imported = await iphone.api.importOwnerKey(file, PASSWORD);
  assert.equal(JSON.stringify(imported), JSON.stringify(ownerKeys.publicJwk));
  assert.equal(await iphone.api.unsealText(ciphertext, agentKeys.publicJwk), 'private clipboard');
  assert.equal(await iphone.api.getOwnerFingerprint(), await owner.api.getOwnerFingerprint());
  assert.match(await owner.api.getOwnerFingerprint(), /^[A-F0-9]{4}(?:-[A-F0-9]{4}){3}$/);
  assert.equal(owner.counts.fetch + iphone.counts.fetch + agent.counts.fetch, 0);
});

test('wrong passwords, modified ciphertext and wrong private scalar preserve the active key', async () => {
  const owner = browser(), target = browser(), unrelated = browser();
  const file = await owner.api.exportOwnerKey(PASSWORD);
  await target.api.ensureOwnerKeys();
  const before = snapshot(target);
  await assert.rejects(target.api.importOwnerKey(file, 'wrong password 12345'), /Неверный пароль/);
  assert.equal(snapshot(target), before);
  const corrupt = JSON.parse(file);
  const blob = Buffer.from(corrupt.data, 'base64url');
  blob[10] ^= 1;
  corrupt.data = blob.toString('base64url');
  await assert.rejects(target.api.importOwnerKey(JSON.stringify(corrupt), PASSWORD), /повреждённый/);
  assert.equal(snapshot(target), before);
  const other = await unrelated.api.ensureOwnerKeys();
  const mismatched = await forgedKeyFile(file, PASSWORD, keys => { keys.privateJwk.d = other.privateJwk.d; });
  await assert.rejects(target.api.importOwnerKey(mismatched, PASSWORD), /повреждённый/);
  assert.equal(snapshot(target), before);
});

test('unsupported or excessive import parameters are rejected before password derivation', async () => {
  const owner = browser(), target = browser();
  const envelope = JSON.parse(await owner.api.exportOwnerKey(PASSWORD));
  const cases = [
    {...envelope, format: 'other'}, {...envelope, version: 2}, {...envelope, iterations: 210001},
    {...envelope, iterations: 1e12}, {...envelope, iterations: '210000'}, {...envelope, kdf: 'scrypt'},
    {...envelope, cipher: 'AES-CBC'}, {...envelope, salt: 'A'}, {...envelope, nonce: 'A'.repeat(16)},
    {...envelope, data: 'A'.repeat(12000)}
  ];
  // A 12-byte all-zero nonce is well-formed, so use an invalid 11-byte length.
  cases[8].nonce = Buffer.alloc(11).toString('base64url');
  for (const candidate of cases) await assert.rejects(target.api.importOwnerKey(JSON.stringify(candidate), PASSWORD));
  await assert.rejects(target.api.importOwnerKey('x'.repeat(16385), PASSWORD), /16 КБ/);
  await assert.rejects(target.api.importOwnerKey('not JSON', PASSWORD), /не файл/);
  assert.equal(target.counts.deriveKey, 0);
  assert.equal(target.counts.writes, 0);
});

test('password checks do not generate or replace browser keys', async () => {
  const b = browser();
  for (const password of ['', 'short', '🔐'.repeat(6), 'x'.repeat(4097)]) {
    await assert.rejects(b.api.exportOwnerKey(password), /12 до 4096/);
    await assert.rejects(b.api.importOwnerKey('{}', password), /12 до 4096/);
  }
  assert.equal(b.counts.generateKey, 0);
  assert.equal(b.counts.writes, 0);
});

test('legacy pairs migrate atomically and malformed legacy data is never replaced silently', async () => {
  const old = await browser().api.ensureOwnerKeys();
  const entries = [['xass-e2e-private-jwk', JSON.stringify(old.privateJwk)], ['xass-e2e-public-jwk', JSON.stringify(old.publicJwk)]];
  const legacy = browser(entries);
  assert.equal((await legacy.api.ensureOwnerKeys()).privateJwk.d, old.privateJwk.d);
  assert.deepEqual([...legacy.values.keys()], [STORAGE]);
  const quota = browser(entries);
  quota.storage.failWrites = true;
  const before = snapshot(quota);
  await assert.rejects(quota.api.ensureOwnerKeys(), /quota/);
  assert.equal(snapshot(quota), before);
  const broken = browser([entries[0], ['xass-e2e-public-jwk', '{}']]);
  const brokenBefore = snapshot(broken);
  await assert.rejects(broken.api.ensureOwnerKeys(), /не совпадают/);
  assert.equal(snapshot(broken), brokenBefore);
});

test('failed atomic import preserves existing keys and can be retried without reloading', async () => {
  const owner = browser(), target = browser();
  const file = await owner.api.exportOwnerKey(PASSWORD);
  await target.api.ensureOwnerKeys();
  const before = snapshot(target);
  target.storage.failWrites = true;
  await assert.rejects(target.api.importOwnerKey(file, PASSWORD), /quota/);
  assert.equal(snapshot(target), before);
  target.storage.failWrites = false;
  await Promise.all([target.api.ensureOwnerKeys(), target.api.importOwnerKey(file, PASSWORD)]);
  assert.equal(await target.api.getOwnerFingerprint(), await owner.api.getOwnerFingerprint());
});

test('agent public key lookup uses the current XASS bootstrap namespace', async () => {
  const b = browser();
  const key = (await b.api.ensureOwnerKeys()).publicJwk;
  b.window.XASS.state.boot.sources = [{source_name: 'Home PC', last_payload: {e2e_public_jwk: key}}];
  assert.equal(b.api.agentPublic('Home PC'), key);
  assert.equal(b.api.agentPublic('Missing PC'), null);
});

test('sealed clipboard responses cannot fall back to plaintext or another encryption purpose', async () => {
  const owner = browser(), agent = browser();
  const ownerKeys = await owner.api.ensureOwnerKeys(), agentKeys = await agent.api.ensureOwnerKeys();
  await assert.rejects(owner.api.unsealText({sealed: true, text: 'untrusted plaintext'}, agentKeys.publicJwk), /повреждён/);
  const ciphertext = await agent.api.sealBytes(encoder.encode('file content'), ownerKeys.publicJwk, 'file_upload');
  await assert.rejects(owner.api.unsealText({sealed: true, blob: agent.api.b64url(ciphertext)}, agentKeys.publicJwk), /расшифровать/);
});
