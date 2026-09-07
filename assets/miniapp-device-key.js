(() => {
  'use strict';
  const X = window.XASS;
  const host = document.getElementById('registerPasskeyBtn')?.closest('.section');
  if (!X || !host || typeof XassE2E === 'undefined') return;
  const panel = document.createElement('details');
  panel.className = 'cc-key-transfer';
  panel.innerHTML = `<summary>Защищённые файлы ПК на iPhone</summary>
    <p>Telegram, Safari и приложение на экране «Домой» могут хранить разные ключи. Чтобы открывать снимки экрана, файлы и буфер одного ПК, перенесите ключ из браузера, в котором вы привязали этот ПК.</p>
    <ol class="steps"><li>Здесь сохраните ключ под надёжным паролем.</li><li>Передайте файл себе через AirDrop или «Файлы».</li><li>Откройте этот раздел в приложении XASS на iPhone и импортируйте файл с тем же паролем.</li></ol>
    <p>Файл шифруется на устройстве, ключ и пароль не отправляются серверу. Это не файл подключения агента и не ссылка для входа. Блокировка и другие обычные команды ПК работают без переноса этого ключа.</p>
    <label class="label" for="ccKeyPassword">Пароль файла ключа · минимум 12 символов</label>
    <input class="input" id="ccKeyPassword" type="password" autocomplete="off" minlength="12" maxlength="1024" placeholder="Пароль только для этого файла">
    <div class="toolbar"><button class="btn blue" id="ccKeyExport">Сохранить ключ</button><button class="btn" id="ccKeyImport">Импортировать ключ</button></div>
    <input id="ccKeyFile" type="file" accept=".xass-key,.json,application/json" hidden>
    <p id="ccKeyStatus" role="status" aria-live="polite"></p>
    <p class="mono" id="ccKeyFingerprint"></p>`;
  host.appendChild(panel);
  const $ = id => document.getElementById(id);
  const status = text => { $('ccKeyStatus').textContent = text; };
  const busy = value => { $('ccKeyExport').disabled = value; $('ccKeyImport').disabled = value; $('ccKeyPassword').disabled = value; };
  async function fingerprint() {
    if (X.demo) { $('ccKeyFingerprint').textContent = 'Демо · ключ не создаётся'; return; }
    try { $('ccKeyFingerprint').textContent = 'Ключ этого браузера: ' + await XassE2E.getOwnerFingerprint(); }
    catch (error) { status(error.message); }
  }
  panel.addEventListener('toggle', () => { if (panel.open) fingerprint(); });
  $('ccKeyExport').onclick = async () => {
    if (X.demo) return X.toast('Перенос ключа доступен после входа в свой XASS');
    busy(true); status('Шифруем файл на вашем устройстве…');
    try {
      const text = await XassE2E.exportOwnerKey($('ccKeyPassword').value);
      const file = new File([text], 'XASS-device.xass-key', {type: 'application/json'});
      let shared = false;
      if (navigator.canShare?.({files: [file]})) {
        try { await navigator.share({files: [file], title: 'Ключ устройств XASS'}); shared = true; }
        catch (error) { if (error.name === 'AbortError') { status('Отправка отменена. Ключ не изменён.'); return; } }
      }
      if (!shared) {
        const url = URL.createObjectURL(file), link = document.createElement('a');
        link.href = url; link.download = file.name; document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
      }
      $('ccKeyPassword').value = '';
      status('Зашифрованный файл подготовлен. Сохраните пароль отдельно: восстановить его нельзя.');
      await fingerprint();
    } catch (error) { status(error.message || 'Не удалось сохранить ключ'); }
    finally { busy(false); }
  };
  $('ccKeyImport').onclick = () => {
    if (X.demo) return X.toast('Перенос ключа доступен после входа в свой XASS');
    if ([...$('ccKeyPassword').value].length < 12) return status('Сначала введите пароль файла — минимум 12 символов.');
    $('ccKeyFile').click();
  };
  $('ccKeyFile').onchange = async event => {
    const file = event.target.files?.[0]; event.target.value = '';
    if (!file) return;
    if (file.size > 16384) return status('Файл ключа слишком большой. Выберите .xass-key, сохранённый в XASS.');
    if (!await new Promise(resolve => X.ask('Импорт заменит ключ только в этом браузере. Если здесь привязаны другие ПК, сначала сохраните текущий ключ: их зашифрованные данные могут стать недоступны.', resolve))) return;
    busy(true); status('Проверяем пароль и ключ…');
    try {
      await XassE2E.importOwnerKey(await file.text(), $('ccKeyPassword').value);
      $('ccKeyPassword').value = '';
      status('Ключ импортирован. Можно запрашивать новые снимки, файлы и буфер этого ПК.');
      await fingerprint();
    } catch (error) { status(error.message || 'Не удалось импортировать ключ. Текущий ключ сохранён.'); }
    finally { busy(false); }
  };
})();
