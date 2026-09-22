import { $, $$, setText } from '../core/dom.js';
import { post } from '../core/api.js';
import { plural } from '../core/format.js';
import { onSnapshot, refresh } from '../core/shell.js';
import { toast } from '../core/toast.js';

let enabled = false;
let busy = false;
let waiting = 0;

const switches = () => $$('[data-autocomplete]');

function describe() {
  if (enabled) return 'New agents are completed from the config files and go straight to pairing.';
  if (waiting) return `New agents wait here for you. Turning this on also completes the ${plural(waiting, 'agent')} waiting now.`;
  return 'New agents wait here until you confirm how they are configured.';
}

function paint(on) {
  enabled = Boolean(on);
  switches().forEach(sw => {
    sw.setAttribute('aria-checked', String(enabled));
    setText(sw.querySelector('.switch-state'), enabled ? 'On' : 'Off');
  });
  setText($('autoHint'), describe());
}

async function toggle() {
  if (busy) return;
  const next = !enabled;
  busy = true;
  switches().forEach(sw => sw.setAttribute('aria-busy', 'true'));
  paint(next);
  try {
    const r = await post('/api/autocomplete', { enabled: next });
    paint(r.autocomplete);
    if (!r.autocomplete) toast('Auto-complete off. New agents wait for your review.', 'ok');
    else if (r.completed) toast(`Auto-complete on. Completed ${plural(r.completed, 'waiting agent')}.`, 'ok');
    else toast('Auto-complete on. New agents skip review.', 'ok');
  } catch (e) {
    paint(!next);
    toast(`Could not change auto-complete: ${e.message}`, 'bad');
  } finally {
    busy = false;
    switches().forEach(sw => sw.removeAttribute('aria-busy'));
    refresh();
  }
}

export function initAutocomplete() {
  switches().forEach(sw => sw.addEventListener('click', toggle));
  onSnapshot(({ review }) => {
    if (!review) return;
    waiting = review.queue.length;
    if (!busy) paint(review.autocomplete);
  });
}
