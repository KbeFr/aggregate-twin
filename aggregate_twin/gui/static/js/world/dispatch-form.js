import { $, html, render } from '../core/dom.js';
import { post } from '../core/api.js';

const NEEDS_GOAL = ['GOTO_WAYPOINT', 'TIME_GATED_GOTO'];

export function createDispatchForm(state, { onChange }) {
  let optionsLoaded = false;

  function note(message, bad = false) {
    const p = $('formNote');
    p.className = `empty note ${bad ? 'error' : 'ok'}`;
    p.textContent = message;
  }

  function typeChanged() {
    const type = $('mType').value;
    $('goalFields').hidden = !NEEDS_GOAL.includes(type);
    $('wpField').hidden = type !== 'COVERAGE_PATROL';
    $('targetField').hidden = type !== 'TRACK_TARGET';
    $('unlockField').hidden = type !== 'TIME_GATED_GOTO';
  }

  function loadOptions(options) {
    if (optionsLoaded || !options?.types || !options?.postures) return;
    render($('mType'), options.types.map(t => html`<option>${t}</option>`));
    render($('mPosture'), options.postures.map(p => html`<option>${p}</option>`));
    if (options.postures.includes('COVERAGE')) $('mPosture').value = 'COVERAGE';
    $('mType').addEventListener('change', typeChanged);
    typeChanged();
    optionsLoaded = true;
  }

  function pick([x, y]) {
    state.picked = [x, y];
    if ($('mType').value === 'COVERAGE_PATROL') {
      const box = $('mWaypoints');
      box.value = `${box.value ? `${box.value}  ` : ''}${x.toFixed(1)},${y.toFixed(1)}`;
      note('Waypoint added.');
    } else {
      $('mX').value = x.toFixed(2);
      $('mY').value = y.toFixed(2);
      note(`Goal set to ${x.toFixed(1)}, ${y.toFixed(1)}.`);
    }
    onChange();
  }

  function spec() {
    const waypoints = ($('mWaypoints').value || '').trim().split(/\s+/).filter(Boolean)
      .map(p => p.split(',').map(Number))
      .filter(p => p.length === 2 && p.every(v => !Number.isNaN(v)));
    const hasGoal = $('mX').value !== '' && $('mY').value !== '';
    return {
      mission_id: $('mId').value,
      type: $('mType').value,
      posture: $('mPosture').value,
      goal_xy: hasGoal ? [Number($('mX').value), Number($('mY').value)] : null,
      waypoints,
      target_id: $('mTarget').value || null,
      unlock_time: $('mUnlock').value || 0,
      battery_budget: $('mBudget').value || null,
    };
  }

  $('btnDispatch').addEventListener('click', async () => {
    try {
      const r = await post('/api/missions', spec());
      note(`Queued ${r.mission_id}. Planning starts next tick.`);
      $('mId').value = '';
      $('mWaypoints').value = '';
      state.picked = null;
      onChange();
    } catch (e) { note(e.message, true); }
  });

  $('btnReplan').addEventListener('click', async () => {
    try {
      await post('/api/replan');
      note('Replanning the whole fleet.');
    } catch (e) { note(e.message, true); }
  });

  return { loadOptions, pick, note };
}
