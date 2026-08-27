const app = document.getElementById('app');
let timer = null;
let lastState = null;
let positionLockUntil = 0;

const cents = v => v == null ? '—' : (Number(v) * 100).toFixed(1) + '¢';
const money = v => v == null ? '—' : (Number(v) >= 0 ? '+' : '') + '$' + Number(v).toFixed(2);
const pct = v => v == null ? '—' : (Number(v) >= 0 ? '+' : '') + Number(v).toFixed(1) + '%';
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({
  '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
}[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: {'Content-Type':'application/json', ...(opts.headers || {})}
  });
  if (r.status === 401) throw new Error('AUTH');
  const x = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(x.detail || 'Request failed');
  return x;
}

async function login() {
  try {
    await api('/api/login', {
      method:'POST',
      body:JSON.stringify({password:document.getElementById('pw').value})
    });
    buildShell();
    await refresh(true);
    start();
  } catch (e) {
    document.getElementById('loginerr').textContent =
      e.message === 'AUTH' ? 'Wrong password' : e.message;
  }
}

function ruleOptions(selected = 'price_below', context = 'exit') {
  const labels = context === 'entry' ? {
    price_below:'Enter when price falls to',
    price_above:'Enter when price rises to',
    take_profit_dollars:'Add when current profit reaches',
    stop_loss_dollars:'Add when current loss reaches',
    take_profit_percent:'Add when current profit reaches (%)',
    stop_loss_percent:'Add when current loss reaches (%)'
  } : {
    price_below:'Exit if price falls to',
    price_above:'Exit if price rises to',
    take_profit_dollars:'Take profit at',
    stop_loss_dollars:'Stop loss at',
    take_profit_percent:'Take profit at (%)',
    stop_loss_percent:'Stop loss at (%)'
  };
  return Object.entries(labels)
    .map(([v,l]) => `<option value="${v}" ${v===selected?'selected':''}>${l}</option>`)
    .join('');
}

function ruleToBackend(rule, rawValue) {
  const value = Math.abs(Number(rawValue));
  if (!Number.isFinite(value)) throw new Error('Enter a trigger value');
  if (rule === 'price_below') return {mode:'price', operator:'lte', value};
  if (rule === 'price_above') return {mode:'price', operator:'gte', value};
  if (rule === 'take_profit_dollars') return {mode:'pnl_dollars', operator:'gte', value};
  if (rule === 'stop_loss_dollars') return {mode:'pnl_dollars', operator:'lte', value:-value};
  if (rule === 'take_profit_percent') return {mode:'pnl_percent', operator:'gte', value};
  if (rule === 'stop_loss_percent') return {mode:'pnl_percent', operator:'lte', value:-value};
  throw new Error('Unknown trigger rule');
}

function backendToRule(mode, operator) {
  if (mode === 'price') return operator === 'gte' ? 'price_above' : 'price_below';
  if (mode === 'pnl_dollars') return operator === 'gte' ? 'take_profit_dollars' : 'stop_loss_dollars';
  return operator === 'gte' ? 'take_profit_percent' : 'stop_loss_percent';
}

function ruleDisplayValue(mode, trigger) {
  if (trigger == null) return '';
  if (mode === 'price') return (Number(trigger) * 100).toFixed(1);
  return Math.abs(Number(trigger)).toFixed(mode === 'pnl_dollars' ? 2 : 1);
}

function ruleSentence(rule, rawValue, context = 'exit') {
  const v = Math.abs(Number(rawValue));
  if (!Number.isFinite(v)) return '';
  if (rule === 'price_below') return `${context==='entry'?'Enter':'Exit'} when price is ${v.toFixed(1)}¢ or lower.`;
  if (rule === 'price_above') return `${context==='entry'?'Enter':'Exit'} when price is ${v.toFixed(1)}¢ or higher.`;
  if (rule === 'take_profit_dollars') return `${context==='entry'?'Add to position':'Take profit'} when profit reaches +$${v.toFixed(2)}.`;
  if (rule === 'stop_loss_dollars') return `${context==='entry'?'Add to position':'Stop loss'} when loss reaches -$${v.toFixed(2)}.`;
  if (rule === 'take_profit_percent') return `${context==='entry'?'Add to position':'Take profit'} when profit reaches +${v.toFixed(1)}%.`;
  if (rule === 'stop_loss_percent') return `${context==='entry'?'Add to position':'Stop loss'} when loss reaches -${v.toFixed(1)}%.`;
  return '';
}

function isPnlRule(rule) {
  return rule.includes('profit') || rule.includes('loss');
}

