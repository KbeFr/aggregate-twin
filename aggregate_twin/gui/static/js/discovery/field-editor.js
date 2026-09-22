import { $$, html, render } from '../core/dom.js';
import { parseCustom, toRaw, validate } from './validation.js';

const CUSTOM = 'custom';
const CHECK = html`<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3.5 8.5l3 3 6-7"/></svg>`;

let uid = 0;

const preview = value => (value !== null && typeof value === 'object'
  ? JSON.stringify(value, null, 2) : String(value));

function editorMarkup(spec, id) {
  if (spec.type === 'boolean') {
    return html`<div class="bool" role="radiogroup" aria-labelledby="${id}-name">
      <button type="button" role="radio" aria-checked="false" data-bool="true">true</button>
      <button type="button" role="radio" aria-checked="false" data-bool="false">false</button>
    </div>`;
  }
  const described = `${id}-error`;
  if (spec.type === 'string' || spec.type === 'number') {
    return html`<input class="dfield-input" type="text" spellcheck="false" autocomplete="off"
      ${spec.type === 'number' ? html`inputmode="decimal"` : ''} aria-labelledby="${id}-name"
      aria-describedby="${described}" placeholder="${spec.type === 'number' ? 'e.g. 0.5' : 'Write a value'}">`;
  }
  const example = spec.type === 'list' ? '["lidar", "imu"]' : spec.type === 'object' ? '{"length": 0.9}' : 'Any JSON value';
  return html`<textarea class="dfield-input" rows="3" spellcheck="false" aria-labelledby="${id}-name"
    aria-describedby="${described}" placeholder="${example}"></textarea>`;
}

/**
 * `draft` ({source, raw, touched}) is owned by the caller and mutated in place,
 * so answers survive switching between agents.
 */
export function createFieldEditor(spec, sources, draft, onChange) {
  const id = `field-${++uid}`;
  const candidates = spec.candidates || [];
  const candidateValue = source => candidates.find(c => c.source === source)?.value;
  const sourceIds = [...candidates.map(c => c.source), CUSTOM];

  if (draft.source === undefined) {
    draft.source = spec.selected ?? CUSTOM;
    draft.raw = draft.source === CUSTOM ? '' : toRaw(candidateValue(draft.source), spec.type);
    draft.touched = false;
  }

  let revealed = false;
  let serverError = null;

  const el = document.createElement('div');
  el.className = 'dfield';
  el.dataset.field = spec.name;
  render(el, html`
    <div class="dfield-head">
      <span class="dfield-name" id="${id}-name">${spec.name}</span>
      <span class="dfield-type">${spec.type}</span>
      ${spec.conflict ? html`<span class="dfield-flag">sources differ</span>` : ''}
      <span class="dfield-status"></span>
    </div>
    ${candidates.length
      ? html`<div class="chips" role="radiogroup" aria-labelledby="${id}-name">
          ${sourceIds.map(source => html`
            <button class="chip${source === CUSTOM ? ' chip-custom' : ''}" type="button" role="radio"
                    aria-checked="false" data-source="${source}" title="${sources[source]?.hint ?? ''}">
              ${sources[source]?.label ?? source}</button>`)}
        </div>`
      : html`<p class="dfield-help">Nothing reported or configured this. Write the value.</p>`}
    <pre class="dfield-value" hidden></pre>
    <div class="dfield-editor" hidden>${editorMarkup(spec, id)}</div>
    <p class="dfield-error" id="${id}-error" role="status"></p>`);

  const chips = $$('.chip', el);
  const valueBox = el.querySelector('.dfield-value');
  const editorBox = el.querySelector('.dfield-editor');
  const input = el.querySelector('.dfield-input');
  const bools = $$('[data-bool]', el);
  const status = el.querySelector('.dfield-status');
  const errorLine = el.querySelector('.dfield-error');

  function evaluate() {
    if (draft.source === CUSTOM) {
      const parsed = parseCustom(draft.raw, spec.type);
      if (parsed.error) return { value: null, error: parsed.error };
      return { value: parsed.value, error: validate(parsed.value, spec.rules) };
    }
    if (!candidates.some(c => c.source === draft.source)) {
      return { value: null, error: 'That source has no value for this field. Choose another.' };
    }
    const value = candidateValue(draft.source);
    return { value, error: validate(value, spec.rules) };
  }

  function paint() {
    const custom = draft.source === CUSTOM;
    chips.forEach(chip => {
      const on = chip.dataset.source === draft.source;
      chip.setAttribute('aria-checked', String(on));
      chip.tabIndex = on ? 0 : -1;
    });

    valueBox.hidden = custom;
    if (!custom) valueBox.textContent = preview(candidateValue(draft.source));
    editorBox.hidden = !custom;
    if (input && input.value !== draft.raw) input.value = draft.raw;
    if (input?.tagName === 'TEXTAREA') input.rows = Math.min(14, Math.max(3, draft.raw.split('\n').length + 1));
    bools.forEach(b => b.setAttribute('aria-checked', String(b.dataset.bool === draft.raw)));

    const { error } = evaluate();
    const message = serverError ?? error;
    const visible = message && (serverError || revealed || draft.touched || !custom);
    const state = !message ? 'ready' : visible ? 'error' : 'pending';
    el.dataset.state = state;
    render(status, state === 'ready' ? CHECK : html`${state === 'error' ? 'Fix this' : 'Needs a value'}`);
    errorLine.textContent = visible ? message : '';
    input?.setAttribute('aria-invalid', String(Boolean(visible)));
  }

  function choose(source, { focus = false } = {}) {
    if (source === CUSTOM && draft.source !== CUSTOM && !draft.raw) {
      draft.raw = toRaw(candidateValue(draft.source), spec.type);
    }
    draft.source = source;
    serverError = null;
    paint();
    onChange();
    if (focus) (source === CUSTOM ? (input || bools[0]) : chips.find(c => c.dataset.source === source))?.focus();
  }

  chips.forEach(chip => chip.addEventListener('click', () => {
    draft.touched = true;
    choose(chip.dataset.source, { focus: chip.dataset.source === CUSTOM });
  }));

  el.querySelector('.chips')?.addEventListener('keydown', event => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const at = sourceIds.indexOf(draft.source);
    draft.touched = true;
    choose(sourceIds[(at + step + sourceIds.length) % sourceIds.length]);
    chips.find(c => c.dataset.source === draft.source)?.focus();
  });

  input?.addEventListener('input', () => {
    draft.raw = input.value;
    serverError = null;
    paint();
    onChange();
  });
  input?.addEventListener('blur', () => {
    if (draft.touched) return;
    draft.touched = true;
    paint();
  });
  bools.forEach(button => button.addEventListener('click', () => {
    draft.raw = button.dataset.bool;
    draft.touched = true;
    serverError = null;
    paint();
    onChange();
  }));

  paint();

  return {
    name: spec.name,
    el,
    evaluate,
    answer() {
      const { value } = evaluate();
      return draft.source === CUSTOM ? { source: CUSTOM, value } : { source: draft.source };
    },
    reveal() { revealed = true; paint(); },
    setServerError(message) { serverError = message; paint(); },
    focus() {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
      (draft.source === CUSTOM ? (input || bools[0]) : chips.find(c => c.dataset.source === draft.source))
        ?.focus({ preventScroll: true });
    },
  };
}
