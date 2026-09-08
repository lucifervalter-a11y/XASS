const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname,'../assets/miniapp-music.js'),'utf8');

function runtime(api, options={}) {
  const nodes=new Map(),events={},requests=[],toasts=[],intervals=[];
  function node(id='') {return {id,innerHTML:'',textContent:'',value:0,hidden:false,dataset:{},style:{setProperty(){}},classList:{toggle(){},contains(){return false;}},handlers:{},addEventListener(name,fn){this.handlers[name]=fn;},setAttribute(){},removeAttribute(){},append(){},focus(){},click(){},close(){this.open=false;},showModal(){this.open=true;},querySelector(){return node();},querySelectorAll(){return[];},play(){return Promise.resolve();},pause(){},load(){}};}
  const get=id=>{if(!nodes.has(id))nodes.set(id,node(id));return nodes.get(id);};
  const X={state:{boot:{}},esc:value=>String(value??'').replace(/[&<>"']/g,x=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x])),toast:value=>toasts.push(value),ask:(_message,done)=>done(false),api:async(p,o)=>{requests.push({path:p,...o});return api?api(p,o):{status:200,data:{ok:true}};}};
  const window={XASS:X,addEventListener:(name,fn)=>{events[name]=fn;}};
  const document={getElementById:get,createElement:tag=>get(tag==='audio'?'audio':tag),body:node('body'),addEventListener(){},querySelectorAll(){return[];}};
  const context=vm.createContext({window,document,navigator:{userAgent:'QA'},location:{origin:'https://xass.example'},crypto:webcrypto,URL,URLSearchParams,FormData,Uint8Array,Promise,Number,Math,Date,setInterval(fn){intervals.push(fn);},setTimeout:options.fastTimers?(fn,ms)=>setTimeout(fn,Math.min(ms,10)):setTimeout,clearTimeout,btoa:raw=>Buffer.from(raw,'latin1').toString('base64')});
  vm.runInContext(source,context);
  return{music:X.music,X,window,nodes,events,requests,toasts,intervals};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('media URL accepts only exact ticketed library paths, never arbitrary origins or extra parameters',()=>{
  const {music}=runtime();const url=new URL(music.mediaUrl('/api/music/tracks/42/stream?ticket=opaque-test_ticket.abc'));
  assert.equal(url.origin,'https://xass.example');assert.equal(url.pathname,'/proxy.php');assert.equal(url.searchParams.get('_media'),'1');
  for(const unsafe of ['https://evil.example/audio.mp3','//evil.example','/api/mini/files','/api/music/tracks/42/stream?ticket=x&url=evil','/api/music/tracks/42/stream?ticket=x#fragment','/api/music/tracks/42/../stream?ticket=x'])assert.throws(()=>music.mediaUrl(unsafe));
});

test('chunk encoder round trips the full 512KiB payload without unsafe argument expansion',()=>{
  const {music}=runtime(),bytes=Uint8Array.from({length:512*1024},(_,i)=>i%256);
  assert.deepEqual(Buffer.from(music.base64(bytes.buffer),'base64'),Buffer.from(bytes));
});

test('library filtering and playlist order use actual metadata with escaped rendering',()=>{
  const {music,nodes}=runtime();music.state.tracks=[{id:1,title:'<script>first</script>',artist:'Alice',duration:62,favorite:true},{id:2,title:'Second',artist:'Bob',duration:75}];
  music.renderLibrary();assert.match(nodes.get('xmTracks').innerHTML,/&lt;script&gt;/);assert(!nodes.get('xmTracks').innerHTML.includes('<script>'));
  music.state.filter='favorites';music.renderLibrary();assert(!nodes.get('xmTracks').innerHTML.includes('Second'));
  music.state.filter='all';music.state.query='BOB';music.renderLibrary();assert(nodes.get('xmTracks').innerHTML.includes('Second'));assert(!nodes.get('xmTracks').innerHTML.includes('first'));
  music.state.query='';music.state.playlists=[{id:9,name:'Order',track_ids:[2,1]}];music.state.playlist=9;music.renderLibrary();const html=nodes.get('xmTracks').innerHTML;assert(html.indexOf('Second')<html.indexOf('first'));
});

test('cancelled delete confirmation never issues DELETE',async()=>{
  const {nodes,requests,music}=runtime();music.state.tracks=[{id:1,title:'Owned file'}];
  nodes.get('xassMusic').handlers.click({target:{closest:()=>({dataset:{xm:'delete-track',id:'1'}})}});await tick();
  assert.equal(requests.length,0);
});

test('unsupported Windows formats are refused before any API or player action',async()=>{
  const {music,requests}=runtime();music.state.device='agent:PC';
  await assert.rejects(music.play({id:1,title:'AAC',mime:'audio/mp4'}),/MP3/);
  await assert.rejects(music.play({id:2,title:'Opus',pc_supported:false}),/MP3/);
  assert.equal(requests.length,0);
});

test('rapid track changes serialize session claims and cannot start an old ticket',async()=>{
  let finishFirst;let sessionCount=0;
  const r=runtime(async(p,o)=>{
    if(p==='music/session'&&o.method==='POST'){if(++sessionCount===1)await new Promise(resolve=>{finishFirst=resolve;});return{status:200,data:{ok:true}};}
    const id=p.split('/')[2];return{status:200,data:{ok:true,path:`/api/music/tracks/${id}/stream?ticket=qa-ticket`}};
  });
  const one={id:1,title:'One',duration:10},two={id:2,title:'Two',duration:10};r.music.state.tracks=[one,two];
  const first=r.music.play(one),second=r.music.play(two);finishFirst();await Promise.all([first,second]);
  const tickets=r.requests.filter(q=>q.path.endsWith('/ticket'));assert.deepEqual(tickets.map(q=>q.path),['music/tracks/2/ticket']);assert.equal(sessionCount,2);assert.equal(r.music.state.current.id,2);
});

test('native download and playback events reach the listener and update actual UI state',()=>{
  const r=runtime();r.music.state.tracks=[{id:1,title:'One',duration:10},{id:2,title:'Two',duration:20}];r.music.state.current=r.music.state.tracks[0];
  const event=r.events['xass:native-audio'];assert.equal(typeof event,'function');
  // Exercise the exact JavaScript emitted by production Swift. A direct test
  // event could hide a document/window mismatch (these events do not bubble).
  const swift=fs.readFileSync(path.join(__dirname,'../ios/Sources/WebContainer.swift'),'utf8');
  const bridge=swift.match(/evaluateJavaScript\("([^"]+)" \+ json \+ "([^"]+)"/);
  assert(bridge,'Expected production native event emitter');assert.match(bridge[1],/^window\.dispatchEvent/);
  r.window.dispatchEvent=e=>r.events[e.type]?.(e);
  const send=detail=>vm.runInNewContext(bridge[1]+JSON.stringify(detail)+bridge[2],{window:r.window,CustomEvent:class{constructor(type,options){this.type=type;this.detail=options.detail;}}});
  send({action:'download',trackId:1,downloaded:true});assert(r.music.state.downloads.has(1));
  send({trackId:2,state:'playing',position:4,duration:20});assert.equal(r.music.state.current.id,2);assert.equal(r.music.state.position,4);assert.equal(r.nodes.get('xmPlayerTitle').textContent,'Two');
});

test('an unresolved media play promise times out and restores an actionable error state',async()=>{
  const r=runtime(async(p)=>({status:200,data:{ok:true,...(p.endsWith('/ticket')?{path:'/api/music/tracks/1/stream?ticket=test-ticket'}:{})}}),{fastTimers:true});
  r.nodes.get('audio').play=()=>new Promise(()=>{});const track={id:1,title:'Silent fixture',duration:10};r.music.state.tracks=[track];
  await assert.rejects(r.music.play(track),/Браузер не запустил/);assert.equal(r.music.state.state,'error');assert.match(r.nodes.get('xmPlayerStatus').textContent,/Повторите/);
});

test('native bridge sends session and a bounded queue containing tracks beyond the first 200',async()=>{
  const r=runtime(async(p)=>({status:200,data:{ok:true,...(p.endsWith('/ticket')?{path:'/api/music/tracks/250/stream?ticket=test-ticket'}:{})}}));
  const commands=[];r.window.XASS_NATIVE_AUDIO=true;r.window.webkit={messageHandlers:{xassAudio:{postMessage:message=>commands.push(message)}}};
  r.music.state.tracks=Array.from({length:300},(_,i)=>({id:i+1,title:'Трек '+(i+1),artist:'Исполнитель',duration:100}));r.music.state.queue=r.music.state.tracks.map(t=>t.id);
  await r.music.play(r.music.state.tracks[249],{keepQueue:true});const sent=commands.find(c=>c.action==='play');
  assert.equal(sent.queue.length,200);assert(sent.queue.some(t=>t.trackId===250));assert(sent.queue.some(t=>t.trackId===249));assert.equal(sent.session.track_id,250);assert(sent.session.session_key.length>=16);assert.equal(sent.repeat,'off');
  const swift=fs.readFileSync(path.join(__dirname,'../ios/Sources/AudioController.swift'),'utf8');
  for(const action of ['play','pause','resume','stop','seek','volume','download','downloads','route','session','queue','next','previous'])assert(swift.includes('"'+action+'"'),'native action contract: '+action);
});

test('playback timeout is bounded and there is no repeated PC status command in the timer',()=>{
  assert.match(source,/function startBrowserAudio\(/);assert.match(source,/12000\)/);
  assert.match(source,/request\('players'\)/);assert(!/control\('status'/.test(source));
});

test('site sharing stays confirmed OFF on 503 and never tells native to publish speculatively',async()=>{
  let finish;const commands=[];
  const r=runtime(()=>new Promise(resolve=>{finish=resolve;}));
  r.window.XASS_NATIVE_AUDIO=true;r.window.webkit={messageHandlers:{xassAudio:{postMessage:message=>commands.push(message)}}};
  r.music.state.current={id:1,title:'Fixture',duration:10};r.music.state.sessionOwned=true;
  const input=r.nodes.get('xmShareSite');input.checked=true;const changed=input.handlers.change();
  assert.equal(input.checked,false);assert.equal(input.disabled,true);assert.equal(r.music.state.shareSite,false);assert.equal(commands.length,0);
  r.events['xass:native-audio']({detail:{state:'paused',position:1,duration:10}});
  assert.equal(input.checked,false);assert.equal(input.disabled,true,'a playback render must not unlock the pending switch');
  finish({status:503,data:{detail:'Сервис временно недоступен'}});await changed;
  assert.equal(input.checked,false);assert.equal(input.disabled,false);assert.equal(r.music.state.shareSite,false);assert.equal(commands.length,0);
  assert.match(r.toasts.at(-1),/Не удалось изменить публикацию.*Сервис временно недоступен/);
  assert.equal(r.requests.length,1);assert.equal(r.requests[0].body.share_site,true);
});

test('network failure preserves confirmed ON when disabling and allows a later acknowledged retry',async()=>{
  let failing=true;const commands=[];
  const r=runtime(async()=>{if(failing)throw new Error('Network unavailable');return{status:200,data:{ok:true}};});
  r.window.XASS_NATIVE_AUDIO=true;r.window.webkit={messageHandlers:{xassAudio:{postMessage:message=>commands.push(message)}}};
  Object.assign(r.music.state,{current:{id:1,title:'Fixture'},sessionOwned:true,shareSite:true});
  const input=r.nodes.get('xmShareSite');input.checked=false;await input.handlers.change();
  assert.equal(input.checked,true);assert.equal(input.disabled,false);assert.equal(r.music.state.shareSite,true);assert.equal(commands.length,0);assert.match(r.toasts.at(-1),/Network unavailable/);
  failing=false;input.checked=false;await input.handlers.change();
  assert.equal(input.checked,false);assert.equal(input.disabled,false);assert.equal(r.music.state.shareSite,false);
  assert.equal(commands.length,1);assert.equal(commands[0].action,'session');assert.equal(commands[0].session.share_site,false);
});

test('a sharing write survives a background snapshot, ignores double-toggle and reaches the next track claim',async()=>{
  const pending=[];let inFlight=0,maxInFlight=0;
  const r=runtime(async(p,o)=>{
    if(p==='music/session'&&o.method==='POST'){
      inFlight++;maxInFlight=Math.max(maxInFlight,inFlight);
      await new Promise(resolve=>pending.push(resolve));inFlight--;
      return{status:200,data:{ok:true}};
    }
    return{status:200,data:{ok:true,path:'/api/music/tracks/2/stream?ticket=qa-ticket'}};
  });
  const one={id:1,title:'One',duration:10},two={id:2,title:'Two',duration:10};
  Object.assign(r.music.state,{current:one,tracks:[one,two],sessionOwned:true});
  r.nodes.get('audio').handlers.playing();
  const input=r.nodes.get('xmShareSite');input.checked=true;const changed=input.handlers.change();
  input.checked=true;await input.handlers.change();
  const nextTrack=r.music.play(two);
  assert.equal(r.requests.length,1);assert.equal(input.checked,false);assert.equal(input.disabled,true);
  pending.shift()();await tick();
  const sessions=()=>r.requests.filter(q=>q.path==='music/session');
  assert.equal(sessions().length,2);assert.equal(sessions()[1].body.share_site,true);assert.equal(sessions()[1].body.takeover,false);
  assert.equal(input.checked,false);assert.equal(r.music.state.shareSite,false);
  // The periodic snapshot must coalesce rather than overwrite the explicit write.
  r.music.state.lastPublish=0;
  await Promise.all([r.intervals[0](),r.intervals[0](),r.intervals[0]()]);
  assert.equal(sessions().length,2);
  pending.shift()();await changed;await tick();
  assert.equal(input.checked,true);assert.equal(input.disabled,false);assert.equal(sessions().length,3);
  assert.equal(sessions()[2].body.share_site,true,'the next track must use the committed sharing value');
  pending.shift()();await nextTrack;
  assert.equal(maxInFlight,1);assert.equal(sessions().length,3);assert.equal(r.music.state.current.id,2);
});

test('losing session ownership while waiting prevents the queued sharing write',async()=>{
  let finish;const r=runtime(()=>new Promise(resolve=>{finish=resolve;}));
  Object.assign(r.music.state,{current:{id:1,title:'Fixture'},sessionOwned:true});
  r.nodes.get('audio').handlers.playing();
  const input=r.nodes.get('xmShareSite');input.checked=true;const changed=input.handlers.change();
  finish({status:409,data:{detail:'Другое устройство'}});await changed;
  assert.equal(r.requests.length,1);assert.equal(r.music.state.sessionOwned,false);assert.equal(input.checked,false);assert.equal(input.disabled,false);
  assert.match(r.toasts.at(-1),/Управление изменилось/);
});

test('fullscreen player escapes retained view transforms and keeps its background and notices',()=>{
  const css=fs.readFileSync(path.join(__dirname,'../assets/miniapp-music.css'),'utf8');
  assert.match(css,/\.xm-player-open #view-music\{animation:none;transform:none\}/);
  assert.match(css,/\.xm-player\{height:max-content\}/);
  assert.match(css,/\.xm-player-open #toast\{z-index:140\}/);
});