function updateEntryRuleText() {
  const rule = document.getElementById('entryRule')?.value;
  const value = document.getElementById('entryValue')?.value;
  const hint = document.getElementById('entryHint');
  if (!hint) return;
  hint.textContent = ruleSentence(rule, value, 'entry') +
    (isPnlRule(rule) ? ' P/L entry requires an existing position on the same side.' : '');
}

function updateExitRuleText() {
  const rule = document.getElementById('exitRule')?.value;
  const value = document.getElementById('exitValue')?.value;
  const hint = document.getElementById('exitHint');
  if (hint) hint.textContent = ruleSentence(rule, value, 'exit');
}

function buildShell() {
  app.innerHTML = `
  <div class="top">
    <h1>Kalchi Kill</h1>
    <div id="status" class="status"></div>
  </div>
  <div id="banner"></div>

  <h2>New entry → linked exit</h2>
  <div class="sectionnote">
    Pick the trading action you mean. The app handles the comparison logic internally.
  </div>

  <div class="card" id="entryForm">
    <div class="grid3">
      <div>
        <label>Kalshi market ticker</label>
        <input id="entryTicker" placeholder="Paste ticker from Kalshi" onblur="lookupMarket()">
      </div>
      <div class="actions">
        <button class="secondary" onclick="lookupMarket()">Decode market name</button>
      </div>
      <div id="marketPreview" class="namePreview"></div>
    </div>

    <div class="subcard">
      <div class="label">ENTRY</div>
      <div class="grid">
        <div>
          <label>Entry rule</label>
          <select id="entryRule" onchange="updateEntryRuleText()">
            ${ruleOptions('price_below','entry')}
          </select>
        </div>
        <div>
          <label>Trigger value</label>
          <input id="entryValue" type="number" step="0.01" placeholder="54" oninput="updateEntryRuleText()">
        </div>
        <div>
          <label>Side</label>
          <select id="entrySide">
            <option value="yes">YES</option>
            <option value="no">NO</option>
          </select>
        </div>
        <div>
          <label>Contracts</label>
          <input id="entryQty" type="number" min="0.01" step="0.01" value="10">
        </div>
        <div>
          <label>Max entry slippage (¢)</label>
          <input id="entrySlip" type="number" min="0" max="25" step="0.1" value="2.0">
        </div>
      </div>
      <div id="entryHint" class="hint"></div>
    </div>

    <div class="subcard">
      <div class="label">LINKED EXIT</div>
      <div class="grid">
        <div>
          <label>Exit rule</label>
          <select id="exitRule" onchange="updateExitRuleText()">
            ${ruleOptions('take_profit_dollars','exit')}
          </select>
        </div>
        <div>
          <label>Trigger value</label>
          <input id="exitValue" type="number" step="0.01" value="5" oninput="updateExitRuleText()">
        </div>
        <div>
          <label>Max exit slippage (¢)</label>
          <input id="exitSlip" type="number" min="0" max="25" step="0.1" value="3.0">
        </div>
        <div class="actions">
          <button class="accent" onclick="createEntry()">Arm entry + linked exit</button>
        </div>
      </div>
      <div id="exitHint" class="hint"></div>
    </div>
    <div id="entryError" class="error"></div>
  </div>

  <h2>Armed / linked entries</h2>
  <div id="plans"></div>

  <h2>Current Kalshi positions</h2>
  <div id="positions"></div>

  <h2>Execution log</h2>
  <div class="card"><div id="events" class="events"></div></div>`;

  const positions = document.getElementById('positions');
  positions.addEventListener('pointerdown', e => {
    if (e.target.closest('input,select,button')) positionLockUntil = Date.now() + 1500;
  }, true);
  positions.addEventListener('focusin', () => {
    positionLockUntil = Number.MAX_SAFE_INTEGER;
  }, true);
  positions.addEventListener('focusout', () => {
    positionLockUntil = Date.now() + 500;
  }, true);

  updateEntryRuleText();
  updateExitRuleText();
}

function start() {
  if (timer) clearInterval(timer);
  timer = setInterval(() => refresh(false), 500);
}

async function lookupMarket() {
  const t = document.getElementById('entryTicker').value.trim().toUpperCase();
  if (!t) return;
  const el = document.getElementById('marketPreview');
  try {
    const m = await api('/api/markets/' + encodeURIComponent(t));
    el.textContent = (m.title || t) + (m.subtitle ? ' — ' + m.subtitle : '') +
      ` · YES: ${m.yes_label || 'YES'} · NO: ${m.no_label || 'NO'}`;
  } catch (e) {
    el.textContent = e.message;
  }
}

