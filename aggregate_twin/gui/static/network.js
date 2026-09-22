/* network.js -- who is talking to whom, and how recently. */
(() => {
  const { $, n, esc, clock, secs, surface } = App;
  const cv = $('cvNet');
  const C = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
  const calm = matchMedia('(prefers-reduced-motion: reduce)').matches;

  let net = null, placed = [], hover = null, logChannel = 'handshake';

  const STATE = { live: '--ok', stale: '--warn', silent: '--bad',
                  pending: '--signal', released: '--dim' };
  const colour = s => C(STATE[s] || '--muted');

  const EMPTY_CH = { count: 0, hz: 0, age: null };

  /* ---- layout: aggregate → instances → agents ------------------------- */
  function layout(w, h) {
    const nodes = net.nodes || [];
    const inst = nodes.filter(x => x.role === 'instance');
    const agents = nodes.filter(x => x.role === 'agent');
    const agg = nodes.find(x => x.role === 'aggregate');
    const col = (list, x) => list.map((node, i) => ({
      ...node, x, y: (h * (i + 1)) / (list.length + 1)
    }));
    placed = [
      ...(agg ? [{ ...agg, x: w * 0.12, y: h / 2 }] : []),
      ...col(inst, w * 0.5),
      ...col(agents, w * 0.85)
    ];
    return Object.fromEntries(placed.filter(p => p.id).map(p => [p.id, p]));
  }

  function link(ctx, a, b, l, t) {
    if (!a || !b) return;
    const mx = (a.x + b.x) / 2;
    ctx.strokeStyle = colour(l.state);
    ctx.globalAlpha = l.state === 'released' ? .35 : .75;
    ctx.lineWidth = l.kind === 'telemetry' ? 1.6 : 1.1;
    ctx.setLineDash(l.kind === 'instantiate' ? [] : l.state === 'live' ? [] : [4, 4]);
    ctx.beginPath();
    ctx.moveTo(a.x + 13, a.y);
    ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x - 13, b.y);
    ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;

    if (l.state === 'live' && !calm) {                       // one packet in transit
      const p = (t / 1400 + (a.y % 7) / 7) % 1;
      const px = a.x + (b.x - a.x) * p, py = a.y + (b.y - a.y) * p;
      ctx.fillStyle = colour(l.state);
      ctx.beginPath(); ctx.arc(px, py, 2, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = C('--dim'); ctx.font = '9px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(l.detail || '', mx, (a.y + b.y) / 2 - 5);
    ctx.textAlign = 'left';
  }

  function node(ctx, p) {
    const c = colour(p.state), r = p.role === 'aggregate' ? 15 : 10;
    ctx.strokeStyle = c; ctx.lineWidth = hover === p.id ? 2.2 : 1.5;
    ctx.fillStyle = 'rgba(17,28,34,.9)';
    ctx.beginPath();
    if (p.role === 'aggregate') {                            // hexagon: the orchestrator
      for (let i = 0; i < 6; i++) {
        const a = (Math.PI / 3) * i - Math.PI / 6;
        i ? ctx.lineTo(p.x + r * Math.cos(a), p.y + r * Math.sin(a))
          : ctx.moveTo(p.x + r * Math.cos(a), p.y + r * Math.sin(a));
      }
      ctx.closePath();
    } else if (p.role === 'instance') {
      ctx.rect(p.x - r, p.y - r, 2 * r, 2 * r);
    } else {
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
    }
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = C('--text'); ctx.font = '10.5px ui-monospace, monospace';
    ctx.textAlign = 'center';
    ctx.fillText(p.label || p.id || '?', p.x, p.y + r + 13);
    ctx.fillStyle = C('--muted'); ctx.font = '9px ui-monospace, monospace';
    ctx.fillText(p.detail || '', p.x, p.y + r + 24);
    ctx.textAlign = 'left';
  }

  function draw(t = performance.now()) {
    const { ctx, w, h } = surface(cv);
    if (!net) return;
    const at = layout(w, h);
    ['instantiate', 'telemetry'].forEach(kind =>
      (net.links || []).filter(l => l.kind === kind)
        .forEach(l => link(ctx, at[l.source], at[l.target], l, t)));
    placed.forEach(p => node(ctx, p));

    if (placed.length <= 1) {
      ctx.fillStyle = C('--muted'); ctx.font = '11px ui-monospace, monospace';
      ctx.fillText('No instances bound yet — the aggregate is alone on the bus.', 20, h - 26);
    }
    const c = net.counters || {};
    const hovered = placed.find(p => p.id === hover);
    $('netReadout').innerHTML = hovered
      ? `${esc(hovered.label)}  ·  ${esc(hovered.role)}  ·  <em>${esc(hovered.state)}</em>\n${esc(hovered.detail || '')}`
      : `${c.msgs_in ?? 0} in / ${c.msgs_out ?? 0} out  ·  ` +
        `stale after <em>${c.stale_after ?? '—'}s</em>`;
  }

  if (!calm) (function spin() { if (net) draw(); requestAnimationFrame(spin); })();

  cv.addEventListener('mousemove', e => {
    const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const hit = placed.find(p => Math.hypot(p.x - x, p.y - y) < 18);
    hover = hit ? hit.id : null;
  });
  cv.addEventListener('mouseleave', () => { hover = null; });

  /* ---- panels ---------------------------------------------------------- */
  function counters(c) {
    c = c || {};
    const cells = [
      ['linked', c.linked ?? 0, c.linked ? 'ok' : 'muted'],
      ['degraded', c.silent ?? 0, c.silent ? 'warn' : 'muted'],
      ['msgs in', c.msgs_in ?? 0, 'info'],
      ['msgs out', c.msgs_out ?? 0, 'info'],
      ['handshakes', c.in_flight ?? 0, c.in_flight ? 'warn' : 'muted'],
      ['planner', c.planning ? 'busy' : 'idle', c.planning ? 'info' : 'muted'],
    ];
    $('counters').innerHTML = cells.map(([l, v, k]) =>
      `<div><b class="${k}">${esc(v)}</b><span>${l}</span></div>`).join('');
    if (c.planner_error) App.banner('Planner: ' + c.planner_error);
  }

  function rows(list) {
    list = list || [];
    const ch = c => { c = c || EMPTY_CH; return `${c.count}${c.hz ? ` · ${c.hz.toFixed(1)}Hz` : ''}`; };
    $('linkRows').innerHTML = list.length ? list.map(r => {
      const ts = r.twin_state || EMPTY_CH;
      const inst = r.instantiate || { out: 0, in: 0 };
      const mis = r.mission || { out: 0, in: 0 };
      return `
      <tr>
        <td>${esc(r.agent)}</td>
        <td class="muted">${esc(r.instance || '—')}</td>
        <td class="${r.state === 'live' ? 'ok' : r.state === 'stale' ? 'warn' :
                     r.state === 'silent' ? 'error' : 'info'}">${esc(r.phase)} · ${secs(r.since)}</td>
        <td class="num">${ch(r.discovery)}</td>
        <td class="num">${inst.out}↑ ${inst.in}↓</td>
        <td class="num">${mis.out}↑ ${mis.in}↓</td>
        <td class="num">${ch(ts)}</td>
        <td class="num ${ts.age != null && ts.age > 3 ? 'warn' : ''}">${secs(ts.age)}</td>
        <td class="num">${(r.obstacle || EMPTY_CH).count}</td>
      </tr>`; }).join('')
      : '<tr><td colspan="9" class="muted">Nothing has announced itself yet.</td></tr>';
  }

  function log(events) {
    events = events || [];
    $('logCount').textContent = events.length;
    $('eventLog').innerHTML = events.length ? events.slice().reverse().map(e => `
      <div><time>${clock(e.t)}</time>
        <b class="${e.dir === 'out' ? 'info' : e.level === 'ok' ? 'ok' :
                    e.level === 'warn' ? 'warn' : 'muted'}">${e.dir === 'out' ? '↑' : '↓'}</b>
        <span><b>${esc(e.kind)}</b> ${esc(e.peer)} — ${esc(e.detail)}</span></div>`).join('')
      : '<p class="empty">Quiet so far.</p>';
  }

  document.querySelectorAll('.logTab').forEach(b => b.addEventListener('click', () => {
    logChannel = b.dataset.log;
    document.querySelectorAll('.logTab').forEach(x =>
      x.setAttribute('aria-selected', String(x === b)));
    if (net) log(logChannel === 'telemetry' ? net.telemetry_events : net.events);
  }));

  App.register('network', {
    endpoint: '/api/network',
    resize: draw,
    render(data) {
      net = data;
      counters(data.counters);
      rows(data.rows);
      log(logChannel === 'telemetry' ? data.telemetry_events : data.events);
      draw();
    }
  });
})();