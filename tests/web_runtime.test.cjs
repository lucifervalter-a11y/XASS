const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const transport = require('../assets/miniapp-network.js');

test('proxy envelope and HTTP errors retain useful status without leaking gateway HTML', async t => {
  t.mock.method(globalThis, 'fetch', async () => new Response(JSON.stringify({_s:403,_b:JSON.stringify({ok:false,detail:'Owner only'})})));
  const forbidden = await transport.json('/api', {envelope:true});
  assert.equal(forbidden.status,403); assert.equal(forbidden.data.detail,'Owner only');
  globalThis.fetch = async () => new Response('<html>upstream private address</html>',{status:502});
  const outage = await transport.json('/api');
  assert.equal(outage.status,502); assert.match(outage.data.detail,/временно недоступен/);
  assert.doesNotMatch(outage.data.detail,/private/);
});

test('deadline also covers a response body stalled after headers', async t => {
  t.mock.method(globalThis,'fetch', async (_url,{signal}) => ({
    status:200, headers:new Headers(),
    text:() => new Promise((resolve,reject) => signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')),{once:true})),
  }));
  await assert.rejects(transport.json('/api',{timeoutMs:20}), /не ответил вовремя/);
});

test('requests cannot cache authenticated responses', async t => {
  let options;
  t.mock.method(globalThis,'fetch',async (_url,opts)=>{options=opts;return Response.json({ok:true})});
  await transport.json('/api',{method:'POST',body:'{}',cache:'force-cache'});
  assert.equal(options.cache,'no-store');assert.equal(options.method,'POST');assert.equal(options.body,'{}');
});

function worker(response = new Response('shell',{headers:{'Content-Type':'text/html'}})) {
  const handlers={},puts=[];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../sw.js'),'utf8'), {
    URL, Response, console,
    self:{location:{origin:'https://xass.test'},addEventListener:(event,fn)=>handlers[event]=fn},
    fetch:async()=>response,
    caches:{open:async()=>({put:async(...args)=>puts.push(args),match:async()=>new Response('offline notice')})},
  });
  return {puts, async request(route,mode='navigate') {
    let promise;
    handlers.fetch({request:{method:'GET',url:'https://xass.test'+route,mode},respondWith:value=>promise=value});
    return promise ? await promise : undefined;
  }};
}

test('public profile navigation cannot replace the iPhone app shell',async()=>{
  const sw=worker();assert.equal(await sw.request('/profile.php'),undefined);assert.equal(sw.puts.length,0);
});
test('temporary server failure returns the public offline notice, not nginx error page',async()=>{
  const sw=worker(new Response('gateway',{status:503}));
  assert.equal(await (await sw.request('/miniapp.php?standalone=1')).text(),'offline notice');
  assert.equal(sw.puts.length,0);
});
test('demo pages and private downloads do not pollute caches',async()=>{
  const sw=worker();await sw.request('/miniapp.php?demo=1');assert.equal(sw.puts.length,0);
  assert.equal(await sw.request('/agent/update-manifest','cors'),undefined);
  assert.equal(await sw.request('/data/avatar-private.jpg','cors'),undefined);
  assert.equal(await sw.request('/proxy.php?_p=/api/pwa/config','cors'),undefined);
});
test('normal Mini App navigation is fresh and never persists authenticated HTML',async()=>{
  const sw=worker();const response=await sw.request('/miniapp.php?standalone=1');
  assert.equal(await response.text(),'shell');assert.equal(sw.puts.length,0);
});
