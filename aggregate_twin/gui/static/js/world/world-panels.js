import { $, html, render, setText } from '../core/dom.js';
import { num, pct, plural } from '../core/format.js';

const MISSION_TONE = { ACTIVE: 'ok', PENDING: 'info', FAILED: 'error', CANCELLED: 'warn', COMPLETE: 'ok' };

function fleetRow(a, state) {
  const low = a.battery != null && a.battery < 25;
  return html`
    <div class="row selectable${state.agent === a.name ? ' sel' : ''}" data-agent="${a.name}"
         data-state="${a.stale ? 'stale' : 'live'}">
      <div class="hd"><span>${a.name}</span><span class="tag">${a.kind}</span></div>
      <div class="sub"><span>${num(a.x)}, ${num(a.y)}</span><span class="${low ? 'error' : ''}">${pct(a.battery)}</span></div>
      <div class="sub"><span>${a.instance || 'no instance'}</span>
        <span>${a.arrived ? 'arrived' : (a.mission_id || 'idle')}</span></div>
      ${fovLine(a, state)}
    </div>`;
}

function fovLine(a, state) {
  const hull = a.sensor_footprint || [];
  if (hull.length < 2) return html`<div class="sub"><span class="muted">no sensor reach seen yet</span></div>`;
  const shown = !state.fovHidden.has(a.name);
  return html`
    <div class="sub"><span>sensor fov, ${hull.length} pts</span>
      <button class="ghost" type="button" data-fov="${a.name}" aria-pressed="${String(shown)}">${shown ? 'Hide fov' : 'Show fov'}</button></div>`;
}

function missionRow(m, state) {
  const hasRoute = Array.isArray(m.path) && m.path.length > 1;
  const where = m.goal ? m.goal.map(v => num(v, 1)).join(', ') : `${(m.waypoints || []).length} wp`;
  const routeText = hasRoute ? (state.mission === m.id ? 'route shown' : 'show route') : 'no route yet';
  return html`
    <div class="row selectable${state.mission === m.id ? ' sel' : ''}" data-mission="${m.id}"
         data-state="${m.status === 'ACTIVE' ? 'live' : m.status === 'FAILED' ? 'lost' : 'idle'}">
      <div class="hd"><span>${m.id}</span><span class="tag ${MISSION_TONE[m.status] || ''}">${m.status}</span></div>
      <div class="sub"><span>${m.type}, ${m.posture}</span><span>${where}</span></div>
      <div class="sub"><span>${m.assigned || 'unassigned'}${m.in_flight ? ', handshake' : ''}</span>
        <span>${m.cost == null ? '—' : `cost ${m.cost}`}</span></div>
      <div class="sub"><span class="${hasRoute ? 'info' : 'muted'}">${routeText}</span>
        <button class="ghost" type="button" data-cancel="${m.id}">Cancel</button></div>
    </div>`;
}

function obstacleRow(o) {
  const shape = o.polygon ? 'polygon' : `r ${num(o.radius ?? 0)}`;
  const confidence = o.confidence != null && o.confidence < 1 ? `, ${pct(o.confidence * 100)}` : '';
  return html`
    <div class="row">
      <div class="hd"><span>${o.id}</span><span class="tag${o.dynamic ? ' warn' : ''}">${o.dynamic ? 'dynamic' : 'static'}</span></div>
      <div class="sub"><span>x ${num(o.x)}  y ${num(o.y)}</span><span>${shape}</span></div>
      <div class="sub"><span>${o.source}</span><span>${o.age != null ? `${num(o.age, 1)}s old` : '—'}${confidence}</span></div>
    </div>`;
}

export function createWorldPanels(state, { onSelect, onCancel }) {
  $('fleetBox').addEventListener('click', event => {
    const fov = event.target.closest('[data-fov]');
    if (fov) {
      const name = fov.dataset.fov;
      if (state.fovHidden.has(name)) state.fovHidden.delete(name); else state.fovHidden.add(name);
      return onSelect();
    }
    const row = event.target.closest('[data-agent]');
    if (!row) return;
    state.agent = state.agent === row.dataset.agent ? null : row.dataset.agent;
    onSelect();
  });

  $('missionBox').addEventListener('click', event => {
    const cancel = event.target.closest('[data-cancel]');
    if (cancel) return onCancel(cancel.dataset.cancel);
    const row = event.target.closest('[data-mission]');
    if (!row) return;
    state.mission = state.mission === row.dataset.mission ? null : row.dataset.mission;
    onSelect();
  });

  function renderPanels() {
    const snap = state.snap || {};
    const fleet = snap.agents || [], missions = snap.missions || [], obstacles = snap.obstacles || [];

    setText($('fleetCount'), plural(fleet.length, 'agent'));
    render($('fleetBox'), fleet.length ? fleet.map(a => fleetRow(a, state))
      : html`<p class="empty">No agents linked. Waiting for discovery.</p>`);

    setText($('missionCount'), String(missions.length));
    render($('missionBox'), missions.length ? missions.map(m => missionRow(m, state))
      : html`<p class="empty">Nothing dispatched yet.</p>`);

    setText($('obstacleCount'), String(obstacles.length));
    render($('obstacleBox'), obstacles.length ? obstacles.map(obstacleRow)
      : html`<p class="empty">None observed yet.</p>`);
  }

  return { render: renderPanels };
}