async function createEntry() {
  const g = id => document.getElementById(id).value;
  try {
    document.getElementById('entryError').textContent = '';
    const er = ruleToBackend(g('entryRule'), g('entryValue'));
    const xr = ruleToBackend(g('exitRule'), g('exitValue'));
    await api('/api/entries', {
      method:'POST',
      body:JSON.stringify({
        ticker:g('entryTicker').trim().toUpperCase(),
        direction:g('entrySide'),
        quantity:g('entryQty'),
        entry_mode:er.mode,
        entry_operator:er.operator,
        entry_trigger_value:er.value,
        entry_slippage_cents:g('entrySlip'),
        exit_mode:xr.mode,
        exit_operator:xr.operator,
        exit_trigger_value:xr.value,
        exit_slippage_cents:g('exitSlip')
      })
    });
    await refresh(true);
  } catch (e) {
    document.getElementById('entryError').textContent = e.message;
  }
}

async function cancelPlan(id) {
  if (!confirm('Cancel this trigger? If it already entered, this cancels only its linked exit and leaves the position open.')) return;
  try {
    await api('/api/entries/' + encodeURIComponent(id), {method:'DELETE'});
    await refresh(true);
  } catch (e) {
    alert(e.message);
  }
}

function sideName(m, d) {
  return d === 'YES' ? (m.yes_label || 'YES') : (m.no_label || 'NO');
}

function positionCard(p, i) {
  const st = p.stop || {};
  const armed = !!st.armed;
  const mode = st.trigger_mode || 'price';
  const op = st.operator || 'lte';
  const rule = backendToRule(mode, op);
  const value = ruleDisplayValue(mode, st.trigger);
  const slip = st.slippage != null ? (Number(st.slippage) * 100).toFixed(1) : '3.0';
  const m = p.market || {};
  const pnlReady = p.avg_entry != null && p.pnl_dollars != null;
  const summary = value ? ruleSentence(rule, value, 'exit') : 'Choose an exit rule and value.';

  return `<div class="card" id="pos-${i}" data-ticker="${esc(p.ticker)}">
    <div class="rowhead">
      <div>
        <div class="title">${esc(m.title || p.ticker)}</div>
        <div class="subtitle">${esc(m.subtitle || '')}</div>
        <div class="ticker">${esc(p.ticker)}</div>
      </div>
      <div>
        <div class="planStatus ${armed?'bad':''}">${armed?'EXIT ARMED':'DISARMED'}</div>
        <div class="small">${esc(p.direction)} · ${esc(sideName(m,p.direction))}</div>
      </div>
    </div>

    <div class="price">${cents(p.held_bid)}</div>
    <div class="label">Executable exit bid · ${esc(p.quantity)} contracts</div>

    <div class="stats">
      <div class="stat"><span class="label">Avg entry</span><strong>${cents(p.avg_entry)}</strong></div>
      <div class="stat"><span class="label">P/L</span><strong class="${p.pnl_dollars==null?'':Number(p.pnl_dollars)>=0?'ok':'bad'}">${money(p.pnl_dollars)}</strong></div>
      <div class="stat"><span class="label">P/L %</span><strong class="${p.pnl_percent==null?'':Number(p.pnl_percent)>=0?'ok':'bad'}">${pct(p.pnl_percent)}</strong></div>
    </div>

    ${pnlReady ? '' : '<div class="hint warn">P/L basis is not available yet. Price exits still work; P/L exits cannot arm until Avg entry appears.</div>'}

    <div class="grid">
      <div>
        <label>Exit rule</label>
        <select class="stopRule" onchange="updatePositionRule(${i})">
          ${ruleOptions(rule,'exit')}
        </select>
      </div>
      <div>
        <label>Trigger value</label>
        <input class="stopValue" type="number" step="0.01" value="${value}" placeholder="5" oninput="updatePositionRule(${i})">
      </div>
      <div>
        <label>Max exit slippage (¢)</label>
        <input class="stopSlip" type="number" min="0" max="25" step="0.1" value="${slip}">
      </div>
      <div class="actions">
        <button class="${armed?'secondary':'danger'}" onclick="saveStop(${i},${!armed})">
          ${armed?'Disarm exit':'Arm exit'}
        </button>
      </div>
    </div>
    <div class="hint ruleSummary">${esc(summary)}</div>
    ${st.last_error ? `<div class="error">${esc(st.last_error)}</div>` : ''}
  </div>`;
}

function updatePositionRule(i) {
  const c = document.getElementById('pos-' + i);
  if (!c) return;
  const rule = c.querySelector('.stopRule').value;
  const value = c.querySelector('.stopValue').value;
  c.querySelector('.ruleSummary').textContent = ruleSentence(rule, value, 'exit');
}

