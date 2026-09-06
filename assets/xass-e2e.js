const XassE2E = (() => {
  const STORAGE_PRIV = 'xass-e2e-private-jwk';
  const STORAGE_PUB = 'xass-e2e-public-jwk';
  const enc = new TextEncoder();
  const dec = new TextDecoder();

  function b64url(bytes) {
    let bin = '';
    bytes = new Uint8Array(bytes);
    for (let i = 0; i < bytes.length; i += 1) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }
  function b64urlToBytes(value) {
    const padded = String(value || '').replace(/-/g, '+').replace(/_/g, '/') + '=='.slice(0, (4 - (String(value || '').length % 4)) % 4);
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
  async function ensureOwnerKeys() {
    try {
      const storedPriv = JSON.parse(localStorage.getItem(STORAGE_PRIV) || 'null');
      const storedPub = JSON.parse(localStorage.getItem(STORAGE_PUB) || 'null');
      if (storedPriv?.d && storedPub?.x && storedPub?.y) {
        return { privateJwk: storedPriv, publicJwk: publicOnly(storedPub) };
      }
    } catch (_) { /* generate new */ }
    const pair = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, ['deriveBits']);
    const privateJwk = await crypto.subtle.exportKey('jwk', pair.privateKey);
    const publicJwk = publicOnly(await crypto.subtle.exportKey('jwk', pair.publicKey));
    localStorage.setItem(STORAGE_PRIV, JSON.stringify(privateJwk));
    localStorage.setItem(STORAGE_PUB, JSON.stringify(publicJwk));
    return { privateJwk, publicJwk };
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
    if (data.length < 18 || data[0] !== 88 || data[1] !== 65 || data[2] !== 83 || data[3] !== 83 || data[4] !== 1) {
      throw new Error('Пакет не зашифрован или повреждён');
    }
    const keys = await ensureOwnerKeys();
    const aes = await sharedAesKey(keys.privateJwk, peerPublicJwk);
    const nonce = data.slice(5, 17);
    const ct = data.slice(17);
    return new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv: nonce, additionalData: enc.encode(aad) }, aes, ct));
  }
  async function sealText(text, peerPublicJwk) {
    const blob = await sealBytes(enc.encode(text), peerPublicJwk, 'clipboard');
    return { sealed: true, blob: b64url(blob) };
  }
  async function unsealText(payload, peerPublicJwk) {
    if (!payload?.sealed || !payload.blob) return String(payload?.text || '');
    const plain = await unsealBytes(b64urlToBytes(payload.blob), peerPublicJwk, 'clipboard');
    return dec.decode(plain);
  }
  function agentPublic(source) {
    const sources = (window.X && X.state && X.state.boot && X.state.boot.sources) || (window.state && state.boot && state.boot.sources) || [];
    const item = sources.find(row => row.source_name === source);
    return item && item.last_payload && item.last_payload.e2e_public_jwk || null;
  }
  return { ensureOwnerKeys, publicOnly, sealBytes, unsealBytes, sealText, unsealText, agentPublic, b64url, b64urlToBytes };
})();
