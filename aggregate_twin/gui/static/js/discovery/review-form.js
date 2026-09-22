import { html, render } from '../core/dom.js';
import { plural } from '../core/format.js';
import { createFieldEditor } from './field-editor.js';

const WARN = html`<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v6M8 12v.5"/></svg>`;

function groupMarkup(title, hint, key) {
  return html`
    <div class="rgroup" data-group="${key}">
      <div class="rgroup-head"><h4>${title}</h4><p>${hint}</p></div>
      <div class="rgroup-fields"></div>
    </div>`;
}

/**
 * The form for one waiting agent. `drafts` maps field name to the editor's draft
 * and outlives the form, so an operator can switch agents without losing work.
 */
export function createReviewForm(form, drafts, { onApprove, onReject, onProgress }) {
  const root = document.createElement('div');
  root.className = 'rform';

  const missing = form.fields.filter(f => !f.candidates.length);
  const sourced = form.fields.filter(f => f.candidates.length);
  const tags = [form.agent.kind, form.agent.agent_type].filter(Boolean);

  render(root, html`
    <div class="rform-head">
      <div class="rform-title">
        <h3>${form.id}</h3>
        ${tags.map(t => html`<span class="tag">${t}</span>`)}
      </div>
      <div class="progress">
        <div class="progress-track"><i></i></div>
        <span class="progress-text"></span>
      </div>
    </div>
    <div class="rform-notice" hidden></div>
    <div class="rform-fields">
      ${missing.length ? groupMarkup('Needs a value', 'No report or config file has these. Write them yourself.', 'missing') : ''}
      ${sourced.length ? groupMarkup('Confirm the source', 'Pick where each value comes from. The highest-priority source is preselected.', 'sourced') : ''}
      ${form.fields.length ? '' : html`<p class="empty">This agent reported everything the aggregate asks for.</p>`}
    </div>
    <div class="rform-foot">
      <p class="rform-status" role="status" aria-live="polite"></p>
      <button class="button-quiet" type="button" data-reject>Reject</button>
      <button class="primary" type="button" data-approve>Approve agent</button>
    </div>`);

  const approveButton = root.querySelector('[data-approve]');
  const rejectButton = root.querySelector('[data-reject]');
  const statusLine = root.querySelector('.rform-status');
  const notice = root.querySelector('.rform-notice');
  const bar = root.querySelector('.progress-track i');
  const progressText = root.querySelector('.progress-text');

  let attempted = false;
  let confirmTimer = null;

  const editors = form.fields.map(spec => {
    drafts[spec.name] ??= {};
    const editor = createFieldEditor(spec, form.sources, drafts[spec.name], update);
    const group = spec.candidates.length ? 'sourced' : 'missing';
    root.querySelector(`[data-group="${group}"] .rgroup-fields`).append(editor.el);
    return editor;
  });

  function setStatus(text, tone = '') {
    statusLine.className = `rform-status ${tone}`;
    render(statusLine, tone === 'error' ? html`${WARN}${text}` : html`${text}`);
  }

  function update() {
    const failing = editors.filter(e => e.evaluate().error);
    const ready = editors.length - failing.length;
    const share = editors.length ? ready / editors.length : 1;
    bar.style.width = `${Math.round(share * 100)}%`;
    bar.parentElement.classList.toggle('complete', !failing.length);
    progressText.textContent = `${ready} of ${plural(editors.length, 'field')} ready`;
    onProgress?.(ready, editors.length);

    if (!failing.length) setStatus('Everything is filled in. Approve to put the agent in the pairing pool.', 'ok');
    else if (attempted) setStatus(`${plural(failing.length, 'field')} ${failing.length === 1 ? 'needs' : 'need'} attention.`, 'error');
    else setStatus(`${plural(failing.length, 'field')} still ${failing.length === 1 ? 'needs' : 'need'} a value.`);
    return failing;
  }

  approveButton.addEventListener('click', () => {
    attempted = true;
    editors.forEach(e => e.reveal());
    const failing = update();
    if (failing.length) {
      failing[0].focus();
      return;
    }
    onApprove(Object.fromEntries(editors.map(e => [e.name, e.answer()])));
  });

  rejectButton.addEventListener('click', () => {
    if (rejectButton.dataset.confirm) {
      clearTimeout(confirmTimer);
      onReject();
      return;
    }
    rejectButton.dataset.confirm = 'true';
    rejectButton.textContent = 'Click again to reject';
    confirmTimer = setTimeout(() => {
      delete rejectButton.dataset.confirm;
      rejectButton.textContent = 'Reject';
    }, 3500);
  });

  update();

  return {
    el: root,
    focusFirst() {
      (editors.find(e => e.evaluate().error) || editors[0])?.focus();
    },
    setBusy(busy) {
      approveButton.disabled = rejectButton.disabled = busy;
      root.classList.toggle('busy', busy);
    },
    applyServerErrors(fields, message) {
      attempted = true;
      editors.forEach(e => e.setServerError(fields[e.name] ?? null));
      update();
      if (message) setStatus(message, 'error');
      editors.find(e => fields[e.name])?.focus();
    },
    setStatus,
    markGone(message) {
      notice.hidden = false;
      notice.textContent = message;
      approveButton.disabled = rejectButton.disabled = true;
      root.classList.add('gone');
    },
  };
}