async function saveStop(i, armed) {
  const c = document.getElementById('pos-' + i);
  const ticker = c.dataset.ticker;
  try {
    const r = ruleToBackend(c.querySelector('.stopRule').value, c.querySelector('.stopValue').value);
    await api('/api/stops/' + encodeURIComponent(ticker), {
      method:'PUT',
      body:JSON.stringify({
        trigger_mode:r.mode,
        operator:r.operator,
        trigger_value:r.value,
        slippage_cents:c.querySelector('.stopSlip').value,
        armed
      })
    });
    positionLockUntil = 0;
    await refresh(true);
  } catch (e) {
    alert(e.message);
  }
}

function triggerText(mode, operator, trigger, context='exit') {
  const rule = backendToRule(mode, operator);
  const value = ruleDisplayValue(mode, trigger);
  return ruleSentence(rule, value, context);
}

function metricFormat(mode, v) {
  return mode === 'price' ? cents(v) : mode === 'pnl_dollars' ? money(v) : pct(v);
}

function planCard(p) {
  const m = p.market || {};
  const em = p.entry_mode || 'price';
  return `<div class="card">
    <div class="rowhead">
      <div>
        <div class="title">${esc(m.title || p.ticker)}</div>
        <div class="subtitle">${esc(m.subtitle || '')}</div>
        <div class="ticker">${esc(p.ticker)}</div>
      </div>
      <div class="planStatus ${esc(p.status)}">${esc(p.status.replaceAll('_',' ').toUpperCase())}</div>
    </div>
    <div class="stats">
      <div class="stat"><span class="label">Entry</span><strong>${esc(p.direction.toUpperCase())} ${esc(p.quantity)}</strong></div>
      <div class="stat"><span class="label">Entry rule</span><strong>${esc(triggerText(em,p.entry_operator,p.entry_trigger,'entry'))}</strong></div>
      <div class="stat"><span class="label">Current entry metric</span><strong>${metricFormat(em,p.entry_metric)}</strong></div>
      <div class="stat"><span class="label">Actual fill</span><strong>${p.entry_price?cents(p.entry_price):'—'}</strong></div>
      <div class="stat"><span class="label">Linked qty open</span><strong>${esc(p.open_qty)}</strong></div>
      <div class="stat"><span class="label">Linked exit</span><strong>${esc(triggerText(p.exit_mode,p.exit_operator,p.exit_trigger,'exit'))}</strong></div>
      <div class="stat"><span class="label">Current exit metric</span><strong>${metricFormat(p.exit_mode,p.exit_metric)}</strong></div>
    </div>
    <div style="margin-top:12px"><button class="secondary" onclick="cancelPlan('${esc(p.plan_id)}')">Cancel trigger</button></div>
    ${p.last_error?`<div class="error">${esc(p.last_error)}</div>`:''}
  </div>`;
}

function render(s, force = false) {
  lastState = s;
  document.getElementById('status').innerHTML =
    `<span class="pill ${s.ws_connected?'ok':'bad'}">● ${s.ws_connected?'Live feed':'Feed offline'}</span>` +
    `<span class="pill ${s.execution_enabled?'ok':'warn'}">${s.execution_enabled?'LIVE EXECUTION':'SIMULATION'}</span>` +
    `<span class="pill">${esc(s.environment).toUpperCase()}</span>` +
    (s.kalshi_configured ? '' : '<span class="pill bad">Credentials missing</span>');

  document.getElementById('banner').innerHTML = s.execution_enabled ? '' :
    '<div class="banner">Execution is disabled. Triggers are simulated only.</div>';

  document.getElementById('plans').innerHTML =
    s.entry_plans?.length ? s.entry_plans.map(planCard).join('') :
    '<div class="card empty">No entry triggers configured.</div>';

  const focus = document.activeElement;
  const editingPositions = focus && document.getElementById('positions').contains(focus);
  if (force || (!editingPositions && Date.now() > positionLockUntil)) {
    const open = s.positions.filter(p => Number(p.position) !== 0 || p.stop);
    document.getElementById('positions').innerHTML =
      open.length ? open.map(positionCard).join('') :
      '<div class="card empty">No open Kalshi positions detected.</div>';
  }

  document.getElementById('events').innerHTML = s.events.length ?
    s.events.map(e => `<div class="event">${new Date(e.ts*1000).toLocaleTimeString()} ${esc(e.ticker||'')} ${esc(e.message)}</div>`).join('') :
    'No events yet';
}

async function refresh(force = false) {
  try {
    render(await api('/api/state'), force);
  } catch (e) {
    if (e.message === 'AUTH') {
      if (timer) clearInterval(timer);
      app.innerHTML = '<div class="login"><h1>Kalchi Kill</h1><input id="pw" type="password" placeholder="Dashboard password"><button onclick="login()">Sign in</button><div id="loginerr" class="error"></div></div>';
    }
  }
}

refresh(true).then(() => {
  if (document.getElementById('status')) start();
}).catch(() => {});
