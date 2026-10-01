import { $ } from '../core/dom.js';
import { post } from '../core/api.js';
import { registerSheet } from '../core/shell.js';
import { createWorldCanvas } from './world-canvas.js';
import { createWorldPanels } from './world-panels.js';
import { createDispatchForm } from './dispatch-form.js';

export function registerWorldSheet() {
  const state = { snap: null, agent: null, mission: null, picked: null };

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
