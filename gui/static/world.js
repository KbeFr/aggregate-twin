/* world.js -- the field: every agent, every observation, every planned path. */
(() => {
  const { $, n, pct, esc, kv, surface, post } = App;
  const cv = $('cvWorld');
  const C = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();

  let snap = null, T = null, selected = null, picked = null, optionsDone = false;

  /* ---- projection ---------------------------------------------------- */
  function fit(world, w, h) {
    const pad = 26;
    const scale = Math.min((w - 2 * pad) / Math.max(world.width, 1e-6),
                           (h - 2 * pad) / Math.max(world.height, 1e-6));
    const ox = (w - world.width * scale) / 2 - world.origin_x * scale;
    const oy = h - (h - world.height * scale) / 2 + world.origin_y * scale;
    return {
      scale,
      toX: x => ox + x * scale,
      toY: y => oy - y * scale,
      toWorld: (px, py) => [(px - ox) / scale, (oy - py) / scale]
    };
  }

  /* ---- primitives ----------------------------------------------------- */
  function grid(ctx, world, w, h) {
    const stepTarget = 40 / T.scale;
    const nice = [0.5, 1, 2, 5, 10, 20, 50].find(s => s >= stepTarget) || 100;
    ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(127,209,222,.06)';
    ctx.beginPath();
    for (let x = world.origin_x; x <= world.origin_x + world.width + 1e-9; x += nice) {
      ctx.moveTo(T.toX(x), T.toY(world.origin_y));
      ctx.lineTo(T.toX(x), T.toY(world.origin_y + world.height));
    }
    for (let y = world.origin_y; y <= world.origin_y + world.height + 1e-9; y += nice) {
      ctx.moveTo(T.toX(world.origin_x), T.toY(y));
      ctx.lineTo(T.toX(world.origin_x + world.width), T.toY(y));
    }
    ctx.stroke();
    ctx.strokeStyle = 'rgba(127,209,222,.22)'; ctx.lineWidth = 1.2;
    ctx.strokeRect(T.toX(world.origin_x), T.toY(world.origin_y + world.height),
                   world.width * T.scale, world.height * T.scale);
    return nice;
  }

  function obstacles(ctx, list) {
    list.forEach(o => {
      const alpha = 0.25 + 0.5 * (o.confidence ?? 1);
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = o.dynamic ? `rgba(224,104,95,${alpha})` : `rgba(224,160,64,${alpha})`;
      ctx.fillStyle = o.dynamic ? 'rgba(224,104,95,.12)' : 'rgba(224,160,64,.14)';
      ctx.setLineDash(o.confidence < 1 ? [4, 3] : []);
      ctx.beginPath();
      if (o.polygon && o.polygon.length > 2) {
        o.polygon.forEach((p, i) => i ? ctx.lineTo(T.toX(p[0]), T.toY(p[1]))
                                      : ctx.moveTo(T.toX(p[0]), T.toY(p[1])));
        ctx.closePath();
      } else {
        ctx.arc(T.toX(o.x), T.toY(o.y), Math.max((o.radius || 0.25) * T.scale, 3), 0, Math.PI * 2);
      }
      ctx.fill(); ctx.stroke(); ctx.setLineDash([]);
    });
  }

  function paths(ctx, agents) {
    agents.forEach(a => {
      if (!a.path.length) return;
      const hot = !selected || selected === a.name;
      ctx.lineWidth = hot ? 1.8 : 1;
      ctx.strokeStyle = hot ? 'rgba(127,209,222,.75)' : 'rgba(127,209,222,.2)';
      ctx.beginPath();
      a.path.forEach((p, i) => i ? ctx.lineTo(T.toX(p[0]), T.toY(p[1]))
                                 : ctx.moveTo(T.toX(p[0]), T.toY(p[1])));
      ctx.stroke();
    });
  }

  function goals(ctx, missions) {
    ctx.font = '10px ui-monospace, monospace';
    missions.forEach(m => {
      const pts = m.goal ? [m.goal] : m.waypoints;
      pts.forEach((g, i) => {
        const x = T.toX(g[0]), y = T.toY(g[1]);
        const live = m.status === 'ACTIVE';
        ctx.strokeStyle = live ? C('--goal') : 'rgba(217,123,166,.45)';
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y);
        ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11); ctx.stroke();
        ctx.fillStyle = live ? C('--goal') : 'rgba(217,123,166,.55)';
        ctx.fillText(m.waypoints.length > 1 ? `${m.id}·${i + 1}` : m.id, x + 13, y - 6);
      });
    });
    if (picked) {
      ctx.setLineDash([3, 3]); ctx.strokeStyle = C('--goal'); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(T.toX(picked[0]), T.toY(picked[1]), 11, 0, Math.PI * 2);
      ctx.stroke(); ctx.setLineDash([]);
    }
  }

  function footprint(ctx, a, colour) {
    const s = a.shape, k = T.scale;
    ctx.beginPath();
    if (s.type === 'polygon') {
      s.points.forEach((p, i) => i ? ctx.lineTo(p[0] * k, -p[1] * k) : ctx.moveTo(p[0] * k, -p[1] * k));
      ctx.closePath();
    } else if (s.type === 'rect') {
      const l = Math.max(s.length * k, 8), w = Math.max(s.width * k, 6);
      ctx.rect(-l / 2, -w / 2, l, w);
    } else if (s.type === 'rotor') {
      const r = Math.max(s.radius * k, 7);
      ctx.arc(0, 0, r * .38, 0, Math.PI * 2);
      ctx.moveTo(-r, -r); ctx.lineTo(r, r); ctx.moveTo(r, -r); ctx.lineTo(-r, r);
      [[-r, -r], [r, r], [r, -r], [-r, r]].forEach(([x, y]) => {
        ctx.moveTo(x + r * .34, y); ctx.arc(x, y, r * .34, 0, Math.PI * 2);
      });
    } else {
      ctx.arc(0, 0, Math.max((s.radius || .25) * k, 6), 0, Math.PI * 2);
    }
    ctx.fillStyle = colour.replace('rgb', 'rgba').replace(')', ',.2)');
    ctx.fill(); ctx.strokeStyle = colour; ctx.lineWidth = 1.6; ctx.stroke();
  }

  function agents(ctx, list) {
    ctx.font = '10px ui-monospace, monospace';
    list.forEach(a => {
      const x = T.toX(a.x), y = T.toY(a.y);
      const colour = a.stale ? C('--dim') : (a.kind.toLowerCase().endsWith('uav') ? C('--uav') : C('--signal'));
      ctx.save();
      ctx.translate(x, y); ctx.rotate(-a.theta);
      footprint(ctx, a, colour);
      if (a.shape.type !== 'rotor') {                       // heading tick
        ctx.beginPath(); ctx.moveTo(0, 0);
        ctx.lineTo(Math.max((a.shape.length || a.shape.radius || .3) * T.scale, 9), 0);
        ctx.strokeStyle = colour; ctx.lineWidth = 1.4; ctx.stroke();
      }
      ctx.restore();

      if (a.stale) {
        ctx.setLineDash([3, 4]); ctx.strokeStyle = 'rgba(224,160,64,.8)'; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(x, y, 16, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
      }
      if (selected === a.name) {
        ctx.strokeStyle = C('--signal'); ctx.lineWidth = 1;
        ctx.strokeRect(x - 20, y - 20, 40, 40);
      }
      ctx.fillStyle = a.stale ? C('--muted') : C('--text');
      ctx.fillText(a.name, x + 14, y + 4);
      if (a.battery !== null) {
        ctx.fillStyle = a.battery < 25 ? C('--bad') : C('--muted');
        ctx.fillText(pct(a.battery), x + 14, y + 15);
      }
    });
  }

  /* ---- draw ----------------------------------------------------------- */
  function draw() {
    const { ctx, w, h } = surface(cv);
    if (!snap) return;
    T = fit(snap.world, w, h);
    const step = grid(ctx, snap.world, w, h);
    obstacles(ctx, snap.obstacles);
    paths(ctx, snap.agents);
    goals(ctx, snap.missions);
    agents(ctx, snap.agents);

    const sel = snap.agents.find(a => a.name === selected);
    $('readout').innerHTML = sel
      ? `${esc(sel.name)}  x <em>${n(sel.x)}</em>  y <em>${n(sel.y)}</em>  θ <em>${n(sel.theta)}</em>\n` +
        `v <em>${n(sel.v)}</em>  ω <em>${n(sel.w)}</em>  ·  telemetry <em>${n(sel.age, 1)}s</em> old`
      : `${snap.agents.length} agent(s) · ${snap.obstacles.length} obstacle(s) · 1 square = ${step} m`;
  }

  /* ---- side panels ---------------------------------------------------- */
  function fleet(list) {
    $('fleetCount').textContent = `${list.length} agent${list.length === 1 ? '' : 's'}`;
    $('fleetBox').innerHTML = list.length ? list.map(a => `
      <div class="row${selected === a.name ? ' sel' : ''}" data-agent="${esc(a.name)}"
           data-state="${a.stale ? 'stale' : 'live'}" style="cursor:pointer">
        <div class="hd"><span>${esc(a.name)}</span><span class="tag">${esc(a.kind)}</span></div>
        <div class="sub"><span>${n(a.x)}, ${n(a.y)}</span>
          <span class="${a.battery !== null && a.battery < 25 ? 'error' : ''}">${pct(a.battery)}</span></div>
        <div class="sub"><span>${esc(a.instance || 'no instance')}</span>
          <span>${a.arrived ? 'arrived' : (a.mission_id ? esc(a.mission_id) : 'idle')}</span></div>
      </div>`).join('')
      : '<p class="empty">No agents linked. Waiting for discovery.</p>';

    $('fleetBox').querySelectorAll('.row').forEach(r => r.onclick = () => {
      selected = selected === r.dataset.agent ? null : r.dataset.agent;
      fleet(list); draw();
    });
  }

  function missions(list) {
    $('missionCount').textContent = list.length;
    const cls = { ACTIVE: 'ok', PENDING: 'info', FAILED: 'error', CANCELLED: 'warn', COMPLETE: 'ok' };
    $('missionBox').innerHTML = list.length ? list.map(m => `
      <div class="row" data-state="${m.status === 'ACTIVE' ? 'live' : m.status === 'FAILED' ? 'lost' : 'idle'}">
        <div class="hd"><span>${esc(m.id)}</span>
          <span class="tag ${cls[m.status] || ''}">${esc(m.status)}</span></div>
        <div class="sub"><span>${esc(m.type)} · ${esc(m.posture)}</span>
          <span>${m.goal ? m.goal.map(v => n(v, 1)).join(', ') : m.waypoints.length + ' wp'}</span></div>
        <div class="sub"><span>${esc(m.assigned || 'unassigned')}${m.in_flight ? ' · handshake' : ''}</span>
          <span>${m.cost === null ? '—' : 'cost ' + m.cost}</span></div>
        <div class="sub"><span></span>
          <button class="ghost" data-cancel="${esc(m.id)}">Cancel</button></div>
      </div>`).join('')
      : '<p class="empty">Nothing dispatched yet.</p>';

    $('missionBox').querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => {
      try { await post('/api/missions/cancel', { mission_id: b.dataset.cancel }); note('Cancelled.'); }
      catch (e) { note(e.message, true); }
    });
  }

  function note(msg, bad) {
    const p = $('formNote');
    p.className = bad ? 'empty error' : 'empty ok';
    p.textContent = msg;
  }

  function fillOptions(o) {
    if (optionsDone) return;
    $('mType').innerHTML = o.types.map(t => `<option>${t}</option>`).join('');
    $('mPosture').innerHTML = o.postures.map(p => `<option>${p}</option>`).join('');
    $('mPosture').value = 'COVERAGE';
    $('mType').onchange = typeChanged;
    typeChanged();
    optionsDone = true;
  }

  function typeChanged() {
    const t = $('mType').value;
    $('goalFields').hidden = !(t === 'GOTO_WAYPOINT' || t === 'TIME_GATED_GOTO');
    $('wpField').hidden = t !== 'COVERAGE_PATROL';
    $('targetField').hidden = t !== 'TRACK_TARGET';
    $('unlockField').hidden = t !== 'TIME_GATED_GOTO';
  }

  /* ---- interaction ---------------------------------------------------- */
  cv.addEventListener('mousemove', e => {
    if (!T) return;
    const r = cv.getBoundingClientRect();
    const [x, y] = T.toWorld(e.clientX - r.left, e.clientY - r.top);
    $('readout').innerHTML = `pointer  x <em>${n(x)}</em>  y <em>${n(y)}</em>  · click to set a goal`;
  });
  cv.addEventListener('mouseleave', draw);
  cv.addEventListener('click', e => {
    if (!T) return;
    const r = cv.getBoundingClientRect();
    const [x, y] = T.toWorld(e.clientX - r.left, e.clientY - r.top);
    picked = [x, y];
    const t = $('mType').value;
    if (t === 'COVERAGE_PATROL') {
      const box = $('mWaypoints');
      box.value = (box.value ? box.value + '  ' : '') + `${x.toFixed(1)},${y.toFixed(1)}`;
      note('Waypoint added.');
    } else {
      $('mX').value = x.toFixed(2); $('mY').value = y.toFixed(2);
      note(`Goal set to ${x.toFixed(1)}, ${y.toFixed(1)}.`);
    }
    draw();
  });

  $('btnDispatch').onclick = async () => {
    const t = $('mType').value;
    const wp = ($('mWaypoints').value || '').trim().split(/\s+/).filter(Boolean)
      .map(p => p.split(',').map(Number)).filter(p => p.length === 2 && p.every(v => !isNaN(v)));
    const spec = {
      mission_id: $('mId').value, type: t, posture: $('mPosture').value,
      goal_xy: ($('mX').value !== '' && $('mY').value !== '')
        ? [Number($('mX').value), Number($('mY').value)] : null,
      waypoints: wp,
      target_id: $('mTarget').value || null,
      unlock_time: $('mUnlock').value || 0,
      battery_budget: $('mBudget').value || null
    };
    try {
      const r = await post('/api/missions', spec);
      note(`Queued ${r.mission_id} — planning next tick.`);
      $('mId').value = ''; $('mWaypoints').value = ''; picked = null;
    } catch (e) { note(e.message, true); }
  };

  $('btnReplan').onclick = async () => {
    try { await post('/api/replan'); note('Replanning the whole fleet.'); }
    catch (e) { note(e.message, true); }
  };

  App.register('world', {
    endpoint: '/api/world',
    resize: draw,
    render(data) {
      snap = data;
      fillOptions(data.options);
      fleet(data.agents);
      missions(data.missions);
      draw();
    }
  });
})();
