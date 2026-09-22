import { html, render } from '../core/dom.js';
import { num, plural } from '../core/format.js';
import { reducedMotion, surface, token, withAlpha } from '../core/canvas.js';
import { MODE_LABELS, POOL_STATUS } from './pairing-copy.js';

const TOP = 72;
const BOTTOM = 46;
const HIT_RADIUS = 18;
const RADIUS = { aggregate: 15, instance: 10, agent: 10 };

const STATE_TOKEN = {
  live: '--ok', stale: '--warn', silent: '--bad', releasing: '--dim',
  review: '--warn', waiting: '--muted', chosen: '--goal',
  requesting: '--signal', offered: '--signal', bidding: '--signal',
  free: '--muted', engaged: '--signal',
};

const colourOf = state => token(STATE_TOKEN[state] || '--muted');
const font = (size, weight = 400, family = '--mono') => `${weight} ${size}px ${token(family, 'monospace')}`;

function spread(items, x, h, place) {
  const span = h - TOP - BOTTOM;
  return items.map((item, i) => place(item, x, TOP + (span * (i + 1)) / (items.length + 1)));
}

function layout(data, w, h) {
  const { graph, pairing } = data;
  const cx = w / 2;
  const cols = {
    poolAgents: cx * 0.2,
    poolInstances: cx * 0.6,
    instances: cx + (w - cx) * 0.4,
    agents: cx + (w - cx) * 0.8,
  };

  const nodes = [{
    key: 'aggregate', role: 'aggregate', side: 'center', id: graph.aggregate.id,
    detail: `${graph.aggregate.detail}, ${MODE_LABELS[pairing.mode] || pairing.mode}`,
    state: 'live', x: cx, y: h / 2,
  }];
  const edges = [];

  spread(graph.pairs, 0, h, (pair, _, y) => {
    nodes.push({ key: `i:${pair.instance}`, role: 'instance', side: 'linked', id: pair.instance,
                 detail: pair.telemetry, state: pair.state, x: cols.instances, y, pair });
    nodes.push({ key: `a:${pair.agent}`, role: 'agent', side: 'linked', id: pair.agent,
                 detail: pair.kind, state: pair.state, x: cols.agents, y, pair });
    edges.push({ from: 'aggregate', to: `i:${pair.instance}`, kind: 'instantiate', state: pair.state, label: pair.handshake });
    edges.push({ from: `i:${pair.instance}`, to: `a:${pair.agent}`, kind: 'telemetry', state: pair.state, label: pair.telemetry });
  });

  const poolInstances = new Set(graph.pool.instances.map(i => i.id));
  spread(graph.pool.instances, cols.poolInstances, h, (inst, x, y) => {
    nodes.push({ key: `i:${inst.id}`, role: 'instance', side: 'pool', id: inst.id,
                 detail: POOL_STATUS[inst.state]?.short, state: inst.state, x, y, pool: inst });
    edges.push({ from: 'aggregate', to: `i:${inst.id}`, kind: 'heartbeat', state: inst.state });
  });

  spread(graph.pool.agents, cols.poolAgents, h, (agent, x, y) => {
    nodes.push({ key: `a:${agent.id}`, role: 'agent', side: 'pool', id: agent.id,
                 detail: POOL_STATUS[agent.state]?.short, state: agent.state, x, y, pool: agent });
    const targets = agent.targets.filter(t => poolInstances.has(t));
    targets.forEach(t => edges.push({ from: `i:${t}`, to: `a:${agent.id}`, kind: 'offer', state: agent.state }));
    if (agent.choice && poolInstances.has(agent.choice) && !targets.includes(agent.choice)) {
      edges.push({ from: `i:${agent.choice}`, to: `a:${agent.id}`, kind: 'choice', state: 'chosen', label: 'your pick' });
    }
    if (!targets.length && !agent.choice) {
      edges.push({ from: 'aggregate', to: `a:${agent.id}`, kind: 'heartbeat', state: agent.state });
    }
  });

  return { nodes, byKey: new Map(nodes.map(n => [n.key, n])), edges, cols, cx };
}

function nodePath(ctx, n, r) {
  ctx.beginPath();
  if (n.role === 'aggregate') {
    for (let i = 0; i < 6; i++) {
      const a = (Math.PI / 3) * i + Math.PI / 6;
      ctx.lineTo(n.x + r * Math.cos(a), n.y + r * Math.sin(a));
    }
    ctx.closePath();
  } else if (n.role === 'instance') {
    ctx.rect(n.x - r, n.y - r, 2 * r, 2 * r);
  } else {
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
  }
}

