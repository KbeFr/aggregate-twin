import { $, $$, html, render, setText } from '../core/dom.js';
import { MODES } from './pairing-copy.js';

const UNAVAILABLE = 'First free, Manual and Auction pick from the instance pool. '
  + 'Instances only join it with instance discovery on, so Broadcast is the only mode here.';

function optionsFor(select, values, placeholder) {
  const signature = values.join('|');
  if (select.dataset.signature === signature) return;
  const keep = select.value;
  select.dataset.signature = signature;
  render(select, html`<option value="">${placeholder}</option>${values.map(v => html`<option>${v}</option>`)}`);
  select.value = values.includes(keep) ? keep : '';
}

export function createPairingPanel(actions) {
  let pendingMode = null;

  render($('modeGroup'), MODES.map(m => html`
    <button class="mode" type="button" role="radio" aria-checked="false" data-mode="${m.id}">
      <span class="mode-dot" aria-hidden="true"></span>
      <span class="mode-label">${m.label}</span>
      <span class="mode-text">${m.text}</span>
    </button>`));

  $('modeGroup').addEventListener('click', async event => {
    const button = event.target.closest('[data-mode]');
    if (!button || button.disabled || button.getAttribute('aria-checked') === 'true') return;
    pendingMode = button.dataset.mode;
    paintMode(pendingMode);
    try {
      await actions.setMode(pendingMode);
    } finally {
      setTimeout(() => { pendingMode = null; }, 400);
    }
  });

  $('modeGroup').addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const buttons = $$('[data-mode]:not([disabled])', $('modeGroup'));
    const at = buttons.indexOf(document.activeElement);
    const step = event.key === 'ArrowDown' || event.key === 'ArrowRight' ? 1 : -1;
    buttons[(at + step + buttons.length) % buttons.length]?.focus();
    event.preventDefault();
  });

  const agentSelect = $('pairAgent'), instanceSelect = $('pairInstance'), linkButton = $('pairLink');
  const syncButton = () => { linkButton.disabled = !(agentSelect.value && instanceSelect.value); };
  agentSelect.addEventListener('change', syncButton);
  instanceSelect.addEventListener('change', syncButton);
  linkButton.addEventListener('click', async () => {
    const agent = agentSelect.value, instance = instanceSelect.value;
    if (!agent || !instance) return;
    linkButton.disabled = true;
    if (await actions.link(agent, instance)) {
      agentSelect.value = '';
      instanceSelect.value = '';
    }
    syncButton();
  });

  function paintMode(mode) {
    $$('[data-mode]', $('modeGroup')).forEach(b => {
      const on = b.dataset.mode === mode;
      b.setAttribute('aria-checked', String(on));
      b.tabIndex = on ? 0 : -1;
    });
  }

  function renderPanel(pairing) {
    const available = new Map(pairing.modes.map(m => [m.id, m.available]));
    $$('[data-mode]', $('modeGroup')).forEach(b => { b.disabled = !available.get(b.dataset.mode); });
    paintMode(pendingMode ?? pairing.mode);

    setText($('pairingDiscovery'), pairing.instance_discovery ? 'instance discovery on' : 'instance discovery off');
    const note = $('modeNote');
    note.hidden = pairing.instance_discovery;
    setText(note, UNAVAILABLE);

    const manual = (pendingMode ?? pairing.mode) === 'gui';
    $('manualPair').hidden = !manual;
    if (!manual) return;
    optionsFor(agentSelect, pairing.linkable, pairing.linkable.length ? 'Choose an agent' : 'No agent waiting');
    optionsFor(instanceSelect, pairing.free, pairing.free.length ? 'Choose an instance' : 'No free instance');
    syncButton();
  }

  function prepare(agent) {
    if ($('manualPair').hidden) return;
    if ([...agentSelect.options].some(o => o.value === agent)) agentSelect.value = agent;
    syncButton();
  }

  function focusAgent(agent) {
    prepare(agent);
    $('pairing').scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    instanceSelect.focus();
  }

  return { render: renderPanel, prepare, focusAgent };
}
