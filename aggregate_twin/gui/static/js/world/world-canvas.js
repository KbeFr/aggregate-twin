import { html, render } from '../core/dom.js';
import { num, pct } from '../core/format.js';
import { surface, token, withAlpha } from '../core/canvas.js';

const DEFAULT_SHAPE = { type: 'rect', length: 0.44, width: 0.30 };
const MONO = '10px ui-monospace, monospace';

function projection(world, w, h) {
  const pad = 26;
  const scale = Math.min((w - 2 * pad) / Math.max(world.width, 1e-6),
                         (h - 2 * pad) / Math.max(world.height, 1e-6));
  const ox = (w - world.width * scale) / 2 - world.origin_x * scale;
  const oy = h - (h - world.height * scale) / 2 + world.origin_y * scale;
  return {
    scale,
    toX: x => ox + x * scale,
    toY: y => oy - y * scale,
    toWorld: (px, py) => [(px - ox) / scale, (oy - py) / scale],
  };
}

function polyline(ctx, T, points) {
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(T.toX(p[0]), T.toY(p[1])) : ctx.moveTo(T.toX(p[0]), T.toY(p[1]))));
}

function grid(ctx, T, world) {
  const target = 40 / T.scale;
  const step = [0.5, 1, 2, 5, 10, 20, 50].find(s => s >= target) || 100;
  ctx.lineWidth = 1;
  ctx.strokeStyle = 'rgba(127,209,222,.06)';
  ctx.beginPath();
  for (let x = world.origin_x; x <= world.origin_x + world.width + 1e-9; x += step) {
    ctx.moveTo(T.toX(x), T.toY(world.origin_y));
    ctx.lineTo(T.toX(x), T.toY(world.origin_y + world.height));
  }
  for (let y = world.origin_y; y <= world.origin_y + world.height + 1e-9; y += step) {
    ctx.moveTo(T.toX(world.origin_x), T.toY(y));
    ctx.lineTo(T.toX(world.origin_x + world.width), T.toY(y));
  }
  ctx.stroke();
  ctx.strokeStyle = 'rgba(127,209,222,.22)';
  ctx.lineWidth = 1.2;
  ctx.strokeRect(T.toX(world.origin_x), T.toY(world.origin_y + world.height),
                 world.width * T.scale, world.height * T.scale);
  return step;
}

function obstacles(ctx, T, list) {
  list.forEach(o => {
    const confidence = o.confidence ?? 1;
    const alpha = 0.25 + 0.5 * confidence;
    ctx.lineWidth = 1.2;
    ctx.strokeStyle = o.dynamic ? `rgba(224,104,95,${alpha})` : `rgba(224,160,64,${alpha})`;
    ctx.fillStyle = o.dynamic ? 'rgba(224,104,95,.12)' : 'rgba(224,160,64,.14)';
    ctx.setLineDash(confidence < 1 ? [4, 3] : []);
    if (o.polygon && o.polygon.length > 2) {
      polyline(ctx, T, o.polygon);
      ctx.closePath();
    } else {
      ctx.beginPath();
      ctx.arc(T.toX(o.x), T.toY(o.y), Math.max((o.radius || 0.25) * T.scale, 3), 0, Math.PI * 2);
    }
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
  });
}

function agentPaths(ctx, T, agents, state) {
  agents.forEach(a => {
    if (!Array.isArray(a.path) || a.path.length < 2) return;
    const shown = state.agent === a.name || (state.mission && a.mission_id === state.mission);
    if (!shown) return;
    ctx.lineWidth = 1.8;
    ctx.strokeStyle = 'rgba(127,209,222,.75)';
    polyline(ctx, T, a.path);
    ctx.stroke();
  });
}