function drawZones(ctx, w, h, L, data) {
  const { pool, pairs } = data.graph;

  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, L.cx, h);
  ctx.clip();
  ctx.fillStyle = 'rgba(11,18,22,.35)';
  ctx.fillRect(0, 0, L.cx, h);
  ctx.strokeStyle = 'rgba(127,209,222,.04)';
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = -h; x < L.cx; x += 16) {
    ctx.moveTo(x, h);
    ctx.lineTo(x + h, 0);
  }
  ctx.stroke();
  ctx.restore();

  ctx.save();
  ctx.setLineDash([2, 5]);
  ctx.strokeStyle = 'rgba(127,209,222,.16)';
  ctx.beginPath();
  ctx.moveTo(L.cx, 18);
  ctx.lineTo(L.cx, h - 18);
  ctx.stroke();
  ctx.restore();

  const poolCount = [plural(pool.agents.length, 'agent'), plural(pool.instances.length, 'instance')].join(', ');
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  ctx.fillStyle = token('--text');
  ctx.font = font(12.5, 600, '--sans');
  ctx.fillText('Unlinked pool', 16, 26);
  ctx.fillStyle = token('--muted');
  ctx.font = font(10.5);
  ctx.fillText(poolCount, 16, 42);

  ctx.textAlign = 'right';
  ctx.fillStyle = token('--text');
  ctx.font = font(12.5, 600, '--sans');
  ctx.fillText('Linked pairs', w - 16, 26);
  ctx.fillStyle = token('--muted');
  ctx.font = font(10.5);
  ctx.fillText(plural(pairs.length, 'pair'), w - 16, 42);

  ctx.textAlign = 'center';
  ctx.fillStyle = token('--dim');
  ctx.font = font(9.5);
  [[L.cols.poolAgents, 'agents'], [L.cols.poolInstances, 'instances'],
   [L.cols.instances, 'instances'], [L.cols.agents, 'agents']].forEach(([x, text]) => ctx.fillText(text, x, TOP - 12));

  ctx.fillStyle = token('--muted');
  ctx.font = font(10.5);
  const mid = h / 2;
  if (!data.pairing.instance_discovery) {
    ctx.fillText('Instances are not pooled', L.cols.poolInstances, mid - 6);
    ctx.fillStyle = token('--dim');
    ctx.fillText('instance discovery is off', L.cols.poolInstances, mid + 10);
  } else if (!pool.instances.length) {
    ctx.fillText('No free instances', L.cols.poolInstances, mid);
  }
  ctx.fillStyle = token('--muted');
  if (!pool.agents.length) ctx.fillText('No agents waiting', L.cols.poolAgents, mid);
  if (!pairs.length) ctx.fillText('No pairs linked yet', (L.cols.instances + L.cols.agents) / 2, mid);
}

function drawEdge(ctx, a, b, edge, t, dim) {
  const ra = RADIUS[a.role] + 2, rb = RADIUS[b.role] + 2;
  const dir = Math.sign(b.x - a.x) || 1;
  const x0 = a.x + dir * ra, x1 = b.x - dir * rb;
  const mx = (x0 + x1) / 2;
  const colour = edge.kind === 'offer' ? token('--signal') : colourOf(edge.state);

  ctx.save();
  ctx.globalAlpha = dim ? 0.25 : 1;
  ctx.lineWidth = { telemetry: 1.6, instantiate: 1.1, offer: 1.4, choice: 1.3, heartbeat: 1 }[edge.kind];
  ctx.strokeStyle = withAlpha(colour, edge.kind === 'heartbeat' ? 0.3 : 0.8);
  const dash = { heartbeat: [1.5, 4], offer: [6, 4], choice: [2, 3] }[edge.kind]
    ?? (edge.state === 'live' ? [] : [4, 4]);
  ctx.setLineDash(dash);
  if (edge.kind === 'offer') ctx.lineDashOffset = -t / 40;
  ctx.beginPath();
  ctx.moveTo(x0, a.y);
  ctx.bezierCurveTo(mx, a.y, mx, b.y, x1, b.y);
  ctx.stroke();
  ctx.setLineDash([]);

  if (edge.state === 'live' && (edge.kind === 'telemetry' || edge.kind === 'instantiate') && !reducedMotion()) {
    const p = ((t / (edge.kind === 'telemetry' ? 900 : 1600)) + (a.y % 97) / 97) % 1;
    const u = 1 - p;
    const px = u ** 3 * x0 + 3 * u * u * p * mx + 3 * u * p * p * mx + p ** 3 * x1;
    const py = u ** 3 * a.y + 3 * u * u * p * a.y + 3 * u * p * p * b.y + p ** 3 * b.y;
    ctx.fillStyle = colour;
    ctx.beginPath();
    ctx.arc(px, py, 2.2, 0, Math.PI * 2);
    ctx.fill();
  }

  if (edge.label) {
    ctx.fillStyle = edge.kind === 'choice' ? token('--goal') : token('--dim');
    ctx.font = font(9.5);
    ctx.textAlign = 'center';
    ctx.fillText(edge.label, mx, (a.y + b.y) / 2 - 6);
  }
  ctx.restore();
}

