import { $, $$, html, render, setText } from '../core/dom.js';
import { clock, num, secs } from '../core/format.js';
import { banner } from '../core/shell.js';
import { POOL_STATUS } from './pairing-copy.js';

const PHASE_TONE = { linked: 'ok', requested: 'info', review: 'warn', released: 'warn', rejected: 'error' };
const STATE_ROW = { live: 'live', stale: 'stale', silent: 'lost', pending: 'idle', released: 'idle' };

function historyRow(r) {
  return html`
    <tr>
      <td>${r.agent}</td>
      <td class="${r.instance ? '' : 'muted'}">${r.instance || '—'}</td>
      <td><span class="tag ${PHASE_TONE[r.phase] || ''}">${r.phase}</span></td>
      <td class="num">${r.discovery.count}</td>
      <td class="num">${r.instantiate.out}↑ ${r.instantiate.in}↓</td>
      <td class="num">${r.mission.out}↑ ${r.mission.in}↓</td>
      <td class="num">${num(r.twin_state.hz, 1)} Hz</td>
      <td class="num ${STATE_ROW[r.state] === 'lost' ? 'error' : ''}">${secs(r.twin_state.age)}</td>
      <td class="num">${r.obstacle.count}</td>
    </tr>`;
}

function poolRow(node, role, pairing) {
  const status = POOL_STATUS[node.state] || { tone: 'muted', long: () => node.state };
  const detail = role === 'agent'
    ? [node.kind, node.agent_type].filter(Boolean).join(', ') || '—'
    : 'instance';
  let action = '';
  if (node.state === 'review') {
    action = html`<button class="ghost" type="button" data-review="${node.id}">Review</button>`;
  } else if (role === 'agent' && pairing.mode === 'gui' && pairing.linkable.includes(node.id)) {
    action = html`<button class="ghost" type="button" data-pair="${node.id}">Pair</button>`;
  }
  return html`
    <tr>
      <td><span class="role-mark ${role}" aria-hidden="true"></span>${node.id}</td>
      <td><span class="status ${status.tone}">${status.long(node, pairing)}</span></td>
      <td class="muted">${detail}</td>
      <td class="num">${node.hz ? `${num(node.hz, 1)} Hz` : '—'}</td>
      <td class="num">${secs(node.age)} ago</td>
      <td class="num ${node.expires_in < 2 ? 'warn' : ''}">${secs(node.expires_in)}</td>
      <td class="num">${action}</td>
    </tr>`;
}

function counterCells(c) {
  return [
    [c.linked, 'linked'], [c.pooled, 'unlinked'], [c.degraded, 'degraded'],
    [c.msgs_in, 'msgs in'], [c.msgs_out, 'msgs out'], [c.handshakes, 'handshakes'],
  ].map(([value, label]) => html`<div><b>${value}</b><span>${label}</span></div>`);
}

function logLines(events) {
  const glyph = { in: '←', out: '→' };
  const tone = { ok: 'ok', warn: 'warn', error: 'error' };
  return events.slice().reverse().map(e => html`
    <div><time>${clock(e.t)}</time><b>${glyph[e.dir] || '·'}</b>
      <span class="${tone[e.level] || ''}">${e.kind} ${e.peer}: ${e.detail}</span></div>`);
}

export function createNetworkTables(actions) {
  let table = 'history', channel = 'handshake', latest = null, plannerShown = false;

  const selectTab = (attr, value) => $$(`[data-${attr}]`).forEach(b =>
    b.setAttribute('aria-selected', String(b.dataset[attr] === value)));

  $$('[data-table]').forEach(b => b.addEventListener('click', () => {
    table = b.dataset.table;
    selectTab('table', table);
    $('historyTable').hidden = table !== 'history';
    $('poolTable').hidden = table !== 'pool';
  }));
  $$('[data-log]').forEach(b => b.addEventListener('click', () => {
    channel = b.dataset.log;
    selectTab('log', channel);
    if (latest) renderLog(latest);
  }));
  $('poolRows').addEventListener('click', event => {
    const review = event.target.closest('[data-review]');
    if (review) return actions.review(review.dataset.review);
    const pair = event.target.closest('[data-pair]');
    if (pair) actions.focusAgent(pair.dataset.pair);
  });

  function renderLog(data) {
    const events = (channel === 'telemetry' ? data.telemetry_events : data.events) || [];
    setText($('logCount'), String(events.length));
    render($('eventLog'), events.length ? logLines(events) : html`<p class="empty">Quiet so far.</p>`);
  }

  function renderTables(data) {
    latest = data;
    const { pool } = data.graph;
    const c = data.counters || {};

    render($('counters'), counterCells(c));
    if (c.planner_error) banner(`Planner: ${c.planner_error}`);
    else if (plannerShown) banner('');
    plannerShown = Boolean(c.planner_error);

    const rows = data.rows || [];
    setText($('historyCount'), String(rows.length));
    render($('historyRows'), rows.length ? rows.map(historyRow)
      : html`<tr><td colspan="9" class="muted">No agent has announced itself yet.</td></tr>`);

    const poolSize = pool.agents.length + pool.instances.length;
    setText($('poolCount'), String(poolSize));
    render($('poolRows'), poolSize
      ? [...pool.agents.map(a => poolRow(a, 'agent', data.pairing)),
         ...pool.instances.map(i => poolRow(i, 'instance', data.pairing))]
      : html`<tr><td colspan="7" class="muted">Every known node is paired.</td></tr>`);

    renderLog(data);
  }

  return { render: renderTables };
}
