import { $ } from '../core/dom.js';
import { post } from '../core/api.js';
import { registerSheet } from '../core/shell.js';
import { createWorldCanvas } from './world-canvas.js';
import { createWorldPanels } from './world-panels.js';
import { createDispatchForm } from './dispatch-form.js';

const FOV_KEY = 'hdt.world.showFov';

function readFov() {
  try { return localStorage.getItem(FOV_KEY) !== '0'; } catch { return true; }
}

export function registerWorldSheet() {
  // showFov: master switch in the map legend; fovHidden: agents switched off in the fleet list
  const state = { snap: null, agent: null, mission: null, picked: null, showFov: readFov(), fovHidden: new Set() };

  const redraw = () => { panels.render(); canvas.draw(); };
  const form = createDispatchForm(state, { onChange: () => canvas.draw() });
  const canvas = createWorldCanvas($('cvWorld'), $('readout'), state, { onPick: form.pick });
  const panels = createWorldPanels(state, {
    onSelect: redraw,
    onCancel: async id => {
      try {
        await post('/api/missions/cancel', { mission_id: id });
        form.note(`Cancelling ${id}.`);
      } catch (e) { form.note(e.message, true); }
    },
  });

  const fovButton = $('btnFov');
  const syncFov = () => fovButton.setAttribute('aria-pressed', String(state.showFov));
  syncFov();
  fovButton.addEventListener('click', () => {
    state.showFov = !state.showFov;
    try { localStorage.setItem(FOV_KEY, state.showFov ? '1' : '0'); } catch { /* per-viewer only */ }
    syncFov();
    canvas.draw();
  });

  registerSheet('world', {
    endpoint: '/api/world',
    activate: () => canvas.draw(),
    resize: () => canvas.draw(),
    render(data) {
      state.snap = data;
      form.loadOptions(data.options);
      redraw();
    },
  });
}
