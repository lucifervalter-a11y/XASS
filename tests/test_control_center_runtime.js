'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'assets', 'miniapp-control-center.js'), 'utf8');
const functions = source.slice(source.indexOf('  function serviceHealth('), source.indexOf('\n  const toolDefinitions'));

function harness(boot) {
  const host = { innerHTML: '', querySelector: () => ({open: true}) };
  const home = { querySelector: () => host };
  const context = vm.createContext({
    X: {state: {boot}, demo: false}, $: id => id === 'view-home' ? home : null,
    esc: value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;'),
    REASONS: {offline: 'нет связи', high_cpu: 'высокая нагрузка процессора'},
  });
  vm.runInContext(functions, context);
  return {snapshot: context.overviewSnapshot, render() { context.renderHomeOverview(); return host.innerHTML; }};
}

const onlineSystem = () => Object.fromEntries(['backend', 'database', 'telegram_bot', 'public_site'].map(key => [key, {available: true, status: 'online'}]));

test('thousands of unread notifications do not become current problems or reduce service health', () => {
  const boot = {notifications_unread: 4159, system_status: onlineSystem(), sources: []};
  const h = harness(boot), result = h.snapshot(boot);
  assert.equal(result.active, 0);
  assert.equal(result.percent, 100);
  assert.equal(result.statuses[4].tone, 'unknown', 'zero agents is not a failed service');
  assert.doesNotMatch(h.render(), /4159|67%|требуют внимания/);
});

test('unknown site renders neutral coverage instead of offline or fake100percent', () => {
  for (const site of [undefined, {status: 'unknown', available: null}, {status: 'not_configured', available: null}]) {
    const boot = {notifications_unread: 4159, system_status: {...onlineSystem(), public_site: site}};
    const h = harness(boot), result = h.snapshot(boot), html = h.render();
    assert.equal(result.percent, null);
    assert.equal(result.checked, 3);
    assert.equal(result.statuses[3].tone, 'unknown');
    assert.match(html, />3\/4</);
    assert.doesNotMatch(html, /100%|Нет связи|Недоступно|OFFLINE|Активных проблем нет/);
  }
});

test('explicit missing Telegram configuration remains a checked service failure', () => {
  const boot = {
    system_status: {...onlineSystem(), telegram_bot: {status: 'not_configured', available: false}},
    health_summary: {total_services: 4, checked_services: 4, healthy_services: 3, unknown_services: 0, score: 75},
  };
  const h = harness(boot), result = h.snapshot(boot);
  assert.equal(result.checked, 4);
  assert.equal(result.percent, 75);
  assert.equal(result.active, 1);
  assert.equal(result.statuses[2].tone, 'error');
  assert.equal(result.statuses[2].text, 'Не настроен');
  assert.match(h.render(), /75%/);
  assert.doesNotMatch(h.render(), /Проверка не завершена/);
});

test('missing checks stay unknown and contradictory summary cannot invent healthy checks', () => {
  const boot = {health_summary: {total_services: 4, checked_services: 4, healthy_services: 4, score: 100}};
  const h = harness(boot), result = h.snapshot(boot);
  assert.equal(result.checked, 0);
  assert.equal(result.percent, null);
  assert.match(h.render(), />—</);
  assert.doesNotMatch(h.render(), /100%|Активных проблем нет/);
});

test('real failed service reduces percentage and remains actionable', () => {
  const boot = {system_status: {...onlineSystem(), public_site: {available: false, status: 'error', http_status: 503}}};
  const h = harness(boot), result = h.snapshot(boot);
  assert.equal(result.percent, 75);
  assert.equal(result.active, 1);
  assert.equal(result.statuses[3].tone, 'error');
  assert.equal(result.statuses[3].text, 'Ошибка');
  assert.match(h.render(), /75%/);
  assert.match(h.render(), /Текущие проблемы: 1/);
});

test('backend current attention wins over historical source flags, while review and updates stay separate', () => {
  const boot = {
    system_status: onlineSystem(), notifications_unread: 4159,
    sources: [{source_name: 'ПК', requires_attention: true, attention_reasons: ['offline']}],
    attention_summary: {active_count: 0, active_issues: [], review_count: 2, update_count: 1},
  };
  const h = harness(boot), result = h.snapshot(boot), html = h.render();
  assert.equal(result.active, 0);
  assert.equal(result.review, 2);
  assert.equal(result.updates, 1);
  assert.equal(result.percent, 100);
  assert.match(html, /Есть события к проверке/);
  assert.match(html, /События к проверке в уведомлениях: 2/);
  assert.match(html, /Доступны обновления для ПК: 1/);
  assert.doesNotMatch(html, /ПК: нет связи/);
});

test('legacy fallback deduplicates current reasons and excludes update-only warnings', () => {
  const boot = {system_status: onlineSystem(), sources: [
    {source_name: 'A', requires_attention: true, attention_reasons: ['update_available']},
    {source_name: 'B', requires_attention: true, attention_reasons: ['offline', 'offline', 'high_cpu', 'update_available']},
  ]};
  const result = harness(boot).snapshot(boot);
  assert.equal(result.active, 1);
  assert.equal(result.issues.length, 1);
  assert.match(result.issues[0].reason, /нет связи, высокая нагрузка/);
  assert.equal(result.percent, 100, 'device problems do not change service percentage');
});

test('status details escape API text, preserve expansion and bound long issue lists', () => {
  const boot = {system_status: onlineSystem(), attention_summary: {
    active_count: 8, active_issues: Array.from({length: 8}, () => ({source_name: '<img src=x onerror=alert(1)>', reason: '<script>bad</script>'})),
  }};
  const html = harness(boot).render();
  assert.match(html, /cc-health-details" open/);
  assert.match(html, /&lt;img/);
  assert.doesNotMatch(html, /<script>|<img/);
  assert.match(html, /Другие текущие проблемы: 2/);
  assert.equal((html.match(/<li>/g) || []).length, 7);
});
