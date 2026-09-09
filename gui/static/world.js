/* world.js -- the field: every agent, every observation, every planned path. */
(() => {
  const { $, n, pct, esc, kv, surface, post, get } = App;
  const cv = $('cvWorld');
  const C = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
  // Canvas silently ignores an invalid/empty fillStyle or strokeStyle (keeps
  // whatever was last set) rather than throwing - so a CSS token that isn't
  // actually declared in theme.css doesn't show up as an error, it just
  // renders as "nothing" (or whatever was drawn right before it). Every
  // colour lookup for a shape goes through this instead of C() directly.
  const Csafe = (k, fallback = '#7fd1de') => C(k) || fallback;

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
    (list || []).forEach(o => {
      const alpha = 0.25 + 0.5 * (o.confidence ?? 1);
      ctx.lineWidth = 1.2;
      ctx.strokeStyle = o.dynamic ? `rgba(224,104,95,${alpha})` : `rgba(224,160,64,${alpha})`;
      ctx.fillStyle = o.dynamic ? 'rgba(224,104,95,.12)' : 'rgba(224,160,64,.14)';
      ctx.setLineDash((o.confidence ?? 1) < 1 ? [4, 3] : []);
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

  /* `path` is optional -- the aggregate only sends it when the planner has a
     route to publish. Reading `.length` off an absent key was what produced
     "Lost the aggregate twin" the moment the first instance came up. */
  function paths(ctx, agents) {
    (agents || []).forEach(a => {
      const pts = a.path;
      if (!Array.isArray(pts) || pts.length < 2) return;
      const hot = !selected || selected === a.name;
      ctx.lineWidth = hot ? 1.8 : 1;
      ctx.strokeStyle = hot ? 'rgba(127,209,222,.75)' : 'rgba(127,209,222,.2)';
      ctx.beginPath();
      pts.forEach((p, i) => i ? ctx.lineTo(T.toX(p[0]), T.toY(p[1]))
                              : ctx.moveTo(T.toX(p[0]), T.toY(p[1])));
      ctx.stroke();
    });
  }

  function goals(ctx, missions) {
    ctx.font = '10px ui-monospace, monospace';
    (missions || []).forEach(m => {
      const wps = m.waypoints || [];
      const pts = m.goal ? [m.goal] : wps;
      pts.forEach((g, i) => {
        if (!g || g.length < 2) return;
        const x = T.toX(g[0]), y = T.toY(g[1]);
        const live = m.status === 'ACTIVE';
        ctx.strokeStyle = live ? C('--goal') : 'rgba(217,123,166,.45)';
        ctx.lineWidth = 1.2;
        ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x - 11, y); ctx.lineTo(x + 11, y);
        ctx.moveTo(x, y - 11); ctx.lineTo(x, y + 11); ctx.stroke();
        ctx.fillStyle = live ? C('--goal') : 'rgba(217,123,166,.55)';
        ctx.fillText(wps.length > 1 ? `${m.id}·${i + 1}` : m.id, x + 13, y - 6);
      });
    });
    if (picked) {
      ctx.setLineDash([3, 3]); ctx.strokeStyle = C('--goal'); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(T.toX(picked[0]), T.toY(picked[1]), 11, 0, Math.PI * 2);
      ctx.stroke(); ctx.setLineDash([]);
    }
  }

  const DEFAULT_SHAPE = { type: 'rect', length: 0.44, width: 0.30 };

  function footprint(ctx, a, colour) {
    const s = a.shape || DEFAULT_SHAPE, k = T.scale;
    ctx.beginPath();
    if (s.type === 'polygon' && Array.isArray(s.points)) {
      s.points.forEach((p, i) => i ? ctx.lineTo(p[0] * k, -p[1] * k) : ctx.moveTo(p[0] * k, -p[1] * k));
      ctx.closePath();
    } else if (s.type === 'rect') {
      const l = Math.max((s.length || 0.44) * k, 8), w = Math.max((s.width || 0.30) * k, 6);
      ctx.rect(-l / 2, -w / 2, l, w);
    } else if (s.type === 'rotor') {
      const r = Math.max((s.radius || 0.30) * k, 7);
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
    (list || []).forEach(a => {
      const x = T.toX(a.x || 0), y = T.toY(a.y || 0);
      const kind = String(a.kind || 'ugv').toLowerCase();
      const colour = a.stale ? Csafe('--dim', '#5a6b73')
        : (kind.endsWith('uav') ? Csafe('--uav', '#7fa8ff') : Csafe('--signal', '#7fd1de'));
      const shape = a.shape || DEFAULT_SHAPE;
      ctx.save();
      ctx.translate(x, y); ctx.rotate(-(a.theta || 0));
      footprint(ctx, a, colour);
      if (shape.type !== 'rotor') {                         // heading tick
        ctx.beginPath(); ctx.moveTo(0, 0);
        ctx.lineTo(Math.max((shape.length || shape.radius || .3) * T.scale, 9), 0);
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
      if (a.battery !== null && a.battery !== undefined) {
        ctx.fillStyle = a.battery < 25 ? C('--bad') : C('--muted');
        ctx.fillText(pct(a.battery), x + 14, y + 15);
      }
    });
  }

  /* ---- draw ----------------------------------------------------------- */
  function draw() {
    const { ctx, w, h } = surface(cv);
    if (!snap || !snap.world) return;
    T = fit(snap.world, w, h);
    const step = grid(ctx, snap.world, w, h);
    obstacles(ctx, snap.obstacles);
    paths(ctx, snap.agents);
    goals(ctx, snap.missions);
    agents(ctx, snap.agents);

    const list = snap.agents || [];
    const sel = list.find(a => a.name === selected);
    $('readout').innerHTML = sel
      ? `${esc(sel.name)}  x <em>${n(sel.x)}</em>  y <em>${n(sel.y)}</em>  θ <em>${n(sel.theta)}</em>\n` +
        `v <em>${n(sel.v)}</em>  ω <em>${n(sel.w)}</em>  ·  telemetry <em>${n(sel.age, 1)}s</em> old`
      : `${list.length} agent(s) · ${(snap.obstacles || []).length} obstacle(s) · 1 square = ${step} m`;
  }

  /* ---- discovery -------------------------------------------------------
     A discovery notification is a DiscoveryMessage sitting in BINDING,
     waiting for a human to confirm what the agent actually is before it's
     instantiated. The collapsed list comes for free with every /api/world
     poll (data.discoveries); the per-field candidates only get fetched
     from /api/discoveries/<id>/options when a card is opened, since
     resolving four config layers per agent isn't something we want to pay
     for on every 250ms tick for agents nobody has looked at yet.

     `discoCache[id]`  -- last options response for that id (fields spec)
     `picks[id][name]` -- the operator's current choice for one field:
                          { source: 'reported'|'instance'|'type'|'kind'|'custom',
                            raw: <string, only meaningful when source==='custom'> }
     `discoSeen`       -- ids the operator has already opened once, so the
                          "new" marker only shows for things nobody has looked
                          at yet. In-memory only (a page refresh re-surfaces
                          "new" for anything still pending, which is fine --
                          nothing is lost, it's just a look-at-me hint). */
  let discoCache = {}, picks = {};
  const discoSeen = new Set();

  function summarize(v) {
    if (v === null || v === undefined) return '—';
    if (Array.isArray(v)) return v.length ? `${v.length} item${v.length === 1 ? '' : 's'}` : 'empty list';
    if (typeof v === 'object') {
      const name = v.name ?? Object.keys(v)[0] ?? '—';
      const rest = Object.entries(v).filter(([k]) => k !== 'name').slice(0, 2)
        .map(([k, val]) => `${k}=${val}`).join(' ');
      return rest ? `${name} · ${rest}` : String(name);
    }
    return String(v);
  }

  function seedRaw(spec, value) {
    if (spec.type === 'object' || spec.type === 'list') return JSON.stringify(value ?? null);
    return (value === null || value === undefined) ? '' : String(value);
  }

  function discoNote(id, msg, bad) {
    const p = $('dnote-' + id);
    if (!p) return;
    p.className = bad ? 'empty error' : 'empty ok';
    p.textContent = msg;
  }

  function fieldRowHtml(id, name, spec) {
    const opts = (spec.candidates || []).map(c => `
      <button class="fopt" data-field="${esc(name)}" data-source="${esc(c.source)}"
              aria-pressed="${spec.selected === c.source}" type="button">
        ${esc(summarize(c.value))}<span class="src">${esc(c.label || c.source)}</span>
      </button>`).join('');
    const isCustom = spec.selected === 'custom';
    return `
      <div class="frow">
        <div class="flabel">${esc(name)}</div>
        <div class="fopts" data-fieldgroup="${esc(name)}">
          ${opts}
          <button class="fopt" data-field="${esc(name)}" data-source="custom"
                  aria-pressed="${isCustom}" type="button">write my own<span class="src">custom</span></button>
        </div>
        <div class="fcustom${isCustom ? ' show' : ''}" id="fcustom-${id}-${esc(name)}">
          ${spec.type === 'object' || spec.type === 'list'
            ? `<textarea rows="2" data-custom="${esc(name)}" placeholder="JSON"></textarea>`
            : `<input data-custom="${esc(name)}" placeholder="value">`}
        </div>
      </div>`;
  }

  function wireField(id, name, spec, formEl) {
    const seed = (spec.candidates || []).find(c => c.source === spec.selected);
    picks[id][name] = { source: spec.selected, raw: seedRaw(spec, seed ? seed.value : null) };

    const group = formEl.querySelector(`[data-fieldgroup="${CSS.escape(name)}"]`);
    const customBox = $(`fcustom-${id}-${name}`);
    const customInput = customBox.querySelector('[data-custom]');
    customInput.value = picks[id][name].raw;

    group.querySelectorAll('.fopt').forEach(btn => btn.onclick = () => {
      const src = btn.dataset.source;
      picks[id][name].source = src;
      group.querySelectorAll('.fopt').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
      customBox.classList.toggle('show', src === 'custom');
      if (src === 'custom' && !customInput.value) {
        const fallback = (spec.candidates || [])[0];
        customInput.value = fallback ? seedRaw(spec, fallback.value) : '';
        picks[id][name].raw = customInput.value;
      }
    });

    customInput.oninput = () => {
      picks[id][name] = { source: 'custom', raw: customInput.value };
      group.querySelectorAll('.fopt').forEach(b =>
        b.setAttribute('aria-pressed', String(b.dataset.source === 'custom')));
      customBox.classList.add('show');
    };
  }

  function renderDiscoveryForm(id, formEl) {
    const fields = (discoCache[id] || {}).fields || {};
    picks[id] = picks[id] || {};
    formEl.innerHTML = Object.entries(fields).map(([name, spec]) => fieldRowHtml(id, name, spec)).join('')
      + `<div class="dactions">
           <button class="primary" data-accept="${id}" type="button">Accept</button>
           <button class="ghost" data-reject="${id}" type="button">Reject</button>
         </div>
         <p class="empty" id="dnote-${id}"></p>`;
    Object.entries(fields).forEach(([name, spec]) => wireField(id, name, spec, formEl));
    formEl.querySelector(`[data-accept="${id}"]`).onclick = () => submitDiscovery(id);
    formEl.querySelector(`[data-reject="${id}"]`).onclick = () => rejectDiscovery(id);
  }

  async function loadDiscoveryFields(id, formEl) {
    if (discoCache[id]) { renderDiscoveryForm(id, formEl); return; }
    formEl.innerHTML = '<p class="empty">Loading fields…</p>';
    try {
      const data = await get(`/api/discoveries/${encodeURIComponent(id)}/options`);
      discoCache[id] = data;
      renderDiscoveryForm(id, formEl);
    } catch (e) {
      formEl.innerHTML = `<p class="empty error">${esc(e.message)}</p>`;
    }
  }

  async function submitDiscovery(id) {
    const fields = (discoCache[id] || {}).fields || {};
    const payload = {};
    let badField = null;
    Object.entries(fields).forEach(([name, spec]) => {
      const pick = (picks[id] || {})[name] || { source: spec.selected };
      if (pick.source !== 'custom') { payload[name] = { source: pick.source }; return; }
      const raw = pick.raw ?? '';
      if (spec.type === 'object' || spec.type === 'list') {
        try { payload[name] = { source: 'custom', value: raw.trim() ? JSON.parse(raw) : null }; }
        catch (_) { badField = name; }
      } else if (spec.type === 'number') {
        payload[name] = { source: 'custom', value: raw === '' ? null : Number(raw) };
      } else {
        payload[name] = { source: 'custom', value: raw };
      }
    });
    if (badField) { discoNote(id, `"${badField}" isn't valid JSON.`, true); return; }
    try {
      const r = await post(`/api/discoveries/${encodeURIComponent(id)}/resolve`, { fields: payload });
      discoNote(id, `Accepted as ${r.agent_name || id}.`, false);
      delete discoCache[id];
    } catch (e) { discoNote(id, e.message, true); }
  }

  async function rejectDiscovery(id) {
    try {
      await post(`/api/discoveries/${encodeURIComponent(id)}/reject`, {});
      discoNote(id, 'Rejected.', false);
    } catch (e) { discoNote(id, e.message, true); }
  }

  function discoveryRow(d) {
    const isNew = !discoSeen.has(d.id);
    const fieldCount = (d.reported_fields || []).length;
    return `
      <div class="row" data-discovery="${esc(d.id)}">
        <div class="hd" data-toggle="${esc(d.id)}">
          <span>${esc(d.agent_name || d.id)}</span><span class="tag">${esc(d.kind || '?')}</span>
        </div>
        <div class="sub">
          <span>${esc(d.agent_type || 'type unset')}</span>
          <span class="${isNew ? 'warn' : ''}">${isNew ? 'new · ' : ''}${fieldCount} field${fieldCount === 1 ? '' : 's'} reported</span>
        </div>
        <div class="dform" id="dform-${esc(d.id)}"></div>
      </div>`;
  }
  let lastDiscoSig = '';

  function discoveries(list) {
    list = list || [];
    // Create a signature of the IDs currently pending
    const sig = list.map(d => d.id).join(',');

    // If an operator is currently typing or has a form open, DO NOT wipe the DOM
    const isInteracting = document.querySelector('#discoveryBox .dform.open');
    if (sig === lastDiscoSig || isInteracting) {
    // Still update the badge counter in case the count changed
    $('discoveryCount').textContent = `${list.length} waiting`;
    return;
    }

  lastDiscoSig = sig;
    $('discoveryCount').textContent = `${list.length} waiting`;
    $('discoveryBox').innerHTML = list.length
      ? list.map(discoveryRow).join('')
      : '<p class="empty">No agents announcing themselves right now.</p>';

    $('discoveryBox').querySelectorAll('[data-toggle]').forEach(h => h.onclick = () => {
      const id = h.dataset.toggle;
      discoSeen.add(id);
      const form = $('dform-' + id);
      const opening = !form.classList.contains('open');
      form.classList.toggle('open');
      if (opening) loadDiscoveryFields(id, form);
    });
  }

  /* ---- side panels ---------------------------------------------------- */
  function fleet(list) {
    list = list || [];
    $('fleetCount').textContent = `${list.length} agent${list.length === 1 ? '' : 's'}`;
    $('fleetBox').innerHTML = list.length ? list.map(a => `
      <div class="row${selected === a.name ? ' sel' : ''}" data-agent="${esc(a.name)}"
           data-state="${a.stale ? 'stale' : 'live'}" style="cursor:pointer">
        <div class="hd"><span>${esc(a.name)}</span><span class="tag">${esc(a.kind)}</span></div>
        <div class="sub"><span>${n(a.x)}, ${n(a.y)}</span>
          <span class="${a.battery != null && a.battery < 25 ? 'error' : ''}">${pct(a.battery)}</span></div>
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
    list = list || [];
    $('missionCount').textContent = list.length;
    const cls = { ACTIVE: 'ok', PENDING: 'info', FAILED: 'error', CANCELLED: 'warn', COMPLETE: 'ok' };
    $('missionBox').innerHTML = list.length ? list.map(m => `
      <div class="row" data-state="${m.status === 'ACTIVE' ? 'live' : m.status === 'FAILED' ? 'lost' : 'idle'}">
        <div class="hd"><span>${esc(m.id)}</span>
          <span class="tag ${cls[m.status] || ''}">${esc(m.status)}</span></div>
        <div class="sub"><span>${esc(m.type)} · ${esc(m.posture)}</span>
          <span>${m.goal ? m.goal.map(v => n(v, 1)).join(', ') : (m.waypoints || []).length + ' wp'}</span></div>
        <div class="sub"><span>${esc(m.assigned || 'unassigned')}${m.in_flight ? ' · handshake' : ''}</span>
          <span>${m.cost === null || m.cost === undefined ? '—' : 'cost ' + m.cost}</span></div>
        <div class="sub"><span></span>
          <button class="ghost" data-cancel="${esc(m.id)}">Cancel</button></div>
      </div>`).join('')
      : '<p class="empty">Nothing dispatched yet.</p>';

    $('missionBox').querySelectorAll('[data-cancel]').forEach(b => b.onclick = async () => {
      try { await post('/api/missions/cancel', { mission_id: b.dataset.cancel }); note('Cancelled.'); }
      catch (e) { note(e.message, true); }
    });
  }

  /* one row per report - the same obstacle can appear more than once if
     several agents (or the static world file) all reported it separately;
     `source` tells them apart ('world' for ground truth, else the
     reporting agent's name). */
  function obstaclesPanel(list) {
    list = list || [];
    $('obstacleCount').textContent = list.length;
    $('obstacleBox').innerHTML = list.length ? list.map(o => `
      <div class="row">
        <div class="hd"><span>${esc(o.id)}</span>
          <span class="tag${o.dynamic ? ' warn' : ''}">${o.dynamic ? 'dynamic' : 'static'}</span></div>
        <div class="sub"><span>x ${n(o.x)}  y ${n(o.y)}</span>
          <span>${o.polygon ? 'polygon' : 'r ' + n(o.radius ?? 0)}</span></div>
        <div class="sub"><span>${esc(o.source)}</span>
          <span>${o.age != null ? n(o.age, 1) + 's old' : '—'}${o.confidence != null && o.confidence < 1 ? ' · ' + pct(o.confidence * 100) : ''}</span></div>
      </div>`).join('')
      : '<p class="empty">None observed yet.</p>';
  }

  function note(msg, bad) {
    const p = $('formNote');
    p.className = bad ? 'empty error' : 'empty ok';
    p.textContent = msg;
  }

  function fillOptions(o) {
    if (optionsDone || !o || !o.types || !o.postures) return;
    $('mType').innerHTML = o.types.map(t => `<option>${t}</option>`).join('');
    $('mPosture').innerHTML = o.postures.map(p => `<option>${p}</option>`).join('');
    if (o.postures.includes('COVERAGE')) $('mPosture').value = 'COVERAGE';
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
      discoveries(data.discoveries);
      fleet(data.agents);
      missions(data.missions);
      obstaclesPanel(data.obstacles);
      draw();
    }
  });
})();