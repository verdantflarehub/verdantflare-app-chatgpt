import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

function page() {
  const context = vm.createContext({ window: {}, document: { querySelector: () => ({}) }, AbortController });
  vm.runInContext(readFileSync(new URL('../dashboard/dashboard.mjs', import.meta.url), 'utf8'), context);
  return expression => vm.runInContext(expression, context);
}

test('new connection has no invented URL, membership or gateway balance', () => {
  const run = page();
  assert.equal(run('blank().url'), '');
  assert.equal(run('blank().used5h'), null);
  assert.equal(run('blank().used7d'), null);
  const html = run("editor='new'; form()");
  assert.match(html, /5 小时已用/);
  assert.match(html, /7 天已用/);
  assert.doesNotMatch(html, /Google|充值|赠金|原型|示例/);
});

test('membership unknown windows and gateway unit stay independent', () => {
  const run = page();
  run(`accounts=[accountView({account_id:'account',name:'Member',api_url:'https://gateway.example.invalid/v1',
    proxy:{mode:'direct'},status:'disabled',registration:{membership_tier:'Pro',used_5h_percent:'25',used_7d_percent:null},
    gateway_observation:{remaining:'12',unit:'Credits',planName:'Gateway pool'}})];`);
  const html = run('list()');
  assert.match(html, /Pro/);
  assert.match(html, /25%/);
  assert.match(html, /7 天用量<\/small><strong class="num">未登记/);
  assert.match(html, /12 Credits/);
  assert.match(html, /已停用/);
  assert.doesNotMatch(html, /USD|Gateway pool|示例/);
});

test('logs preserve units and separate budget from settlement', () => {
  const run = page();
  const html = run(`logs={accounts:[],summary:{task_count:1,budget:{amount:'5',unit:'Credits',unknown_count:0},
    settlement:{amount:'6',unit:'Credits',unknown_count:0}},items:[]}; logView()`);
  assert.match(html, /当前任务/);
  assert.match(html, /预算/);
  assert.match(html, /5 Credits/);
  assert.match(html, /6 Credits/);
  assert.match(html, /生成前估算/);
  assert.doesNotMatch(html, /USD|详情/);
});

test('v0.2 full-width layout, top breadcrumb, official brand logo and zero return buttons', () => {
  const index = readFileSync(new URL('../dashboard/index.html', import.meta.url), 'utf8');
  const css = readFileSync(new URL('../dashboard/dashboard.css', import.meta.url), 'utf8');
  const run = page();
  const formHtml = run("editor='new'; form()");
  assert.ok(index.indexOf('id="appBreadcrumb"') < index.indexOf('class="topline"'));
  assert.match(index, /viewBox="0 0 24 24"/);
  assert.doesNotMatch(index, /渠道管理<\/span>/);
  assert.match(css, /\.viewport\s*\{[^}]*max-width:\s*none/);
  assert.match(css, /\.viewport\s*\{[^}]*padding:\s*0/);
  assert.match(css, /\.g-form\s*\{[^}]*max-width:\s*none/);
  assert.doesNotMatch(formHtml, /data-cancel|>返回概览<|>取消</);
});

test('v0.3 1-20 slider and numeric concurrency control in overview and form', () => {
  const css = readFileSync(new URL('../dashboard/dashboard.css', import.meta.url), 'utf8');
  const run = page();
  assert.equal(run('blank().concurrentLimit'), 4);
  assert.equal(run('clampConcurrency(0)'), 1);
  assert.equal(run('clampConcurrency(25)'), 20);
  assert.equal(run('clampConcurrency(12)'), 12);
  const formHtml = run("editor='new'; form()");
  assert.match(formHtml, /同时出图数 \/ 最大并发（1～20）/);
  assert.match(formHtml, /type="range" class="concurrency-slider" min="1" max="20" step="1" value="4"/);
  assert.match(formHtml, /type="number" name="concurrent_limit" class="concurrency-num-input" min="1" max="20" step="1" value="4"/);
  run(`accounts=[accountView({account_id:'acc-1',name:'Main',api_url:'https://gateway.example.invalid/v1',
    proxy:{mode:'direct'},status:'active',registration:{concurrent_limit:12,membership_tier:'Pro'},gateway_observation:null})]; editor=null;`);
  const listHtml = run('list()');
  assert.match(listHtml, /出图并发：同时出 12 图（最大 20）/);
  assert.match(listHtml, /当前生效：同时出 12 图 · Image MCP 动态同步/);
  assert.match(listHtml, /data-concurrency-range="acc-1"/);
  assert.match(listHtml, /data-concurrency-number="acc-1"/);
  assert.match(css, /\.concurrency-tick\s*\{[^}]*white-space:\s*nowrap/);
});

