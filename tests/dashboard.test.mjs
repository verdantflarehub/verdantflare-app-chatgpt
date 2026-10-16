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
  assert.match(html, /5 小时已用比例/);
  assert.match(html, /7 天已用比例/);
  assert.doesNotMatch(html, /Google|充值|赠金|原型|示例/);
});

test('membership unknown windows and gateway unit stay independent', () => {
  const run = page();
  run(`accounts=[accountView({account_id:'account',name:'Member',api_url:'https://gateway.example.invalid/v1',
    proxy:{mode:'direct'},status:'disabled',registration:{membership_tier:'Pro',used_5h_percent:'25',used_7d_percent:null},
    gateway_observation:{remaining:'12',unit:'Credits',planName:'Gateway pool'}})];`);
  const html = run('list()');
  assert.match(html, /Pro/);
  assert.match(html, /已用 25%/);
  assert.match(html, /7 天用量<\/small><strong class="num">未知/);
  assert.match(html, /12 Credits/);
  assert.match(html, /账号已停用/);
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