function missionRoute(ctx, T, missions, state) {
  const mission = state.mission && missions.find(m => m.id === state.mission);
  const points = mission?.path;
  if (!Array.isArray(points) || points.length < 2) return;
  const xy = points.map(p => [T.toX(p[0]), T.toY(p[1])]);
  const goal = token('--goal');

  ctx.save();
  ctx.lineJoin = ctx.lineCap = 'round';
  ctx.beginPath();
  xy.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.strokeStyle = withAlpha(goal, 0.22);
  ctx.lineWidth = 6;
  ctx.stroke();
  ctx.strokeStyle = goal;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  const total = xy.reduce((s, p, i) => (i ? s + Math.hypot(p[0] - xy[i - 1][0], p[1] - xy[i - 1][1]) : 0), 0);
  const gap = Math.max(60, total / 8);
  let travelled = 0, next = gap;
  for (let i = 1; i < xy.length; i++) {
    const [x0, y0] = xy[i - 1], [x1, y1] = xy[i];
    const seg = Math.hypot(x1 - x0, y1 - y0);
    if (!seg) continue;
    while (travelled + seg >= next) {
      const t = (next - travelled) / seg;
      const px = x0 + (x1 - x0) * t, py = y0 + (y1 - y0) * t;
      const a = Math.atan2(y1 - y0, x1 - x0);
      ctx.beginPath();
      ctx.moveTo(px - 4 * Math.cos(a - 0.5), py - 4 * Math.sin(a - 0.5));
      ctx.lineTo(px, py);
      ctx.lineTo(px - 4 * Math.cos(a + 0.5), py - 4 * Math.sin(a + 0.5));
      ctx.stroke();
      next += gap;
    }
    travelled += seg;
  }

  const [sx, sy] = xy[0], [ex, ey] = xy[xy.length - 1];
  ctx.fillStyle = goal;
  ctx.beginPath(); ctx.arc(sx, sy, 3.5, 0, Math.PI * 2); ctx.fill();
  ctx.beginPath(); ctx.arc(ex, ey, 5, 0, Math.PI * 2); ctx.stroke();
  ctx.restore();
}

function goals(ctx, T, missions, picked) {
  const goal = token('--goal');
  ctx.font = MONO;
  missions.forEach(m => {
    const waypoints = m.waypoints || [];
    const points = m.goal ? [m.goal] : waypoints;
    const live = m.status === 'ACTIVE';
    points.forEach((g, i) => {
      if (!g || g.length < 2) return;
      const x = T.toX(g[0]), y = T.toY(g[1]);
      ctx.strokeStyle = live ? goal : withAlpha(goal, 0.45);
      ctx.lineWidth = 1.2;
      ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y);
      ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11);
      ctx.stroke();
      ctx.fillStyle = live ? goal : withAlpha(goal, 0.55);
      ctx.fillText(waypoints.length > 1 ? `${m.id} ${i + 1}` : m.id, x + 13, y - 6);
    });
  });
  if (picked) {
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = goal;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.arc(T.toX(picked[0]), T.toY(picked[1]), 11, 0, Math.PI * 2); ctx.stroke();
    ctx.setLineDash([]);
  }
}

function footprint(ctx, T, shape, colour) {
  const k = T.scale;
  ctx.beginPath();
  if (shape.type === 'polygon' && Array.isArray(shape.points)) {
    shape.points.forEach((p, i) => (i ? ctx.lineTo(p[0] * k, -p[1] * k) : ctx.moveTo(p[0] * k, -p[1] * k)));
    ctx.closePath();
  } else if (shape.type === 'rect') {
    const l = Math.max((shape.length || 0.44) * k, 8), w = Math.max((shape.width || 0.30) * k, 6);
    ctx.rect(-l / 2, -w / 2, l, w);
  } else if (shape.type === 'rotor') {
    const r = Math.max((shape.radius || 0.30) * k, 7);
    ctx.arc(0, 0, r * 0.38, 0, Math.PI * 2);
    ctx.moveTo(-r, -r); ctx.lineTo(r, r); ctx.moveTo(r, -r); ctx.lineTo(-r, r);
    [[-r, -r], [r, r], [r, -r], [-r, r]].forEach(([x, y]) => {
      ctx.moveTo(x + r * 0.34, y);
      ctx.arc(x, y, r * 0.34, 0, Math.PI * 2);
    });
  } else {
    ctx.arc(0, 0, Math.max((shape.radius || 0.25) * k, 6), 0, Math.PI * 2);
  }
  ctx.fillStyle = withAlpha(colour, 0.2);
  ctx.fill();
  ctx.strokeStyle = colour;
  ctx.lineWidth = 1.6;
  ctx.stroke();
}

