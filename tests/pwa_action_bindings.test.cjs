'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../miniapp.php'), 'utf8');
const start = html.indexOf('async function passkeyAuthenticate(');
const end = html.indexOf('\n}', html.indexOf('async function passkeyAction(', start)) + 2;
const source = html.slice(start, end);
const plain = value => JSON.parse(JSON.stringify(value));

function harness() {
  const calls = [];
  const state = {pwa: true, boot: {passkeys: [{id: 1}], sources: [{id: 7, source_name: 'Мой:ПК'}],
    scenarios: [{id: 'daily', actions: ['lock_all'], devices: ['Мой:ПК'], delay_sec: 5}]}};
  const context = vm.createContext({state, window: {PublicKeyCredential: true},
    navigator: {credentials: {get: async () => ({id: 'fixture'})}},
    prepareCredentialOptions: x => x, serializeCredential: x => x,
    pwaApi: async (route, options) => {
      calls.push([route, plain(options.body)]);
      return {status: 200, data: {ok: true, transaction: 'transaction', options: {}, action_proof: 'xpa_fixture'}};
    },
  });
  vm.runInContext(source, context);
  return {context, state, calls};
}

test('browser sends immutable parameters only in the challenge request', async () => {
  const h = harness();
  const params = {source_id: 7, command: 'file_delete', payload: {root: 'documents', path: 'мой.txt'}};
  const proof = h.context.passkeyAction('agent:file_delete:Мой:ПК', params);
  params.payload.path = 'other.txt';
  assert.equal(await proof, 'xpa_fixture');
  assert.equal(h.calls[0][1].binding.payload.path, 'мой.txt');
  assert.deepEqual(Object.keys(h.calls[1][1]).sort(), ['credential', 'transaction']);
});

test('legacy command buttons include ID and normalized power delay', async () => {
  const h = harness();
  await h.context.passkeyAction('agent:shutdown:Мой:ПК');
  assert.deepEqual(h.calls[0][1].binding, {source_id: 7, command: 'shutdown', payload: {delay_sec: 0}});
});

test('file and delay normalization mirrors server binding, no path traversal stripping', () => {
  const h = harness();
  assert.deepEqual(plain(h.context.agentActionBinding('Мой:ПК', 'file_delete', {root: ' Documents ', path: '/a\\./б.txt/'})),
    {source_id: 7, command: 'file_delete', payload: {root: 'documents', path: 'a/б.txt'}});
  assert.equal(h.context.agentActionBinding('Мой:ПК', 'file_delete', {root: 'documents', path: '../secret'}).payload.path, '../secret');
  assert.equal(h.context.agentActionBinding('Мой:ПК', 'reboot', {delay_sec: 12}).payload.delay_sec, 12);
});

test('scenario confirmation binds current actions, target devices and delay', async () => {
  const h = harness();
  await h.context.passkeyAction('scenario:daily');
  assert.deepEqual(h.calls[0][1].binding, {scenario_id: 'daily', actions: ['lock_all'], devices: ['Мой:ПК'], delay_sec: 5});
});

test('detach and storage explicit binding is preserved; parameterless operations use empty object', async () => {
  const h = harness();
  const binding = {source_id: 7, confirm_name: 'Мой:ПК'};
  await h.context.passkeyAction('agent:detach:7:Мой:ПК', binding);
  assert.deepEqual(h.calls[0][1].binding, binding);
  h.calls.length = 0;
  const storage = {source_name: 'Мой:ПК', sha256: 'a'.repeat(64)};
  await h.context.passkeyAction('music:evict:1:' + storage.sha256, storage);
  assert.deepEqual(h.calls[0][1].binding, storage);
  h.calls.length = 0;
  await h.context.passkeyAction('server:restart');
  assert.deepEqual(h.calls[0][1].binding, {});
});

test('missing/stale target fails before prompting; Telegram is unchanged', async () => {
  const h = harness();
  await assert.rejects(h.context.passkeyAction('agent:lock:missing'), /Обновите/);
  await assert.rejects(h.context.passkeyAction('scenario:missing'), /Обновите/);
  assert.equal(h.calls.length, 0);
  h.state.pwa = false;
  assert.equal(await h.context.passkeyAction('agent:lock:missing'), '');
  assert.equal(h.calls.length, 0);
});
