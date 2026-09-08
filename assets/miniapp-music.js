/* XASS owned library + local/native/Windows playback. No external media search. */
(() => {
  'use strict';
  const X = window.XASS, root = document.getElementById('xassMusic');
  if (!X || !root) return;
  const esc = X.esc, $ = id => document.getElementById(id);
  const ART = '/assets/music-note.svg';
  const PATHS = {
    plus:'M12 5v14M5 12h14', search:'m21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    heart:'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
    play:'m8 4 12 8-12 8Z', pause:'M8 5v14M16 5v14', next:'m5 5 11 7-11 7ZM19 5v14',
    prev:'m19 5-11 7 11 7ZM5 5v14', shuffle:'m18 3 3 3-3 3M3 6h3c5 0 7 12 12 12h3m-3 3 3-3-3-3M3 18h3c2 0 4-3 6-6s4-6 6-6h3',
    repeat:'m17 2 4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4m14-3v4a3 3 0 0 1-3 3H3',
    down:'m5 9 7 7 7-7', close:'m6 6 12 12M6 18 18 6', more:'M5 12h.01M12 12h.01M19 12h.01',
    volume:'m11 5-6 4H2v6h3l6 4ZM15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14',
    speaker:'M5 3h14v18H5ZM12 6h.01M16 14a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
    download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', queue:'M3 5h14M3 10h14M3 15h8m7-3v8m0-8 4-1m-4 9a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
    device:'M8 2h8v20H8ZM11 18h2', check:'m5 12 4 4L19 6', chevron:'m9 5 7 7-7 7', trash:'M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7',
  };
  const icon = (name, filled=false) => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="${filled?'currentColor':'none'}" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="${PATHS[name]||PATHS.play}"/></svg>`;
  const button = (action, label, glyph, extra='') => `<button type="button" class="xm-icon" data-xm="${action}" aria-label="${esc(label)}" title="${esc(label)}" ${extra}>${icon(glyph)}</button>`;
  const seconds = value => {const n=Math.max(0,Math.floor(Number(value)||0));return `${Math.floor(n/60)}:${String(n%60).padStart(2,'0')}`;};
  const finite = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const native = () => window.XASS_NATIVE_AUDIO === true && !!window.webkit?.messageHandlers?.xassAudio;
  const nativeSend = (action, detail={}) => window.webkit.messageHandlers.xassAudio.postMessage({action,...detail});
  const localLabel = () => /iPhone|iPad/i.test(navigator.userAgent) ? 'Этот iPhone' : 'Это устройство';
  const key = Array.from(crypto.getRandomValues(new Uint8Array(20)),b=>b.toString(16).padStart(2,'0')).join('');
  const s = {tracks:[],playlists:[],filter:'all',playlist:null,query:'',limit:50,loaded:false,loading:false,error:'',
    current:null,queue:[],shuffle:false,repeat:'off',state:'stopped',position:0,duration:0,volume:70,
    device:'local',output:'default',outputs:[],shareSite:false,shareSaving:false,shareDiscord:false,playerOpen:false,playGeneration:0,
    pcPolling:false,sessionOwned:false,publishing:false,lastPublish:0,upload:null,downloads:new Set(),busy:false,
    transitioning:false,requestedDevice:null,needsStart:false,enabled:true};
  const audio = document.createElement('audio');
  audio.preload='metadata'; audio.setAttribute('playsinline',''); audio.id='xmAudio';
  root.innerHTML = `<div class="xm-library">
    <header class="xm-heading"><h1>Музыка</h1>${button('upload','Добавить музыку','plus')}</header>
    <label class="xm-search">${icon('search')}<input id="xmSearch" type="search" placeholder="Поиск в библиотеке" aria-label="Поиск в библиотеке" maxlength="240"></label>
    <div class="xm-tabs" role="tablist" aria-label="Музыкальная библиотека"><button role="tab" aria-selected="true" data-xm="filter-all">Все</button><button role="tab" aria-selected="false" data-xm="filter-favorites">Избранное</button><button role="tab" aria-selected="false" data-xm="filter-playlists">Плейлисты</button></div>
    <div class="xm-list-actions"><button class="xm-primary" data-xm="play-all">${icon('play',true)}Слушать всё</button>${button('shuffle-all','Перемешать и слушать','shuffle')}</div>
    <div id="xmUpload" role="status" hidden></div><div id="xmLibraryMessage" role="status"></div><div id="xmPlaylistHeading"></div><div id="xmTracks" class="xm-tracks"></div>
    <button id="xmMore" class="xm-secondary" data-xm="more" hidden>Показать ещё</button>
  </div>
  <input id="xmFile" type="file" accept=".mp3,.wav,.flac,.ogg,.m4a,.zip,audio/*,application/zip" multiple hidden>
  <div id="xmMini" class="xm-mini" hidden><button data-xm="expand" class="xm-mini-track"><img src="${ART}" alt=""><span><strong id="xmMiniTitle"></strong><small id="xmMiniDevice"></small></span></button>${button('toggle','Воспроизвести','play','id="xmMiniPlay"')}</div>
  <div id="xmPlayer" class="xm-player-backdrop" hidden><section class="xm-player" role="dialog" aria-modal="true" aria-labelledby="xmPlayerCaption" tabindex="-1">
    <header class="xm-player-head">${button('collapse','Свернуть плеер','down')}<span id="xmPlayerCaption">Сейчас играет</span>${button('queue','Очередь воспроизведения','queue')}</header>
    <img class="xm-art" src="${ART}" alt="Обложка трека">
    <div class="xm-track-heading"><div><h2 id="xmPlayerTitle"></h2><p id="xmPlayerArtist"></p></div>${button('favorite-current','Добавить в избранное','heart','id="xmPlayerFavorite"')}</div>
    <div class="xm-progress"><input id="xmSeek" type="range" min="0" max="1" step="0.1" value="0" aria-label="Позиция трека"><div><span id="xmPosition">0:00</span><span id="xmDuration">0:00</span></div></div>
    <div id="xmPlayerStatus" class="xm-player-status" role="status"></div>
    <div class="xm-transport">${button('shuffle','Перемешивание','shuffle','id="xmShuffle"')}${button('prev','Предыдущий трек','prev')}${button('toggle','Воспроизвести','play','id="xmMainPlay"')}${button('next','Следующий трек','next')}${button('repeat','Повтор выключен','repeat','id="xmRepeat"')}</div>
    <button class="xm-device" data-xm="devices">${icon('volume')}<span id="xmDeviceName"></span>${icon('chevron')}</button>
    <label class="xm-volume">${icon('volume')}<input id="xmVolume" type="range" min="0" max="100" step="1" value="70" aria-label="Громкость">${icon('speaker')}</label>
    <div class="xm-sharing"><label><input id="xmShareSite" type="checkbox" role="switch" aria-label="Публиковать музыку на сайте"><span class="xm-switch"></span><span id="xmShareSiteText" aria-live="polite">На сайте</span></label><button class="xm-text" data-xm="discord-help">Звук в Discord</button></div>
    <button class="xm-download" data-xm="download-current">${icon('download')}<span id="xmDownloadLabel">Скачать трек</span></button>
  </section></div>
  <dialog id="xmDialog" class="xm-dialog"><div class="xm-dialog-head"><h2 id="xmDialogTitle"></h2>${button('close-dialog','Закрыть','close')}</div><div id="xmDialogBody"></div></dialog>`;
  root.append(audio);

  async function request(path,method='GET',body){
    const result = await X.api('music/'+path,{method,...(body===undefined?{}:{body})});
    if(result.status<200||result.status>=300||!result.data?.ok){const detail=result.data?.detail;const error=new Error(typeof detail==='string'?detail:detail?.message||'Музыкальный сервис недоступен. Повторите позже');error.status=result.status;error.detail=detail;throw error;}
    return result.data;
  }
  function mediaUrl(path){
    if(!/^\/api\/music\/tracks\/\d+\/stream\?ticket=[A-Za-z0-9_.%-]+$/.test(String(path||'')))throw new Error('Сервер вернул некорректную ссылку на аудио');
    return new URL('/proxy.php?_binary=1&_media=1&_p='+encodeURIComponent(path),location.origin).href;
  }
  function tracks(){
    let rows=s.tracks;
    if(s.filter==='favorites')rows=rows.filter(t=>t.favorite);
    if(s.playlist){const ids=s.playlists.find(p=>p.id===s.playlist)?.track_ids||[];rows=ids.map(id=>s.tracks.find(t=>t.id===id)).filter(Boolean);}
    const q=s.query.trim().toLocaleLowerCase();
    return q?rows.filter(t=>[t.title,t.artist,t.album].join(' ').toLocaleLowerCase().includes(q)):rows;
  }
  function row(t){return `<div class="xm-track ${s.current?.id===t.id?'xm-selected':''}" data-track="${t.id}"><button class="xm-track-play" data-xm="play" data-id="${t.id}"><img src="${ART}" alt="" loading="lazy"><span><strong>${esc(t.title)}</strong><small>${esc(t.artist||'Неизвестный исполнитель')}</small></span></button><button class="xm-icon ${t.favorite?'xm-active':''}" data-xm="favorite" data-id="${t.id}" aria-label="${t.favorite?'Убрать из избранного':'В избранное'}" aria-pressed="${!!t.favorite}">${icon('heart',!!t.favorite)}</button>${button('track-menu','Действия с треком','more',`data-id="${t.id}"`)}<time>${seconds(t.duration)}</time></div>`;}
  function renderLibrary(){
    root.querySelectorAll('.xm-tabs button').forEach(el=>el.setAttribute('aria-selected',String(el.dataset.xm==='filter-'+s.filter)));
    const list=tracks(), playlist=s.playlists.find(p=>p.id===s.playlist);
    $('xmPlaylistHeading').innerHTML=playlist?`<div class="xm-playlist-heading"><button class="xm-text" data-xm="back-playlists">Все плейлисты</button><h2>${esc(playlist.name)}</h2>${button('edit-playlist','Изменить плейлист','more',`data-id="${playlist.id}"`)}</div>`:'';
    $('xmLibraryMessage').innerHTML=s.loading?'<p class="xm-empty">Загружаю библиотеку…</p>':s.error?`<div class="xm-empty"><p>${esc(s.error)}</p><button class="xm-secondary" data-xm="retry">Повторить</button></div>`:'';
    if(s.filter==='playlists'&&!playlist){
      $('xmTracks').innerHTML=`<button class="xm-new-playlist" data-xm="new-playlist">${icon('plus')}Создать плейлист</button>`+s.playlists.map(p=>`<div class="xm-playlist-row"><button data-xm="open-playlist" data-id="${p.id}">${icon('queue')}<span><strong>${esc(p.name)}</strong><small>${p.track_ids.length} треков</small></span>${icon('chevron')}</button></div>`).join('');
      $('xmMore').hidden=true;
    }else{
      $('xmTracks').innerHTML=list.slice(0,s.limit).map(row).join('')||(!s.loading&&!s.error?`<div class="xm-empty"><h2>${s.query?'Ничего не найдено':s.filter==='favorites'?'Здесь будут любимые треки':playlist?'Добавьте треки в плейлист':'Ваша музыка — здесь'}</h2><p>${s.query?'Попробуйте другое название или исполнителя.':'Добавьте свои аудиофайлы кнопкой «+». MP3 и WAV подходят для всех устройств.'}</p></div>`:'');
      $('xmMore').hidden=list.length<=s.limit;
    }
    root.querySelector('[data-xm="play-all"]').disabled=!list.length;
    root.querySelector('[data-xm="shuffle-all"]').disabled=!list.length;
  }
  async function load(){
    if(s.loading)return;s.loading=true;s.error='';renderLibrary();
    try{const data=await request('library');s.tracks=data.tracks||[];s.playlists=data.playlists||[];s.maxUpload=data.max_upload_bytes||256*1024*1024;
      let next=data.next_offset;while(data.has_more&&Number.isInteger(next)){const page=await request('library?offset='+next);s.tracks.push(...(page.tracks||[]));if(!page.has_more||!Number.isInteger(page.next_offset)||page.next_offset<=next)break;next=page.next_offset;}s.loaded=true;
      const session=await request('session');if(!s.sessionOwned)s.shareSite=!!session.session?.share_site;
    }catch(error){s.error=error.message;}finally{s.loading=false;renderLibrary();renderPlayer();}
  }
  function deviceName(){return s.device==='local'?localLabel():s.device.slice(6);}
  function renderSharing(){
    const input=$('xmShareSite');input.checked=s.shareSite;input.disabled=s.shareSaving||s.transitioning;input.setAttribute('aria-busy',String(s.shareSaving));
    $('xmShareSiteText').textContent=s.shareSaving?'Сохраняю…':'На сайте';
  }
  function renderPlayer(){
    renderSharing();
    const t=s.current;root.classList.toggle('xm-has-track',!!t);if(!t){$('xmMini').hidden=true;return;}
    const playing=s.state==='playing';
    $('xmMini').hidden=false;$('xmMiniTitle').textContent=t.title;$('xmMiniDevice').textContent=deviceName();
    $('xmPlayerTitle').textContent=t.title;$('xmPlayerArtist').textContent=t.artist||'Неизвестный исполнитель';
    for(const id of ['xmMainPlay','xmMiniPlay']){const b=$(id);b.innerHTML=icon(playing?'pause':'play',!playing);b.setAttribute('aria-label',playing?'Приостановить':'Воспроизвести');}
    $('xmPlayerFavorite').innerHTML=icon('heart',!!t.favorite);$('xmPlayerFavorite').classList.toggle('xm-active',!!t.favorite);$('xmPlayerFavorite').setAttribute('aria-pressed',String(!!t.favorite));
    $('xmDeviceName').textContent=deviceName()+(s.device!=='local'&&s.output!=='default'?' · '+(s.outputs.find(o=>o.id===s.output)?.name||'Выбранный выход'):'');
    $('xmShuffle').classList.toggle('xm-active',s.shuffle);$('xmShuffle').setAttribute('aria-pressed',String(s.shuffle));
    $('xmRepeat').classList.toggle('xm-active',s.repeat!=='off');$('xmRepeat').setAttribute('aria-label','Повтор: '+({off:'выключен',all:'вся очередь',one:'один трек'}[s.repeat]));$('xmRepeat').dataset.repeat=s.repeat;
    $('xmVolume').value=s.volume;$('xmDownloadLabel').textContent=s.downloads.has(t.id)?'Сохранено в приложении':'Скачать трек';
    $('xmPlayerStatus').textContent=s.error||({loading:'Загрузка трека…',paused:'На паузе',ended:'Трек завершён',stopped:'Воспроизведение остановлено'}[s.state]||'');
    renderProgress();
  }
  function renderProgress(){
    const duration=Math.max(0,finite(s.duration||s.current?.duration));
    $('xmPosition').textContent=seconds(s.position);$('xmDuration').textContent=seconds(duration);
    if(document.activeElement!==$('xmSeek')){$('xmSeek').max=Math.max(duration,1);$('xmSeek').value=Math.min(s.position,duration);}
    $('xmSeek').style.setProperty('--xm-range',`${duration?Math.min(100,s.position/duration*100):0}%`);
    $('xmVolume').style.setProperty('--xm-range',s.volume+'%');
  }
  function playerOpen(open){
    s.playerOpen=open;$('xmPlayer').hidden=!open;document.body.classList.toggle('xm-player-open',open);
    if(open){renderPlayer();root.querySelector('.xm-player').focus();}else root.querySelector('[data-xm="expand"]')?.focus();
  }
  function dialog(title,content){$('xmDialogTitle').textContent=title;$('xmDialogBody').innerHTML=content;$('xmDialog').showModal();}
  function closeDialog(){$('xmDialog').close();}
  function notice(error){const text=error?.message||String(error);X.toast(text);return text;}
  const nativeSession = () => ({session_key:key,track_id:s.current?.id,device:'local',share_site:s.shareSite,share_discord:s.shareDiscord});
  function nativeQueue(){
    let list=s.queue.map(id=>s.tracks.find(t=>t.id===id)).filter(Boolean);
    const currentIndex=list.findIndex(t=>t.id===s.current?.id);
    const start=Math.max(0,Math.min(currentIndex-100,list.length-200));
    list=list.slice(start,start+200); // Keep the selected track and nearby history.
    if(s.shuffle){const current=list.find(t=>t.id===s.current?.id),rest=list.filter(t=>t!==current);for(let i=rest.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[rest[i],rest[j]]=[rest[j],rest[i]];}list=current?[current,...rest]:rest;}
    return list.map(t=>({trackId:t.id,title:t.title,artist:t.artist||''}));
  }
  let publication = null, sharingWrite = null, transition = Promise.resolve(), cancelBrowserStart = null;
  async function publish(takeover=false,options={}){
    const explicitShare=typeof options.shareSite==='boolean',mustWrite=takeover||explicitShare;
    // PC playback and queue progress belong to authenticated agent heartbeats.
    // A delayed browser snapshot must not restore the previous track/state.
    if(s.device!=='local'&&!explicitShare)return;
    if(s.transitioning&&!options.allowTransition&&!explicitShare)return;
    if(!s.current||(!takeover&&!s.sessionOwned)){if(explicitShare)throw new Error('Сначала запустите трек на этом устройстве');return;}
    // Background snapshots coalesce; explicit settings must get their own
    // acknowledged write after an in-flight snapshot, never inherit its result.
    while(publication){if(!mustWrite){await publication.catch(()=>{});return;}await publication.catch(()=>{});}
    if(!s.current||(!takeover&&!s.sessionOwned)){if(explicitShare)throw new Error('Управление изменилось. Запустите трек на этом устройстве и повторите');return;}
    s.publishing=true;
    const snapshot=s.device==='local'?{track_id:s.current.id,device:s.device,state:s.state,position:Math.max(0,finite(s.position)),share_discord:s.shareDiscord}:{};
    publication=request('session','POST',{session_key:key,takeover,...snapshot,share_site:explicitShare?options.shareSite:s.shareSite});
    try{await publication;s.sessionOwned=true;s.lastPublish=Date.now();if(explicitShare)s.shareSite=options.shareSite;}
    catch(error){
      if(error.status===409){s.sessionOwned=false;audio.pause();if(native())nativeSend('pause');s.state='paused';
        if(error.detail?.code==='transfer_requested'&&!native())await request('transfers/'+error.detail.transfer_id+'/ack','POST',{session_key:key,position:Math.max(0,finite(audio.currentTime))});
        s.error='Управление перешло на другое устройство. Нажмите воспроизведение, чтобы продолжить здесь.';renderPlayer();}
      if(mustWrite)throw error;
    }finally{s.publishing=false;publication=null;}
  }
  async function changeSiteSharing(){
    const desired=$('xmShareSite').checked;
    // The switch and native reporter always reflect the last confirmed value.
    if(s.shareSaving){renderSharing();return;}
    if(s.transitioning){renderSharing();X.toast('Дождитесь переключения устройства');return;}
    if(!s.sessionOwned){renderSharing();X.toast('Сначала запустите трек на этом устройстве');return;}
    if(desired===s.shareSite){renderSharing();return;}
    s.shareSaving=true;renderSharing();
    sharingWrite=(async()=>{
      try{await publish(false,{shareSite:desired});if(native()&&s.device==='local')nativeSend('session',{session:nativeSession()});}
      catch(error){notice(new Error('Не удалось изменить публикацию на сайте. '+error.message));}
      finally{s.shareSaving=false;renderSharing();}
    })();
    try{await sharingWrite;}finally{sharingWrite=null;}
  }
  async function control(action,extra={},device=s.device){
    if(!device.startsWith('agent:'))throw new Error('Выберите компьютер');
    const sent=await request('control','POST',{source_name:device.slice(6),action,output_id:s.output,volume:Math.round(s.volume),position_sec:s.position,...extra});
    const started=Date.now();
    while(Date.now()-started<25000){
      const check=await request('control/'+sent.command_id);
      if(check.status==='completed'){if(check.result?.ok===false)throw new Error(check.result.message||'Агент не выполнил команду');return check.result?.details||{};}
      if(['failed','cancelled'].includes(check.status))throw new Error(check.result?.message||'Команда не выполнена');
      await new Promise(resolve=>setTimeout(resolve,700));
    }
    throw new Error('ПК не ответил вовремя. Проверьте соединение с агентом');
  }
  function applyPc(data){
    if(data.track_id&&s.current?.id!==data.track_id){const t=s.tracks.find(t=>t.id===data.track_id);if(t)s.current=t;}
    s.state=['playing','paused','loading','stopped','ended','error'].includes(data.state)?data.state:'stopped';
    s.position=finite(data.position_sec);s.duration=finite(data.duration_sec)||s.current?.duration||0;s.volume=data.volume===undefined?s.volume:finite(data.volume);s.error=data.error||'';
    renderPlayer();
  }
  function startBrowserAudio(){
    const generation=s.playGeneration;let timer,cancel;
    // Call play inside the original gesture; a stalled platform decoder must
    // not leave the interface in Loading forever (including desktop WebKit).
    const started=audio.play();
    return Promise.race([started,new Promise(resolve=>{cancel=()=>{audio.pause();resolve();};cancelBrowserStart=cancel;}),new Promise((_,reject)=>{timer=setTimeout(()=>{
      if(generation===s.playGeneration){audio.pause();s.state='error';s.error='Браузер не запустил аудио. Повторите воспроизведение или выберите MP3/WAV';renderPlayer();}
      reject(new Error('Браузер не запустил аудио. Повторите воспроизведение или выберите MP3/WAV'));
    },12000);})]).finally(()=>{clearTimeout(timer);if(cancelBrowserStart===cancel)cancelBrowserStart=null;});
  }
  async function handoff(t,options={}){
    const start=Math.max(0,Math.min(s.queue.indexOf(t.id)-1000,s.queue.length-2000));
    const body={session_key:key,client_id:key,device:s.device,output_id:s.output,track_id:t.id,
      volume:Math.round(s.volume),autoplay:options.autoplay!==false,queue:s.queue.slice(start,start+2000),repeat_mode:s.repeat};
    if(!options.preservePosition)body.position=options.position||0;
    let result=await request('transfers','POST',body);const started=Date.now();
    while(result.status!=='ready'){
      if(result.status==='failed')throw new Error(result.detail||'Переключение не завершено');
      if(result.status!=='waiting'||!(/^[a-f0-9]{32}$/).test(result.transfer_id||''))throw new Error('Сервер не подтвердил переключение');
      if(Date.now()-started>31000)throw new Error('Устройство не подтвердило переключение. Повторите попытку');
      await new Promise(resolve=>setTimeout(resolve,600));result=await request('transfers/'+result.transfer_id);
    }
    if(result.session?.session_key!==key||result.session?.device!==body.device||result.session?.track_id!==t.id)throw new Error('Управление изменилось во время переключения. Повторите воспроизведение');
    return result;
  }
  async function play(t,options={}){
    if(!t)return;
    if(!s.enabled)throw new Error('Войдите в XASS, чтобы продолжить');
    const device=options.device||s.requestedDevice||s.device;
    if(device!=='local'&&(t.pc_supported===false||t.mime==='audio/mp4'))throw new Error('Для этого трека выберите iPhone или загрузите версию MP3/WAV для ПК');
    const generation=++s.playGeneration;
    s.requestedDevice=device;cancelBrowserStart?.();
    // Never overlap server handoffs. A superseded request must settle its lease
    // before the newest request can pause/report it and acquire the next one.
    const run=transition.catch(()=>{}).then(async()=>{
      if(generation!==s.playGeneration||!s.enabled)return;
      if(sharingWrite)await sharingWrite;
      if(generation!==s.playGeneration||!s.enabled)return;
      s.transitioning=true;renderSharing();
      try{await performPlay(t,{...options,device},generation);}
      finally{s.transitioning=false;if(generation===s.playGeneration)s.requestedDevice=null;renderSharing();}
    });
    transition=run;return run;
  }
  async function performPlay(t,options,generation){
    try{
      audio.pause();if(native())nativeSend('stop');
      if(s.current&&s.sessionOwned&&s.device==='local'){
        s.position=native()?s.position:finite(audio.currentTime);s.state='paused';
        await publish(false,{allowTransition:true});
      }
      if(generation!==s.playGeneration)return;
      if(s.device!==options.device){s.output='default';s.outputs=[];}
      s.device=options.device;s.current=t;s.sessionOwned=false;
      s.queue=options.keepQueue&&s.queue.length?s.queue:tracks().map(track=>track.id);if(!s.queue.includes(t.id))s.queue.unshift(t.id);
      s.position=finite(options.position);s.duration=finite(t.duration);s.state='loading';s.error='';s.lastPlayAt=Date.now();renderLibrary();renderPlayer();
      try{
        const result=await handoff(t,options);
        if(!s.enabled)return;
        s.sessionOwned=true;s.lastPublish=Date.now();s.position=finite(result.session.position);
        s.state=result.session.state||'loading';s.shareSite=!!result.session.share_site;
      }catch(error){if(!(native()&&s.downloads.has(t.id)&&navigator.onLine===false))throw error;}
      if(generation!==s.playGeneration)return;
      s.needsStart=options.autoplay===false;
      if(s.needsStart){s.state='paused';renderPlayer();return;}
      if(s.device!=='local'){renderPlayer();}
      else {
        const cached=native()&&s.downloads.has(t.id);
        const ticket=cached?null:await request(`tracks/${t.id}/ticket`,'POST',{purpose:'listen'});
        if(generation!==s.playGeneration)return;
        const url=cached?'':mediaUrl(ticket.path);
        if(native())nativeSend('play',{url,title:t.title,artist:t.artist||'',trackId:t.id,position:s.position,volume:s.volume,artwork:new URL(ART,location.origin).href,session:nativeSession(),queue:nativeQueue(),repeat:s.repeat});
        else {audio.src=url;audio.volume=s.volume/100;audio.currentTime=s.position;await startBrowserAudio();}
      }
      if(generation===s.playGeneration)setMediaSession();
    }catch(error){if(generation!==s.playGeneration)return;s.state='error';s.error=error.name==='NotAllowedError'?'Браузер ждёт нажатия. Нажмите «Воспроизвести» ещё раз.':error.name==='NotSupportedError'?'Этот браузер не поддерживает аудиоформат. Попробуйте MP3 или WAV':error.message;renderPlayer();await publish(false,{allowTransition:true});throw new Error(s.error);}
    renderPlayer();
  }
  async function toggle(){
    if(!s.current)return play(tracks()[0]);
    if(s.device==='local'&&!native()&&s.sessionOwned&&!s.needsStart&&s.state==='error'&&audio.src&&!audio.error){
      // Safari may require a second explicit tap after the ticket request.
      // Invoke play synchronously in that gesture, without fetching again.
      const resumed=startBrowserAudio();await resumed;s.error='';s.state='playing';await publish();renderPlayer();return;
    }
    if(s.state==='playing'){
      if(s.device!=='local')applyPc(await control('pause'));else if(native())nativeSend('pause');else audio.pause();
      s.state='paused';
    }else if(['loading','error','stopped','ended'].includes(s.state)||!s.sessionOwned||s.needsStart){
      return play(s.current,{keepQueue:true,position:s.state==='ended'?0:s.position});
    }else{
      if(s.device!=='local')applyPc(await control('resume'));else if(native())nativeSend('resume');else await startBrowserAudio();
    }
    renderPlayer();await publish();
  }
  async function step(direction,automatic=false){
    if(automatic&&s.device!=='local')return;
    if(native()&&s.device==='local'){if(!automatic)nativeSend(direction>0?'next':'previous');return;}
    const ids=s.queue.filter(id=>s.tracks.some(t=>t.id===id));if(!ids.length)return;
    if(automatic&&!s.sessionOwned)return;
    if(automatic&&s.repeat==='one')return play(s.current,{keepQueue:true,takeover:false});
    let index=ids.indexOf(s.current?.id);
    if(direction<0&&s.position>3)return seek(0);
    if(s.shuffle&&ids.length>1){const others=ids.filter(id=>id!==s.current?.id);return play(s.tracks.find(t=>t.id===others[Math.floor(Math.random()*others.length)]),{keepQueue:true,takeover:!automatic});}
    index+=direction;if(index<0)index=ids.length-1;
    if(index>=ids.length){if(automatic&&s.repeat==='off'){s.state='ended';renderPlayer();publish();return;}index=0;}
    return play(s.tracks.find(t=>t.id===ids[index]),{keepQueue:true,takeover:!automatic});
  }
  async function seek(position){
    const value=Math.max(0,Math.min(finite(position),s.duration||s.current?.duration||0));
    if(s.device!=='local')applyPc(await control('seek',{position_sec:value}));else if(native())nativeSend('seek',{position:value});else audio.currentTime=value;
    s.position=value;renderProgress();publish();
  }
  function setMediaSession(){
    if(native()||!('mediaSession' in navigator)||!s.current)return;
    try{navigator.mediaSession.metadata=new MediaMetadata({title:s.current.title,artist:s.current.artist||'',album:s.current.album||'XASS',artwork:[{src:new URL(ART,location.origin).href,sizes:'512x512',type:'image/png'}]});
      const actions={play:()=>s.state==='playing'?undefined:toggle(),pause:()=>s.state==='playing'?toggle():undefined,previoustrack:()=>step(-1),nexttrack:()=>step(1),seekto:e=>seek(e.seekTime),seekbackward:e=>seek(s.position-(e.seekOffset||10)),seekforward:e=>seek(s.position+(e.seekOffset||10))};
      Object.entries(actions).forEach(([name,handler])=>{try{navigator.mediaSession.setActionHandler(name,e=>Promise.resolve(handler(e)).catch(notice));}catch{}});
    }catch{}
  }
  async function favorite(id){const t=s.tracks.find(t=>t.id===id);if(!t)return;const data=await request(`tracks/${id}`,'PATCH',{favorite:!t.favorite});Object.assign(t,data.track);if(s.current?.id===id)s.current=t;renderLibrary();renderPlayer();}
  function trackMenu(id){
    const t=s.tracks.find(t=>t.id===id);if(!t)return;
    dialog(t.title,`<div class="xm-menu"><button data-xm="add-to-playlist" data-id="${id}">${icon('plus')}Добавить в плейлист</button><button data-xm="edit-track" data-id="${id}">${icon('queue')}Название и исполнитель</button><button data-xm="download" data-id="${id}">${icon('download')}Скачать файл</button><button data-xm="delete-track" data-id="${id}" class="xm-danger">${icon('trash')}Убрать из библиотеки</button></div>`);
  }
  function playlistEditor(id=null){
    const p=s.playlists.find(p=>p.id===id)||{name:'',track_ids:[]};
    dialog(id?'Изменить плейлист':'Новый плейлист',`<form id="xmPlaylistForm" data-id="${id||''}"><label class="xm-field">Название<input name="name" value="${esc(p.name)}" maxlength="160" required placeholder="Например, «В дороге»"></label><div class="xm-checklist">${s.tracks.map(t=>`<label><input type="checkbox" name="track" value="${t.id}" ${p.track_ids.includes(t.id)?'checked':''}><span>${esc(t.title)}<small>${esc(t.artist||'')}</small></span></label>`).join('')||'<p class="xm-muted">Сначала добавьте музыку в библиотеку.</p>'}</div><button class="xm-primary" type="submit">Сохранить плейлист</button>${id?`<button type="button" class="xm-text xm-danger" data-xm="delete-playlist" data-id="${id}">Удалить плейлист</button>`:''}</form>`);
  }
  async function devices(){
    const sources=(X.state.boot?.sources||[]).filter(p=>X.isPc?X.isPc(p):p.source_type==='PC_AGENT');
    dialog('Где слушать',`<div class="xm-menu"><button data-xm="choose-device" data-device="local">${icon('device')}<span>${localLabel()}</span>${s.device==='local'?icon('check'):''}</button>${sources.map(p=>`<button data-xm="choose-device" data-device="${esc('agent:'+p.source_name)}" ${p.is_online?'':'disabled'}>${icon('speaker')}<span>${esc(p.source_name)}<small>${p.is_online?'Windows · в сети':'Не в сети'}</small></span>${s.device==='agent:'+p.source_name?icon('check'):''}</button>`).join('')}${native()?`<button data-xm="airplay">${icon('volume')}AirPlay и Bluetooth</button>`:''}</div><div id="xmOutputs"></div>`);
    if(s.device!=='local')await outputs();
  }
  async function outputs(){
    const target=s.device,box=$('xmOutputs');if(!box)return;box.innerHTML='<p class="xm-muted">Получаю аудиовыходы Windows…</p>';
    try{const result=await control('outputs',{},target);if(s.device!==target||!$('xmOutputs'))return;s.outputs=result.outputs||[];
      $('xmOutputs').innerHTML=`<label class="xm-field">Аудиовыход<select id="xmOutput">${s.outputs.map(o=>`<option value="${esc(o.id)}" ${s.output===o.id?'selected':''}>${esc(o.name)}</option>`).join('')}</select></label><p class="xm-muted">Выбор меняет только звук XASS, не настройки всей Windows.</p>`;
    }catch(error){if($('xmOutputs'))$('xmOutputs').textContent=error.message;}
  }
  async function chooseDevice(value){
    if(value===s.device){if(value!=='local')await outputs();return;}
    const wasPlaying=['playing','loading'].includes(s.state);
    if(s.current)await play(s.current,{device:value,keepQueue:true,preservePosition:true,autoplay:wasPlaying});
    else{s.device=value;s.output='default';s.outputs=[];s.sessionOwned=false;renderPlayer();}
    await devices();
  }
  async function download(id){
    const t=s.tracks.find(t=>t.id===id);if(!t)return;
    const ticket=await request(`tracks/${id}/ticket`,'POST',{purpose:'download'}),url=mediaUrl(ticket.path);
    if(native()){nativeSend('download',{url,trackId:id,title:t.title,artist:t.artist||'',filename:t.filename||t.title,duration:t.duration});X.toast('Сохраняю трек в приложении…');return;}
    const a=document.createElement('a');a.href=url;a.download=t.filename||t.title;a.rel='noreferrer';document.body.append(a);a.click();a.remove();
    X.toast('Скачивание началось. Файл будет в загрузках браузера');
  }
  function base64(buffer){const bytes=new Uint8Array(buffer);let raw='';for(let i=0;i<bytes.length;i+=32768)raw+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(raw);}
  function uploadStatus(){
    const u=s.upload;$('xmUpload').hidden=!u;if(!u)return;
    const pct=Math.round(u.offset/u.file.size*100);
    $('xmUpload').innerHTML=`<div class="xm-upload"><strong>${esc(u.file.name)}</strong><span>${u.error?esc(u.error):u.offset>=u.file.size?'Проверяю аудиофайл…':pct+'%'}</span><progress max="${u.file.size}" value="${u.offset}"></progress>${u.error?'<button class="xm-text" data-xm="resume-upload">Повторить загрузку</button>':''}<button class="xm-text" data-xm="cancel-upload">${u.error?'Закрыть':'Отмена'}</button></div>`;
  }
  async function upload(file,resume=false){
    if(!resume){if(file.size>(s.maxUpload||256*1024*1024))throw new Error('Файл больше допустимого размера');if(!/\.(mp3|wav|flac|ogg|m4a|zip)$/i.test(file.name))throw new Error('Выберите MP3, WAV, FLAC, OGG, M4A или ZIP');s.upload={file,offset:0,id:null,error:'',cancelled:false};}
    const u=s.upload;if(!u)return;u.error='';uploadStatus();
    try{
      if(!u.id){const started=await request('uploads','POST',{filename:u.file.name,size:u.file.size});u.id=started.upload_id;u.offset=started.offset||0;u.chunk=Math.min(512*1024,started.chunk_bytes||512*1024);if(u.cancelled){await request('uploads/'+u.id,'DELETE');return;}}
      while(u.offset<u.file.size&&!u.cancelled){const bytes=await u.file.slice(u.offset,u.offset+u.chunk).arrayBuffer();const result=await request('uploads/'+u.id,'PUT',{offset:u.offset,data:base64(bytes)});if(result.offset<=u.offset)throw new Error('Сервер не подтвердил блок файла');u.offset=result.offset;uploadStatus();}
      if(u.cancelled)return;
      const result=await request('uploads/'+u.id+'/finish','POST');if(u.cancelled||s.upload!==u)return;
      const imported=result.tracks||(result.track?[result.track]:[]),ids=new Set(imported.map(t=>t.id));s.tracks=[...imported,...s.tracks.filter(t=>!ids.has(t.id))];s.upload=null;uploadStatus();renderLibrary();
      X.toast(result.archive?`Добавлено: ${result.added}. Уже в библиотеке: ${result.duplicates}.${result.errors?.length?' '+result.errors[0]:''}`:result.duplicate?'Этот трек уже есть в библиотеке':'Трек добавлен');
    }catch(error){if(u.cancelled)return;u.error=error.message;uploadStatus();}
  }
  async function action(name,el){
    const id=Number(el.dataset.id);
    if(name.startsWith('filter-')){s.filter=name.slice(7);s.playlist=null;s.limit=50;renderLibrary();return;}
    switch(name){
      case 'upload':if(s.upload){X.toast('Дождитесь загрузки или отмените её');return;}$('xmFile').click();break;
      case 'retry':await load();break;
      case 'more':s.limit+=50;renderLibrary();break;
      case 'play':await play(s.tracks.find(t=>t.id===id));break;
      case 'play-all':s.shuffle=false;await play(tracks()[0]);break;
      case 'shuffle-all':s.shuffle=true;await play(tracks()[Math.floor(Math.random()*tracks().length)]);break;
      case 'favorite':await favorite(id);break;
      case 'favorite-current':await favorite(s.current?.id);break;
      case 'track-menu':trackMenu(id);break;
      case 'expand':playerOpen(true);break;
      case 'collapse':playerOpen(false);break;
      case 'toggle':await toggle();break;
      case 'next':await step(1);break;
      case 'prev':await step(-1);break;
      case 'shuffle':s.shuffle=!s.shuffle;if(native()&&s.device==='local')nativeSend('queue',{queue:nativeQueue(),repeat:s.repeat});renderPlayer();break;
      case 'repeat':s.repeat={off:'all',all:'one',one:'off'}[s.repeat];if(native()&&s.device==='local')nativeSend('queue',{queue:nativeQueue(),repeat:s.repeat});renderPlayer();break;
      case 'close-dialog':closeDialog();break;
      case 'queue':dialog('Очередь воспроизведения',`<div class="xm-tracks">${s.queue.map(id=>s.tracks.find(t=>t.id===id)).filter(Boolean).map(row).join('')||'<p class="xm-muted">Выберите трек из библиотеки.</p>'}</div>`);break;
      case 'new-playlist':playlistEditor();break;
      case 'edit-playlist':playlistEditor(id);break;
      case 'open-playlist':s.playlist=id;s.limit=50;renderLibrary();break;
      case 'back-playlists':s.playlist=null;renderLibrary();break;
      case 'add-to-playlist':dialog('Добавить в плейлист',`<div class="xm-menu">${s.playlists.map(p=>`<button data-xm="playlist-add" data-id="${id}" data-playlist="${p.id}">${esc(p.name)}</button>`).join('')||'<p class="xm-muted">Сначала создайте плейлист на вкладке «Плейлисты».</p>'}</div>`);break;
      case 'playlist-add':{const p=s.playlists.find(p=>p.id===Number(el.dataset.playlist));const result=await request('playlists/'+p.id,'PUT',{name:p.name,track_ids:[...new Set([...p.track_ids,id])]});Object.assign(p,result.playlist);closeDialog();renderLibrary();X.toast('Добавлено в плейлист');break;}
      case 'delete-playlist':closeDialog();if(s.playerOpen)playerOpen(false);X.ask('Удалить плейлист? Сами треки останутся в библиотеке.',async confirmed=>{if(!confirmed)return;try{await request('playlists/'+id,'DELETE');s.playlists=s.playlists.filter(p=>p.id!==id);s.playlist=null;renderLibrary();}catch(e){notice(e);}});break;
      case 'edit-track':{const t=s.tracks.find(t=>t.id===id);dialog('Информация о треке',`<form id="xmTrackForm" data-id="${id}"><label class="xm-field">Название<input name="title" value="${esc(t.title)}" maxlength="240" required></label><label class="xm-field">Исполнитель<input name="artist" value="${esc(t.artist||'')}" maxlength="240"></label><button class="xm-primary" type="submit">Сохранить</button></form>`);break;}
      case 'delete-track':closeDialog();if(s.playerOpen)playerOpen(false);X.ask('Убрать трек из библиотеки? Файл останется на сервере для восстановления.',async confirmed=>{if(!confirmed)return;try{await request('tracks/'+id,'DELETE');if(s.current?.id===id){audio.pause();if(native())nativeSend('stop');if(s.device!=='local')await control('stop');s.current=null;playerOpen(false);renderPlayer();}s.tracks=s.tracks.filter(t=>t.id!==id);s.playlists.forEach(p=>p.track_ids=p.track_ids.filter(v=>v!==id));renderLibrary();}catch(e){notice(e);}});break;
      case 'devices':await devices();break;
      case 'choose-device':await chooseDevice(el.dataset.device);break;
      case 'airplay':nativeSend('route');break;
      case 'discord-help':dialog('Музыка в голосовом канале',`<ol class="xm-instructions"><li>Выберите подключённый Windows-ПК в плеере.</li><li>В аудиовыходах XASS выберите уже установленное виртуальное аудиоустройство.</li><li>В настройках голоса Discord выберите соответствующий виртуальный вход.</li><li>Подключитесь к голосовому каналу и запустите трек.</li></ol><p class="xm-muted">XASS не устанавливает виртуальные устройства и не входит в Discord автоматически. Если нужного выхода нет в списке, сначала настройте его в Windows.</p><button class="xm-primary" data-xm="devices">Выбрать устройство</button>`);break;
      case 'download':await download(id);break;
      case 'download-current':await download(s.current?.id);break;
      case 'resume-upload':await upload(null,true);break;
      case 'cancel-upload':{const u=s.upload;if(u){u.cancelled=true;s.upload=null;uploadStatus();if(u.id)await request('uploads/'+u.id,'DELETE');}break;}
    }
  }
  root.addEventListener('click',event=>{const el=event.target.closest('[data-xm]');if(!el||el.disabled)return;Promise.resolve(action(el.dataset.xm,el)).catch(notice);});
  root.addEventListener('submit',async event=>{
    if(!['xmPlaylistForm','xmTrackForm'].includes(event.target.id))return;event.preventDefault();const form=event.target,data=new FormData(form),id=Number(form.dataset.id);const submit=form.querySelector('[type="submit"]');submit.disabled=true;
    try{if(form.id==='xmPlaylistForm'){const result=await request('playlists'+(id?'/'+id:''),id?'PUT':'POST',{name:String(data.get('name')).trim(),track_ids:data.getAll('track').map(Number)});s.playlists=s.playlists.filter(p=>p.id!==result.playlist.id);s.playlists.unshift(result.playlist);}else{const result=await request('tracks/'+id,'PATCH',{title:String(data.get('title')).trim(),artist:String(data.get('artist')).trim()});Object.assign(s.tracks.find(t=>t.id===id),result.track);if(s.current?.id===id)s.current=result.track;renderPlayer();}closeDialog();renderLibrary();}catch(error){notice(error);}finally{submit.disabled=false;}
  });
  $('xmSearch').addEventListener('input',()=>{s.query=$('xmSearch').value;s.limit=50;renderLibrary();});
  $('xmFile').addEventListener('change',async()=>{const files=[...$('xmFile').files];$('xmFile').value='';for(const file of files){if(s.upload)break;try{await upload(file);}catch(e){notice(e);break;}}});
  $('xmSeek').addEventListener('input',()=>{$('xmPosition').textContent=seconds($('xmSeek').value);});
  $('xmSeek').addEventListener('change',()=>seek($('xmSeek').value).catch(notice));
  $('xmVolume').addEventListener('input',()=>{s.volume=Number($('xmVolume').value);if(s.device==='local'){if(native())nativeSend('volume',{volume:s.volume});else audio.volume=s.volume/100;}renderProgress();});
  $('xmVolume').addEventListener('change',()=>{if(s.device!=='local')control('volume',{volume:s.volume}).catch(notice);});
  root.addEventListener('change',event=>{if(event.target.id==='xmOutput'){s.output=event.target.value;renderPlayer();if(s.current)play(s.current,{keepQueue:true,position:s.position}).catch(notice);}});
  $('xmShareSite').addEventListener('change',changeSiteSharing);
  audio.addEventListener('loadedmetadata',()=>{if(s.device!=='local'||native())return;s.duration=finite(audio.duration);renderProgress();});
  audio.addEventListener('timeupdate',()=>{if(s.device!=='local'||native())return;s.position=finite(audio.currentTime);renderProgress();});
  audio.addEventListener('playing',()=>{if(s.device!=='local'||native()||!s.sessionOwned)return;s.state='playing';s.error='';renderPlayer();publish();});
  audio.addEventListener('pause',()=>{if(s.device!=='local'||native()||s.transitioning||s.state==='loading')return;if(!audio.ended)s.state='paused';renderPlayer();publish();});
  audio.addEventListener('ended',()=>{if(s.device==='local'&&!native())step(1,true).catch(notice);});
  audio.addEventListener('error',()=>{if(!audio.src||s.device!=='local'||native())return;s.state='error';s.error='Не удалось воспроизвести аудио. Для этого браузера попробуйте MP3 или WAV';renderPlayer();publish();});
  window.addEventListener('xass:native-audio',event=>{const d=event.detail||{};
    if(d.action==='download'){if(d.downloaded){s.downloads.add(Number(d.trackId));X.toast('Трек сохранён в приложении');}else if(d.error)notice(d.error);renderPlayer();return;}
    if(Array.isArray(d.downloads)){s.downloads=new Set(d.downloads.map(t=>Number(t.trackId)));return;}
    if(s.device!=='local'||s.transitioning||!s.enabled)return;
    if(d.trackId&&Number(d.trackId)!==s.current?.id){const track=s.tracks.find(t=>t.id===Number(d.trackId));if(!track)return;s.current=track;renderLibrary();}
    const ended=d.state==='ended'&&s.state!=='ended';if(['playing','paused','loading','stopped','ended','error'].includes(d.state))s.state=d.state;
    s.position=finite(d.position);s.duration=finite(d.duration)||s.duration;s.error=d.error||'';renderPlayer();if(ended)step(1,true).catch(notice);
  });
  document.addEventListener('keydown',e=>{
    if(!s.playerOpen||$('xmDialog').open)return;
    if(e.key==='Escape')playerOpen(false);
    if(e.key==='Tab'){const items=[...root.querySelectorAll('.xm-player button:not(:disabled),.xm-player input:not(:disabled)')],first=items[0],last=items[items.length-1];if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}}
  });
  document.addEventListener('xass:view',event=>{const visible=event.detail?.name==='music';$('xmMini').classList.toggle('xm-outside',!visible);if(visible&&!s.loaded)load();});
  document.addEventListener('xass:boot',()=>{if(document.getElementById('view-music').classList.contains('on')&&!s.loaded)load();});
  setInterval(async()=>{
    if(s.current&&s.sessionOwned&&!s.transitioning&&Date.now()-s.lastPublish>4000&&!(native()&&s.device==='local'))publish();
    if(s.device==='local'||!s.current||s.pcPolling||document.hidden||s.transitioning)return;
    s.pcPolling=true;const generation=s.playGeneration,device=s.device;
    try{const result=await request('players'),player=(result.players||[]).find(p=>p.source_name===device.slice(6));if(!player?.online)throw new Error('Компьютер не в сети');if(!player.available)throw new Error('Обновите Windows-агент для управления музыкой');const data=player.music_player||{};if(generation===s.playGeneration&&device===s.device){if(data.track_id!==s.current?.id&&Date.now()-s.lastPlayAt<15000)return;applyPc(data);}}
    catch(error){if(device===s.device){s.error=error.message;renderPlayer();}}finally{s.pcPolling=false;}
  },5000);
  window.addEventListener('pagehide',()=>{if(s.device==='local'&&s.sessionOwned&&s.current&&!native()){s.state=audio.paused?'paused':'playing';publish();}});
  for(const id of ['logoutBtn','logoutAllBtn'])$(id)?.addEventListener('click',()=>{s.enabled=false;s.playGeneration++;s.sessionOwned=false;cancelBrowserStart?.();audio.pause();audio.removeAttribute('src');audio.load();if(native())nativeSend('stop');});
  X.music={state:s,load,play,toggle,seek,chooseDevice,mediaUrl,seconds,base64,renderLibrary};
  renderLibrary();if(native())nativeSend('downloads');
})();