function agents(ctx, T, list, state) {
  ctx.font = MONO;
  list.forEach(a => {
    const x = T.toX(a.x || 0), y = T.toY(a.y || 0);
    const kind = String(a.kind || 'ugv').toLowerCase();
    const colour = a.stale ? token('--dim', '#4a5f69') : token(kind.endsWith('uav') ? '--uav' : '--signal');
    const shape = a.shape || DEFAULT_SHAPE;

    ctx.save();
    ctx.translate(x, y);
    ctx.rotate(-(a.theta || 0));
    footprint(ctx, T, shape, colour);
    if (shape.type !== 'rotor') {
      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(Math.max((shape.length || shape.radius || 0.3) * T.scale, 9), 0);
      ctx.strokeStyle = colour;
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }
    ctx.restore();

    if (a.stale) {
      ctx.setLineDash([3, 4]);
      ctx.strokeStyle = withAlpha(token('--warn'), 0.8);
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(x, y, 16, 0, Math.PI * 2); ctx.stroke();
      ctx.setLineDash([]);
    }
    if (state.agent === a.name) {
      ctx.strokeStyle = token('--signal');
      ctx.lineWidth = 1;
      ctx.strokeRect(x - 20, y - 20, 40, 40);
    }
    ctx.fillStyle = token(a.stale ? '--muted' : '--text');
    ctx.fillText(a.name, x + 14, y + 4);
    if (a.battery !== null && a.battery !== undefined) {
      ctx.fillStyle = token(a.battery < 25 ? '--bad' : '--muted');
      ctx.fillText(pct(a.battery), x + 14, y + 15);
    }
  });
}

function summary(snap, state, step) {
  const list = snap.agents || [];
  const agent = list.find(a => a.name === state.agent);
  if (agent) {
    return html`${agent.name}  x <em>${num(agent.x)}</em>  y <em>${num(agent.y)}</em>  θ <em>${num(agent.theta)}</em>
v <em>${num(agent.v)}</em>  ω <em>${num(agent.w)}</em>  telemetry <em>${num(agent.age, 1)}s</em> old`;
  }
  const mission = state.mission && (snap.missions || []).find(m => m.id === state.mission);
  if (mission) {
    return html`${mission.id}  ${mission.assigned || 'unassigned'}  <em>${mission.status}</em>
${(mission.path || []).length} point route${mission.distance != null ? html`  <em>${num(mission.distance, 1)}</em> m` : ''}  cost <em>${mission.cost ?? '—'}</em>`;
  }
  return html`${list.length} agent(s), ${(snap.obstacles || []).length} obstacle(s), 1 square = ${step} m`;
}

export function createWorldCanvas(canvas, readout, state, { onPick }) {
  let T = null;

  function draw() {
    const { ctx, w, h } = surface(canvas);
    const snap = state.snap;
    if (!snap || !snap.world) return;
    T = projection(snap.world, w, h);
    const step = grid(ctx, T, snap.world);
    obstacles(ctx, T, snap.obstacles || []);
    agentPaths(ctx, T, snap.agents || [], state);
    missionRoute(ctx, T, snap.missions || [], state);
    goals(ctx, T, snap.missions || [], state.picked);
    agents(ctx, T, snap.agents || [], state);
    render(readout, summary(snap, state, step));
  }

  const pointer = event => {
    const r = canvas.getBoundingClientRect();
    return T.toWorld(event.clientX - r.left, event.clientY - r.top);
  };

  canvas.addEventListener('mousemove', event => {
    if (!T) return;
    const [x, y] = pointer(event);
    render(readout, html`pointer  x <em>${num(x)}</em>  y <em>${num(y)}</em>  click to set a goal`);
  });
  canvas.addEventListener('mouseleave', draw);
  canvas.addEventListener('click', event => {
    if (!T) return;
    onPick(pointer(event));
  });

  return { draw };
}