function drawNode(ctx, n, look, t, poolTimeout) {
  const r = RADIUS[n.role];
  const colour = colourOf(n.state);
  ctx.save();
  ctx.globalAlpha = look.dim ? 0.3 : 1;

  if (n.pool && poolTimeout) {
    const left = Math.max(0, Math.min(1, n.pool.expires_in / poolTimeout));
    ctx.lineWidth = 2;
    ctx.strokeStyle = withAlpha(token('--rule'), 1);
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + 6, 0, Math.PI * 2);
    ctx.stroke();
    ctx.strokeStyle = withAlpha(token(left < 0.35 ? '--warn' : '--muted'), 0.9);
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + 6, -Math.PI / 2, -Math.PI / 2 + left * Math.PI * 2);
    ctx.stroke();
  }

  if (n.state === 'requesting' && !reducedMotion()) {
    const p = (t / 1600) % 1;
    ctx.strokeStyle = withAlpha(token('--signal'), 0.55 * (1 - p));
    ctx.lineWidth = 1.2;
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + 8 + p * 16, 0, Math.PI * 2);
    ctx.stroke();
  }

  if (look.target) {
    const pulse = reducedMotion() ? 0.6 : 0.45 + 0.35 * Math.sin(t / 220);
    ctx.strokeStyle = withAlpha(token('--goal'), pulse);
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + 11, 0, Math.PI * 2);
    ctx.stroke();
  }
  if (look.armed) {
    ctx.strokeStyle = token('--goal');
    ctx.lineWidth = 2;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.arc(n.x, n.y, r + 11, 0, Math.PI * 2);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  nodePath(ctx, n, r);
  ctx.fillStyle = n.side === 'pool' ? '#0c151a' : 'rgba(17,28,34,.95)';
  ctx.fill();
  ctx.strokeStyle = colour;
  ctx.lineWidth = look.hover || look.armed ? 2.4 : 1.6;
  if (n.state === 'waiting' || n.state === 'free') ctx.setLineDash([3, 2.5]);
  ctx.stroke();
  ctx.setLineDash([]);

  if (n.state === 'review') {
    ctx.fillStyle = colour;
    ctx.font = font(12, 700);
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText('!', n.x, n.y + 0.5);
  }

  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'center';
  ctx.fillStyle = token(look.hover ? '--signal' : '--text');
  ctx.font = font(11);
  ctx.fillText(n.id, n.x, n.y + r + 20);
  if (n.detail) {
    ctx.fillStyle = n.side === 'pool' ? colour : token('--muted');
    ctx.font = font(9.5);
    ctx.fillText(n.detail, n.x, n.y + r + 32);
  }
  ctx.restore();
}

function describe(node, data) {
  if (!node) return null;
  if (node.role === 'aggregate') {
    return html`${node.id}  aggregate
pairing <em>${MODE_LABELS[data.pairing.mode] || data.pairing.mode}</em>  ${data.graph.aggregate.detail}`;
  }
  if (node.pool) {
    const status = POOL_STATUS[node.state]?.long?.(node.pool, data.pairing) ?? node.state;
    return html`${node.id}  unlinked ${node.role}  <em>${status}</em>
last heard ${num(node.pool.age, 1)}s ago, evicted in ${num(node.pool.expires_in, 1)}s`;
  }
  return html`${node.id}  ${node.role}  <em>${node.state}</em>
paired with ${node.role === 'agent' ? node.pair.instance : node.pair.agent}, telemetry ${node.pair.telemetry}`;
}

