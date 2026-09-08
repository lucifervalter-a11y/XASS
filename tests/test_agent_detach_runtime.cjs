'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../assets/miniapp-control-center.js'), 'utf8');
const handler = source.slice(source.indexOf('  async function detachAgent('), source.indexOf('\n  async function runAgentCommand('));
const render = source.slice(source.indexOf('  function agentDetailHtml('), source.indexOf('\n  function bindAgentDetail('));
function harness(options = {}) {
  const item = {id: 8, source_name: 'ПК <дом>', is_online: false}, other = {id: 9, source_name: 'Other'};
  const calls = [], messages = [], status = {textContent: ''}, button = {disabled: false};
  const X = {demo: false, state: {boot: {user: {is_owner: true}, sources: [item, other]}},
    toast: message => messages.push(message), passkeyAction: async purpose => { calls.push(['proof', purpose]); return 'proof'; },
    loadBoot: async () => { calls.push(['refresh']); }, age: () => '1 мин', valueText: () => '—', ...options.X};
  const context = vm.createContext({X, pendingCommands: new Map(), ui: {activeAgent: item.source_name}, $: () => status,
    confirmAction: async text => { calls.push(['confirm', text]); return options.confirm ?? true; },
    ccApi: options.api || (async (path, body) => { calls.push(['api', path, body]); return {data: {ok: true, detached: true}}; }),
    closeAgent: () => calls.push(['close']), ensureAgent() {}, renderCompactAgents() {},
    REASONS: {}, COMMAND_LABELS: {}, agentState: () => ['offline', 'OFFLINE'], agentIcon: () => '', actionIcon: () => '',
    esc: value => String(value).replaceAll('<', '&lt;').replaceAll('>', '&gt;'),
  });
  vm.runInContext(handler + render, context);
  return {context, X, item, other, button, calls, messages, status,
    run: () => context.detachAgent(item, button), html: () => context.agentDetailHtml(item)};
}
test('offline owner can find detach, source text escaped, guest cannot see it', () => {
  const h = harness();
  assert.match(h.html(), /id="ccAgentDetach">Отвязать агент/);
  assert.doesNotMatch(h.html(), /ПК <дом>/);
  h.X.state.boot.user.is_owner = false;
  assert.doesNotMatch(h.html(), /ccAgentDetach/);
});
test('cancel confirmation leaves source, credential request and UI untouched', async () => {
  const h = harness({confirm: false}); await h.run();
  assert.equal(h.calls.length, 1); assert.equal(h.X.state.boot.sources.length, 2);
  assert.equal(h.button.disabled, false); assert.equal(h.context.pendingCommands.size, 0);
});
test('non-owner, demonstration and missing immutable id never send a delete', async () => {
  for (const mode of ['guest', 'demo', 'missing-id']) {
    const h = harness();
    if (mode === 'guest') h.X.state.boot.user.is_owner = false;
    if (mode === 'demo') h.X.demo = true;
    if (mode === 'missing-id') delete h.item.id;
    await h.run(); assert.equal(h.calls.length, 0); assert.equal(h.X.state.boot.sources.length, 2);
  }
});
test('successful delete binds proof and confirmation to identity, preserves other source', async () => {
  const h = harness(); await h.run();
  assert.equal(h.calls[1][1], 'agent:detach:8:ПК <дом>');
  const api = h.calls.find(x => x[0] === 'api');
  assert.equal(api[1], 'agents/' + encodeURIComponent(h.item.source_name));
  assert.equal(api[2].method, 'DELETE');
  assert.equal(api[2].body.source_id, 8); assert.equal(api[2].body.confirm_name, h.item.source_name);
  assert.equal(api[2].body.action_proof, 'proof');
  assert.equal(h.X.state.boot.sources.length, 1); assert.equal(h.X.state.boot.sources[0].id, 9);
  assert.match(h.messages[0], /архивы сохранены/); assert.equal(h.button.disabled, false);
});
test('error and cancelled passkey retain source, expose retry feedback and unlock button', async () => {
  for (const mode of ['network', 'proof', 'server']) {
    const h = harness({api: async () => {if (mode === 'network') throw new Error('Нет связи'); return {data: {detail: 'Обновите список'}};}});
    if (mode === 'proof') h.X.passkeyAction = async () => {throw Object.assign(new Error('cancelled'), {name: 'NotAllowedError'});};
    await h.run(); assert.equal(h.X.state.boot.sources.length, 2); assert.equal(h.button.disabled, false);
    assert.notEqual(h.status.textContent, ''); assert.equal(h.context.pendingCommands.size, 0);
    assert(!h.calls.some(x => x[0] === 'close'));
  }
});
test('concurrent duplicate click sends only one delete', async () => {
  let finish, started;
  const ready = new Promise(resolve => {started = resolve;});
  const h = harness({api: async () => new Promise(resolve => {finish = resolve; started();})});
  const first = h.run(); await ready;
  await h.run(); assert.equal(h.calls.filter(x => x[0] === 'confirm').length, 1);
  finish({data: {ok: true, detached: true}}); await first;
  assert.equal(h.X.state.boot.sources.length, 1);
});
test('failed refresh after confirmed deletion does not claim deletion failed', async () => {
  const h = harness(); h.X.loadBoot = async () => {throw new Error('offline');}; await h.run();
  assert.equal(h.X.state.boot.sources.length, 1);
  assert(h.messages.every(message => !message.includes('Не удалось отвязать')));
});
