import { $ } from '../core/dom.js';
import { post } from '../core/api.js';
import { reducedMotion } from '../core/canvas.js';
import { isActive, refresh, registerSheet } from '../core/shell.js';
import { toast } from '../core/toast.js';
import { openReview } from '../discovery/review-panel.js';
import { createNetworkGraph } from './network-graph.js';
import { createNetworkTables } from './network-tables.js';
import { createPairingPanel } from './pairing-panel.js';
import { MODE_LABELS } from './pairing-copy.js';

export function registerNetworkSheet() {
  const actions = {
    async link(agent, instance) {
      try {
        await post('/api/pairing/link', { agent, instance });
        toast(`Linking ${agent} with ${instance}.`, 'ok');
        refresh();
        return true;
      } catch (e) {
        toast(e.message, 'bad');
        return false;
      }
    },
    async setMode(mode) {
      try {
        await post('/api/pairing/mode', { mode });
        toast(`Pairing set to ${MODE_LABELS[mode]}.`, 'ok');
      } catch (e) {
        toast(e.message, 'bad');
      }
      refresh();
    },
    review: id => openReview(id),
    prepare: agent => pairing.prepare(agent),
    focusAgent: agent => pairing.focusAgent(agent),
    hint: message => toast(message),
  };

  const graph = createNetworkGraph($('cvNet'), $('netReadout'), actions);
  const pairing = createPairingPanel(actions);
  const tables = createNetworkTables(actions);

  const animate = t => {
    if (isActive('network') && !reducedMotion()) graph.draw(t);
    requestAnimationFrame(animate);
  };
  requestAnimationFrame(animate);

  registerSheet('network', {
    endpoint: '/api/network',
    activate: () => graph.draw(),
    resize: () => graph.draw(),
    render(data) {
      graph.update(data);
      pairing.render(data.pairing);
      tables.render(data);
      if (reducedMotion()) graph.draw();
    },
  });
}
