(() => {
  'use strict';
  const X = window.XASS;
  const host = document.getElementById('configExportBtn')?.closest('.disclosure');
  if (!X || !host) return;
  const section = document.createElement('details');
  section.innerHTML = `<summary>Весь сервер и перенос</summary><div class="detail-body">
    <p class="muted">Сайт, база, переписки, медиа и ключи доступа — один зашифрованный файл. Это копия XASS, а не образ диска Linux. Архив доступен 24 часа.</p>
    <label class="label" for="serverBackupPassword">Пароль полной копии</label>
    <input class="input" id="serverBackupPassword" type="password" autocomplete="new-password" minlength="12" maxlength="256" placeholder="Не менее 12 символов">
    <div class="toolbar" style="margin-top:12px"><button class="btn blue" id="serverBackupCreate">Подготовить файл</button><button class="btn" id="serverMigrationCreate">Создать код переноса</button></div>
    <p id="serverBackupStatus" role="status" aria-live="polite" class="muted"></p>
    <button class="btn blue" id="serverBackupDownload" hidden>Скачать полный архив</button>
    <div id="serverMigrationCodeBox" hidden><label class="label" for="serverMigrationCode">Одноразовый код · 1 час с момента создания</label>
    <textarea class="input mono" id="serverMigrationCode" rows="4" readonly spellcheck="false" aria-label="Код переноса сервера"></textarea>
    <p class="muted">Код открывает доступ ко всей копии и её ключам. Сохраните его до закрытия страницы и вставляйте только на своём новом сервере.</p>
    <div class="toolbar"><button class="btn" id="serverMigrationCopy">Копировать код</button><button class="btn danger" id="serverMigrationRevoke">Отозвать код</button></div></div>
    <details style="margin-top:12px"><summary>Как перенести на новый сервер</summary><p class="muted">На новом Ubuntu/Debian выполните команду ниже и вставьте код по запросу. Каталог назначения должен быть новым. После проверки копии остановите старый backend и агент, затем активируйте новый сервер по инструкции. Для окончательного переноса нужна свежая копия и переключение домена.</p>
    <pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere">git clone https://github.com/lucifervalter-a11y/XASS.git /tmp/xass-migration-tools
bash /tmp/xass-migration-tools/deploy/migrate.sh https://github.com/lucifervalter-a11y/XASS.git /opt/xass-new</pre>
    <a class="btn" href="https://github.com/lucifervalter-a11y/XASS/blob/main/docs/SERVER_MIGRATION.md" target="_blank" rel="noopener noreferrer">Инструкция восстановления</a></details>
    </div>`;
  host.prepend(section);
  const $ = id => document.getElementById(id);
  let job = '', mode = 'backup', timer;
  try { job = sessionStorage.getItem('xass-server-backup-job') || ''; } catch (_) {}
  const status = text => { $('serverBackupStatus').textContent = text; };
  async function request(path, options = {}, proof = false) {
    if (X.demo) throw new Error('Полная копия доступна после входа на свой сервер');
    if (proof) options.headers = {'X-XASS-Action-Proof': await X.passkeyAction('server:backup')};
    const response = await X.api(path, options);
    if (!response.data?.ok) throw new Error(response.data?.detail || 'Сервер не ответил');
    return response.data;
  }
  function busy(value) {
    $('serverBackupCreate').disabled = value;
    $('serverMigrationCreate').disabled = value;
  }
  async function poll() {
    clearTimeout(timer);
    if (!job || !X.state.owner) return;
    try {
      const result = await request('server-backups/' + job);
      if (result.job.state === 'building') {
        busy(true); status('Создаётся полная копия. Страницу можно оставить открытой.');
        timer = setTimeout(poll, 3000); return;
      }
      busy(false);
      if (result.job.state === 'failed') throw new Error(result.job.error);
      status('Копия готова · ' + (result.job.size / 1048576).toFixed(1) + ' МБ. ' + (mode === 'migration' ? 'Теперь можно использовать код.' : 'Для восстановления потребуется ваш пароль.'));
      $('serverBackupDownload').hidden = mode === 'migration';
    } catch (error) { busy(false); status(error.message); }
  }
  async function create(nextMode) {
    const password = $('serverBackupPassword').value;
    if (nextMode === 'backup' && password.length < 12) { X.toast('Введите пароль не короче 12 символов'); return; }
    busy(true); clearTimeout(timer);
    try {
      const result = await request('server-backups', {method: 'POST', body: {mode: nextMode, passphrase: nextMode === 'backup' ? password : ''}}, true);
      job = result.job.id; mode = nextMode;
      try { sessionStorage.setItem('xass-server-backup-job', job); } catch (_) {}
      $('serverBackupDownload').hidden = true;
      $('serverMigrationCode').value = result.code || '';
      $('serverMigrationCodeBox').hidden = !result.code;
      $('serverBackupPassword').value = '';
      await poll();
    } catch (error) { busy(false); status(error.message); }
  }
  $('serverBackupCreate').onclick = () => create('backup');
  $('serverMigrationCreate').onclick = () => create('migration');
  $('serverMigrationCopy').onclick = () => X.copyText($('serverMigrationCode').value);
  $('serverMigrationRevoke').onclick = async () => {
    try {
      await request('server-backups/' + job + '/revoke', {method: 'POST'});
      $('serverMigrationCode').value = ''; $('serverMigrationCodeBox').hidden = true;
      status('Код отозван. Уже скачанная копия остаётся у получателя.');
    } catch (error) { status(error.message); }
  };
  $('serverBackupDownload').onclick = async () => {
    $('serverBackupDownload').disabled = true;
    try {
      const result = await request('server-backups/' + job + '/download-ticket', {method: 'POST'}, true);
      const form = document.createElement('form');
      form.method = 'POST'; form.action = '/proxy.php?_binary=1&_p=%2Fapi%2Fserver-transfer%2Fdownload';
      form.hidden = true;
      const input = document.createElement('input'); input.name = 'ticket'; input.value = result.ticket;
      form.appendChild(input); document.body.appendChild(form); form.submit(); form.remove();
    } catch (error) { status(error.message); }
    finally { $('serverBackupDownload').disabled = false; }
  };
  function boot() { section.hidden = !X.state.owner; if (X.state.owner) poll(); }
  document.addEventListener('xass:boot', boot);
  boot();
})();