export function createNetworkGraph(canvas, readout, actions) {
  let data = null, L = null, hover = null, armed = null, lastReadout = '';

  const linkable = () => new Set(data?.pairing.linkable || []);
  const free = () => new Set(data?.pairing.free || []);
  const manual = () => data?.pairing.mode === 'gui';

  function canArm(node) {
    if (!node?.pool || !manual()) return false;
    return node.role === 'agent' ? linkable().has(node.id) : free().has(node.id);
  }

  function isTarget(node) {
    if (!armed || !node?.pool || node.role === armed.role) return false;
    return canArm(node);
  }

  function actionable(node) {
    if (!node?.pool) return false;
    if (node.state === 'review') return true;
    return canArm(node) || (!manual() && data.pairing.instance_discovery);
  }

  function hit(event) {
    if (!L) return null;
    const r = canvas.getBoundingClientRect();
    const x = event.clientX - r.left, y = event.clientY - r.top;
    return L.nodes.find(n => Math.hypot(n.x - x, n.y - y) <= HIT_RADIUS) || null;
  }

  function setReadout(markup) {
    const text = String(markup);
    if (text !== lastReadout) {
      lastReadout = text;
      render(readout, markup);
    }
  }

  function draw(t = performance.now()) {
    const { ctx, w, h } = surface(canvas);
    if (!data) return;
    L = layout(data, w, h);
    if (armed) armed = L.byKey.get(armed.key) || null;
    if (hover) hover = L.byKey.get(hover.key) || null;

    drawZones(ctx, w, h, L, data);
    const focusKeys = armed ? new Set(L.nodes.filter(n => n === armed || isTarget(n)).map(n => n.key)) : null;
    L.edges.forEach(edge => {
      const a = L.byKey.get(edge.from), b = L.byKey.get(edge.to);
      if (a && b) drawEdge(ctx, a, b, edge, t, focusKeys && !(focusKeys.has(a.key) && focusKeys.has(b.key)));
    });
    if (armed && hover && isTarget(hover)) {
      const [agent, inst] = armed.role === 'agent' ? [armed, hover] : [hover, armed];
      drawEdge(ctx, inst, agent, { kind: 'choice', state: 'chosen', label: 'link?' }, t, false);
    }
    L.nodes.forEach(n => drawNode(ctx, n, {
      hover: hover === n,
      armed: armed === n,
      target: isTarget(n),
      dim: focusKeys && !focusKeys.has(n.key) && n.role !== 'aggregate',
    }, t, data.pairing.pool_timeout));

    if (armed) {
      const other = armed.role === 'agent' ? 'a free instance' : 'a waiting agent';
      setReadout(html`Pairing <em>${armed.id}</em>: click ${other} to link them.
Esc or click empty space to cancel.`);
    } else if (hover) {
      setReadout(describe(hover, data));
    } else {
      const c = data.counters;
      setReadout(html`${c.msgs_in} in / ${c.msgs_out} out  stale after <em>${c.stale_after}s</em>
pairing <em>${MODE_LABELS[data.pairing.mode] || data.pairing.mode}</em>`);
    }
  }

  function click(node) {
    if (!node || !node.pool) {
      armed = null;
      return;
    }
    if (node.state === 'review') {
      actions.review(node.id);
      return;
    }
    if (!manual()) {
      if (data.pairing.instance_discovery) actions.hint('Switch pairing to Manual to pair nodes yourself.');
      return;
    }
    if (armed && isTarget(node)) {
      const [agent, inst] = armed.role === 'agent' ? [armed.id, node.id] : [node.id, armed.id];
      armed = null;
      actions.link(agent, inst);
      return;
    }
    armed = canArm(node) && armed !== node ? node : null;
    if (armed?.role === 'agent') actions.prepare(armed.id);
  }

  canvas.addEventListener('mousemove', event => {
    const next = hit(event);
    if (next?.key !== hover?.key) {
      hover = next;
      canvas.style.cursor = actionable(hover) ? 'pointer' : 'default';
      if (reducedMotion()) draw();
    }
  });
  canvas.addEventListener('mouseleave', () => { hover = null; draw(); });
  canvas.addEventListener('click', event => { click(hit(event)); draw(); });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && armed && canvas.offsetParent !== null) {
      armed = null;
      draw();
    }
  });

  return {
    update(next) {
      data = next;
      if (!manual()) armed = null;
    },
    draw,
  };
}
