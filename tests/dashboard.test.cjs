const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const source = fs.readFileSync(require('node:path').join(__dirname, '../app/static/app_v2.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function setup() {
  const dom = new JSDOM('<div id="app"></div>', {runScripts:'outside-only', url:'https://example.test'});
  dom.window.fetch = async () => ({status:401, ok:false, json:async()=>({})});
  dom.window.alert = () => {};
  dom.window.confirm = () => true;
  dom.window.eval(source);
  return dom;
}
const position = {ticker:'TEST', market:{title:'Test'}, position:'10', direction:'YES', quantity:'10', held_bid:'.40', avg_entry:'.30', pnl_dollars:'1', pnl_percent:'33.3', stop:null};
const state = p => ({ws_connected:true, execution_enabled:true, kalshi_configured:true, environment:'demo', positions:[p], entry_plans:[], events:[]});

test('blank trigger is rejected instead of becoming zero', async () => {
  const dom = setup(); await settle();
  assert.throws(() => dom.window.eval("ruleToBackend('stop_loss_dollars', '')"), /trigger value/);
  dom.window.close();
});

test('position edits survive refresh while live prices keep updating', async () => {
  const dom = setup(); await settle(); const w = dom.window;
  w.buildShell(); w.render(state(position));
  const input = w.document.querySelector('.stopValue');
  input.value = '25'; input.dispatchEvent(new w.Event('input', {bubbles:true}));
  w.render(state({...position, held_bid:'.45'}), true);
  assert.equal(w.document.querySelector('.stopValue'), input);
  assert.equal(input.value, '25');
  assert.equal(w.document.querySelector('.price').textContent, '45.0¢');
  dom.window.close();
});

test('double submit arms a single entry and shows success', async () => {
  const dom = setup(); await settle(); const w = dom.window;
  w.buildShell(); w.render(state(position));
  w.document.getElementById('entryTicker').value = 'TEST';
  w.document.getElementById('entryValue').value = '40';
  let submits = 0, release;
  w.fetch = async path => {
    if (path === '/api/entries') { submits++; await new Promise(r => release = r); }
    return {status:200, ok:true, json:async()=>path === '/api/state' ? state(position) : {plan_id:'123', status:'waiting_entry'}};
  };
  const first = w.createEntry(); const second = w.createEntry(); await settle();
  assert.equal(submits, 1);
  release(); await Promise.all([first, second]);
  assert.match(w.document.getElementById('entryFeedback').textContent, /armed/i);
  dom.window.close();
});

test('refresh requests do not overlap and failures are visible', async () => {
  const dom = setup(); await settle(); const w = dom.window;
  w.buildShell(); let calls = 0, release;
  w.fetch = async () => { calls++; await new Promise(r => release = r); throw new Error('Network failed'); };
  const first = w.refresh(); const second = w.refresh(); await settle();
  assert.equal(calls, 1); release(); await Promise.all([first, second]);
  assert.match(w.document.getElementById('connectionNotice').textContent, /Network failed/);
  dom.window.close();
});

test('disarming uses the saved rule even when the draft is blank', async () => {
  const dom = setup(); await settle(); const w = dom.window;
  const p = {...position, stop:{armed:true, trigger_mode:'pnl_dollars', operator:'lte', trigger:'-5', slippage:'.03'}};
  w.buildShell(); w.render(state(p));
  w.document.querySelector('.stopValue').value = '';
  let sent;
  w.fetch = async (path, opts) => {
    if (opts?.method === 'PUT') sent = JSON.parse(opts.body);
    return {status:200, ok:true, json:async()=>state({...p, stop:{...p.stop, armed:false}})};
  };
  await w.saveStop(0, false);
  assert.equal(sent?.armed, false);
  assert.equal(sent.trigger_value, -5);
  dom.window.close();
});

test('editing during stop submission preserves the newer draft', async () => {
  const dom = setup(); await settle(); const w = dom.window;
  w.buildShell(); w.render(state(position));
  const input = w.document.querySelector('.stopValue');
  input.value = '25'; input.dispatchEvent(new w.Event('input', {bubbles:true}));
  let release;
  const saved = {...position, stop:{armed:true, trigger_mode:'price', operator:'lte', trigger:'.25', slippage:'.03'}};
  w.fetch = async (path, opts) => {
    if (opts?.method === 'PUT') await new Promise(r => release = r);
    return {status:200, ok:true, json:async()=>state(saved)};
  };
  const pending = w.saveStop(0, true); await settle();
  input.value = '30'; input.dispatchEvent(new w.Event('input', {bubbles:true}));
  release(); await pending; w.render(state(saved));
  assert.equal(input.value, '30');
  dom.window.close();
});
