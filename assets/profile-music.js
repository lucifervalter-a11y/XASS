/* Opt-in public listening. Never list, cache or auto-play private library files. */
(() => {
  'use strict';
  const panel = document.getElementById('xassPublicMusic');
  if (!panel) return;
  const label = document.getElementById('xassPublicTrack');
  const button = document.getElementById('xassPublicListen');
  const status = document.getElementById('xassPublicMusicStatus');
  const audio = new Audio();
  audio.preload = 'none';
  let current = null, loadedId = null, listening = false, busy = false, timer;
  const mediaUrl = path => '/proxy.php?_binary=1&_media=1&_p=' + encodeURIComponent(path);
  const stop = () => { listening = false; audio.pause(); button.textContent = 'Слушать вместе'; };
  async function playCurrent() {
    if (!current?.playing) return;
    if (loadedId !== current.track.id) {
      audio.src = mediaUrl(current.path);
      loadedId = current.track.id;
    }
    const position = current.position + Math.max(0, (Date.now() - current.received) / 1000);
    try { audio.currentTime = position; } catch (_) { /* Seek again once Safari knows the duration. */ }
    audio.onloadedmetadata = () => { if (current?.playing) audio.currentTime = Math.min(position, audio.duration || position); };
    let playTimeout;
    try {
      const starting = audio.play();
      await Promise.race([starting, new Promise((_, reject) => {
        playTimeout = setTimeout(() => reject(new Error('audio-timeout')), 12000);
      })]);
      listening = true; button.textContent = 'Пауза'; status.textContent = '';
    } catch (error) {
      stop(); status.textContent = error.name === 'NotSupportedError' || audio.error?.code === 4
        ? 'Этот браузер не поддерживает формат трека. Попробуйте другой браузер.'
        : error.message === 'audio-timeout'
          ? 'Аудио не загрузилось вовремя. Проверьте связь и попробуйте ещё раз.'
          : 'Нажмите «Слушать вместе», чтобы начать воспроизведение.';
    } finally {
      clearTimeout(playTimeout);
    }
  }
  async function refresh() {
    if (busy) return;
    busy = true;
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch('/proxy.php?_passthrough=1&_p=%2Fapi%2Fmusic%2Fpublic', {cache:'no-store',signal:controller.signal});
      if (!response.ok) throw new Error('unavailable');
      const data = await response.json();
      if (!data.playing || !data.track || !/^\/api\/music\/tracks\/\d+\/stream\?ticket=/.test(data.path || '')) {
        current = null; stop(); panel.hidden = true; audio.removeAttribute('src'); loadedId = null; return;
      }
      current = {...data, received: Date.now()}; panel.hidden = false;
      label.textContent = [data.track.artist, data.track.title].filter(Boolean).join(' — ');
      if (listening && loadedId !== data.track.id) await playCurrent();
    } catch (_) {
      // A lost connection is not permission to keep broadcasting old audio.
      if (listening) { stop(); status.textContent = 'Связь с трансляцией потеряна. Попробуйте ещё раз.'; }
    } finally {
      clearTimeout(timeout); busy = false;
      clearTimeout(timer); timer = setTimeout(refresh, document.hidden && !listening ? 30000 : 8000);
    }
  }
  button.addEventListener('click', async () => {
    if (listening) { stop(); return; }
    button.disabled = true;
    // Keep play() in the tap's activation turn; awaiting a fetch first causes
    // Safari/iPhone to reject playback even though the visitor pressed Play.
    try { await playCurrent(); } finally { button.disabled = false; }
  });
  audio.addEventListener('ended', () => { loadedId = null; refresh(); });
  audio.addEventListener('error', () => { stop(); loadedId = null; status.textContent = 'Не удалось воспроизвести. Попробуйте ещё раз.'; });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { clearTimeout(timer); refresh(); } });
  window.addEventListener('pagehide', () => { clearTimeout(timer); stop(); });
  refresh();
})();
