const XassE2E = (() => {
  const STORAGE_PRIV = 'xass-e2e-private-jwk';
  const STORAGE_PUB = 'xass-e2e-public-jwk';
  const STORAGE_KEYS = 'xass-e2e-owner-v1';
  const EXPORT_FORMAT = 'xass-e2e-owner-key';
  const EXPORT_ITERATIONS = 210000;
  const MAX_IMPORT_BYTES = 16 * 1024;
  const enc = new TextEncoder();
  const dec = new TextDecoder();
  const exportAad = enc.encode('xass-owner-key-export-v1');
  let pendingKeys = null;
  let keyWrites = Promise.resolve();

  function b64url(bytes) {
    let bin = '';
    bytes = new Uint8Array(bytes);
    for (let i = 0; i < bytes.length; i += 1) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }
  function b64urlToBytes(value) {
    const text = String(value || '');
    const padded = text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - text.length % 4) % 4);
    const bin = atob(padded);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i);
    return out;
  }
  function publicOnly(jwk) {
    return { kty: 'EC', crv: 'P-256', x: jwk.x, y: jwk.y };
  }
  async function importPrivate(jwk) {
    return crypto.subtle.importKey('jwk', jwk, { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  }
  async function importPublic(jwk) {
    return crypto.subtle.importKey('jwk', publicOnly(jwk), { name: 'ECDH', namedCurve: 'P-256' }, true, []);
  }
  async function sharedAesKey(privateJwk, peerPublicJwk) {
    const bits = await crypto.subtle.deriveBits(
      { name: 'ECDH', public: await importPublic(peerPublicJwk) },
      await importPrivate(privateJwk),
      256
    );
    const hkdfKey = await crypto.subtle.importKey('raw', bits, 'HKDF', false, ['deriveBits']);
    const aesBits = await crypto.subtle.deriveBits(
      { name: 'HKDF', hash: 'SHA-256', salt: enc.encode('xass-e2e-v1'), info: enc.encode('xass-e2e-aes') },
      hkdfKey,
      256
    );
    return crypto.subtle.importKey('raw', aesBits, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt']);
  }
  function serialKeyWrite(work) {
    const result = keyWrites.then(work);
    keyWrites = result.catch(() => {});
    return result;
  }
  function keyCoordinate(value) {
    if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{43}$/.test(value)) throw new Error('Некорректный ключ P-256');
    const bytes = b64urlToBytes(value);
    if (bytes.length !== 32 || b64url(bytes) !== value) throw new Error('Некорректный ключ P-256');
    return value;
  }
  function checkedPublic(jwk) {
    if (!jwk || jwk.kty !== 'EC' || jwk.crv !== 'P-256') throw new Error('Нужен ключ P-256');
    return {kty: 'EC', crv: 'P-256', x: keyCoordinate(jwk.x), y: keyCoordinate(jwk.y)};
  }
  async function validateOwnerKeys(value) {
    const publicJwk = checkedPublic(value?.publicJwk);
    const privateJwk = {...checkedPublic(value?.privateJwk), d: keyCoordinate(value?.privateJwk?.d)};
    if (privateJwk.x !== publicJwk.x || privateJwk.y !== publicJwk.y) throw new Error('Открытый и закрытый ключи не совпадают');
    const [secret, published, challenge] = await Promise.all([
      importPrivate(privateJwk), importPublic(publicJwk),
      crypto.subtle.generateKey({name: 'ECDH', namedCurve: 'P-256'}, false, ['deriveBits'])
    ]);
    // Some JWK importers retain supplied public coordinates. Verify they really
    // correspond to d using an independent, ephemeral ECDH agreement.
    const [left, right] = await Promise.all([
      crypto.subtle.deriveBits({name: 'ECDH', public: challenge.publicKey}, secret, 256),
      crypto.subtle.deriveBits({name: 'ECDH', public: published}, challenge.privateKey, 256)
    ]);
    const a = new Uint8Array(left), b = new Uint8Array(right);
    let difference = 0;
    for (let i = 0; i < a.length; i += 1) difference |= a[i] ^ b[i];
    if (difference) throw new Error('Открытый и закрытый ключи не совпадают');
    return {privateJwk, publicJwk};
  }
  function storeOwnerKeys(keys) {
    // A single synchronous write prevents a private/public half-pair on quota
    // failure. Do not erase the legacy pair until its replacement is durable.
    localStorage.setItem(STORAGE_KEYS, JSON.stringify({version: 1, ...keys}));
    try {
      localStorage.removeItem(STORAGE_PRIV);
      localStorage.removeItem(STORAGE_PUB);
    } catch (_) { /* the atomic record remains authoritative */ }
    return keys;
  }
  async function loadOrCreateOwnerKeys() {
    const stored = localStorage.getItem(STORAGE_KEYS);
    if (stored !== null) {
      try {
        const value = JSON.parse(stored);
        if (value.version !== 1) throw new Error('version');
        return await validateOwnerKeys(value);
      } catch (_) {
        throw new Error('Локальный ключ XASS повреждён. Импортируйте сохранённый ключ с другого устройства.');
      }
    }
    const oldPrivate = localStorage.getItem(STORAGE_PRIV), oldPublic = localStorage.getItem(STORAGE_PUB);
    if (oldPrivate !== null || oldPublic !== null) {
      let keys;
      try {
        keys = await validateOwnerKeys({privateJwk: JSON.parse(oldPrivate), publicJwk: JSON.parse(oldPublic)});
      } catch (_) {
        throw new Error('Сохранённые ключи XASS не совпадают. Импортируйте ключ с устройства, где управление ПК работает.');
      }
      return storeOwnerKeys(keys);
    }
    const pair = await crypto.subtle.generateKey({name: 'ECDH', namedCurve: 'P-256'}, true, ['deriveBits']);
    const privateJwk = await crypto.subtle.exportKey('jwk', pair.privateKey);
    const publicJwk = publicOnly(await crypto.subtle.exportKey('jwk', pair.publicKey));
    return storeOwnerKeys({privateJwk: {...publicJwk, d: privateJwk.d}, publicJwk});
  }
  function ensureOwnerKeys() {
    if (!pendingKeys) {
      const task = serialKeyWrite(loadOrCreateOwnerKeys);
      pendingKeys = task;
      const clear = () => { if (pendingKeys === task) pendingKeys = null; };
      task.then(clear, clear);
    }
    return pendingKeys;
  }
  function passwordBytes(password) {
    if (typeof password !== 'string' || password.length > 4096 || [...password].length < 12) {
      throw new Error('Пароль файла ключа должен содержать от 12 до 4096 символов.');
    }
    return enc.encode(password);
  }
  async function exportPasswordKey(password, salt) {
    const base = await crypto.subtle.importKey('raw', passwordBytes(password), 'PBKDF2', false, ['deriveKey']);
    return crypto.subtle.deriveKey(
      {name: 'PBKDF2', hash: 'SHA-256', salt, iterations: EXPORT_ITERATIONS},
      base, {name: 'AES-GCM', length: 256}, false, ['encrypt', 'decrypt']
    );
  }
  async function exportOwnerKey(password) {
    passwordBytes(password);
    const keys = await ensureOwnerKeys();
    const salt = crypto.getRandomValues(new Uint8Array(16)), nonce = crypto.getRandomValues(new Uint8Array(12));
    const aes = await exportPasswordKey(password, salt);
    const data = await crypto.subtle.encrypt({name: 'AES-GCM', iv: nonce, additionalData: exportAad}, aes, enc.encode(JSON.stringify(keys)));
    return JSON.stringify({format: EXPORT_FORMAT, version: 1, kdf: 'PBKDF2-SHA256', iterations: EXPORT_ITERATIONS,
      cipher: 'AES-256-GCM', salt: b64url(salt), nonce: b64url(nonce), data: b64url(data)}, null, 2);
  }
  function importBytes(value, minimum, maximum) {
    if (typeof value !== 'string' || !/^[A-Za-z0-9_-]+$/.test(value) || value.length > Math.ceil(maximum * 4 / 3)) {
      throw new Error('Некорректный файл ключа XASS');
    }
    const bytes = b64urlToBytes(value);
    if (bytes.length < minimum || bytes.length > maximum || b64url(bytes) !== value) throw new Error('Некорректный файл ключа XASS');
    return bytes;
  }
  async function importOwnerKey(text, password) {
    passwordBytes(password);
    if (typeof text !== 'string' || text.length > MAX_IMPORT_BYTES || enc.encode(text).length > MAX_IMPORT_BYTES) {
      throw new Error('Файл ключа XASS должен быть не больше 16 КБ.');
    }
    let envelope;
    try { envelope = JSON.parse(text); } catch (_) { throw new Error('Это не файл ключа XASS.'); }
    if (!envelope || envelope.format !== EXPORT_FORMAT || envelope.version !== 1 || envelope.kdf !== 'PBKDF2-SHA256'
      || envelope.iterations !== EXPORT_ITERATIONS || envelope.cipher !== 'AES-256-GCM') {
      throw new Error('Неподдерживаемый формат или параметры файла ключа XASS.');
    }
    const salt = importBytes(envelope.salt, 16, 16), nonce = importBytes(envelope.nonce, 12, 12);
    const data = importBytes(envelope.data, 17, 8192);
    const aes = await exportPasswordKey(password, salt);
    let keys;
    try {
      const plain = await crypto.subtle.decrypt({name: 'AES-GCM', iv: nonce, additionalData: exportAad}, aes, data);
      keys = await validateOwnerKeys(JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(plain)));
    } catch (_) {
      throw new Error('Неверный пароль или повреждённый файл ключа XASS. Действующий ключ не изменён.');
    }
    await serialKeyWrite(() => storeOwnerKeys(keys));
    return keys.publicJwk;
  }
  async function getOwnerFingerprint() {
    const keys = await ensureOwnerKeys();
    const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', enc.encode(JSON.stringify(publicOnly(keys.publicJwk)))));
    const short = Array.from(bytes.slice(0, 8), byte => byte.toString(16).padStart(2, '0')).join('').toUpperCase();
    return short.match(/.{4}/g).join('-');
  }
  async function sealBytes(bytes, peerPublicJwk, aad) {
    const keys = await ensureOwnerKeys();
    const aes = await sharedAesKey(keys.privateJwk, peerPublicJwk);
    const nonce = crypto.getRandomValues(new Uint8Array(12));
    const ct = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv: nonce, additionalData: enc.encode(aad) }, aes, bytes));
    const out = new Uint8Array(5 + 12 + ct.length);
    out.set([88, 65, 83, 83, 1], 0);
    out.set(nonce, 5);
    out.set(ct, 17);
    return out;
  }
  async function unsealBytes(blob, peerPublicJwk, aad) {
    const data = blob instanceof Uint8Array ? blob : new Uint8Array(blob);
    if (data.length < 33 || data[0] !== 88 || data[1] !== 65 || data[2] !== 83 || data[3] !== 83 || data[4] !== 1) {
      throw new Error('Пакет не зашифрован или повреждён');
    }
    const keys = await ensureOwnerKeys();
    try {
      const aes = await sharedAesKey(keys.privateJwk, peerPublicJwk);
      const nonce = data.slice(5, 17);
      const ct = data.slice(17);
      return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: nonce, additionalData: enc.encode(aad) }, aes, ct));
    } catch (_) {
      throw new Error('Не удалось расшифровать данные. Импортируйте ключ с устройства, на котором привязывали этот ПК, и повторите запрос.');
    }
  }
  async function sealText(text, peerPublicJwk) {
    const blob = await sealBytes(enc.encode(text), peerPublicJwk, 'clipboard');
    return { sealed: true, blob: b64url(blob) };
  }
  async function unsealText(payload, peerPublicJwk) {
    if (!payload?.sealed) return String(payload?.text || '');
    if (typeof payload.blob !== 'string' || !payload.blob) throw new Error('Зашифрованный пакет повреждён. Повторите запрос.');
    const plain = await unsealBytes(b64urlToBytes(payload.blob), peerPublicJwk, 'clipboard');
    return dec.decode(plain);
  }
  function agentPublic(source) {
    const sources = window.XASS?.state?.boot?.sources || [];
    const item = sources.find(row => row.source_name === source);
    return item && item.last_payload && item.last_payload.e2e_public_jwk || null;
  }
  return { ensureOwnerKeys, publicOnly, sealBytes, unsealBytes, sealText, unsealText, agentPublic, b64url, b64urlToBytes,
    exportOwnerKey, importOwnerKey, getOwnerFingerprint };
})();
