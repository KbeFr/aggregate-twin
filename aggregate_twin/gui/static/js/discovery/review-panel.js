import { $, $$, html, render, setText } from '../core/dom.js';
import { get, post } from '../core/api.js';
import { plural } from '../core/format.js';
import { onSnapshot, refresh } from '../core/shell.js';
import { toast } from '../core/toast.js';
import { createReviewForm } from './review-form.js';

const SETTLE_MS = 3000;

const state = {
  open: false,
  seen: false,
  autocomplete: false,
  queue: [],
  activeId: null,
  view: null,
  goneShown: false,
  forms: new Map(),
  drafts: new Map(),
  progress: new Map(),
  settled: new Map(),
  queueSignature: '',
  returnFocus: null,
};

const url = (id, action = '') => `/api/discoveries/${encodeURIComponent(id)}${action}`;

function queueItem(entry) {
  const progress = state.progress.get(entry.id);
  const need = progress
    ? `${progress[0]} of ${progress[1]} ready`
    : entry.missing ? `${plural(entry.missing, 'field')} to write` : 'all fields have a source';
  const complete = progress && progress[0] === progress[1];
  return html`
    <li>
      <button class="queue-item${complete ? ' complete' : ''}" type="button" data-id="${entry.id}"
              aria-current="${entry.id === state.activeId ? 'true' : 'false'}">
        <span class="queue-name">${entry.id}</span>
        <span class="queue-meta">${[entry.kind, entry.agent_type].filter(Boolean).join(', ') || 'unknown kind'}</span>
        <span class="queue-need">${need}</span>
        ${progress ? html`<span class="queue-bar"><i style="width:${Math.round((progress[0] / Math.max(progress[1], 1)) * 100)}%"></i></span>` : ''}
      </button>
    </li>`;
}

function renderQueue() {
  const signature = JSON.stringify([state.activeId, state.queue, [...state.progress]]);
  if (signature === state.queueSignature) return;
  state.queueSignature = signature;
  render($('reviewQueue'), state.queue.length
    ? state.queue.map(queueItem)
    : html`<li class="queue-empty">Nobody is waiting.</li>`);
  setText($('reviewSub'), state.queue.length
    ? `${plural(state.queue.length, 'agent')} waiting. Confirm each one's configuration before it can be paired.`
    : 'New agents wait here until you confirm how they are configured.');
}

function renderTrigger(arrived) {
  const trigger = $('reviewTrigger');
  const count = state.queue.length;
  trigger.classList.toggle('has', count > 0);
  setText($('reviewTriggerLabel'), count ? 'Review discoveries' : 'Discoveries');
  setText($('reviewTriggerCount'), String(count));
  trigger.setAttribute('aria-label', count ? `Review discoveries, ${count} waiting` : 'Discoveries, none waiting');
  if (arrived) {
    trigger.classList.remove('arrived');
    void trigger.offsetWidth;
    trigger.classList.add('arrived');
  }
}

function showMessage(title, text) {
  state.view = null;
  render($('reviewForm'), html`
    <div class="review-empty">
      <svg viewBox="0 0 48 48" aria-hidden="true"><circle cx="24" cy="24" r="17"/><path d="M16.5 24.5l5 5 10-11"/></svg>
      <h3>${title}</h3>
      <p>${text}</p>
    </div>`);
}

function showEmpty(caughtUp = false) {
  state.activeId = null;
  showMessage(caughtUp ? 'All caught up' : 'Nothing to review',
    state.autocomplete
      ? 'Auto-complete is on, so new agents are completed from the config files and go straight to pairing.'
      : 'When an agent announces itself, it waits here until you confirm how it is configured.');
}

function mount(id) {
  const form = state.forms.get(id);
  if (!state.drafts.has(id)) state.drafts.set(id, {});
  state.goneShown = false;
  state.view = createReviewForm(form, state.drafts.get(id), {
    onApprove: answers => approve(id, answers),
    onReject: () => reject(id),
    onProgress: (ready, total) => {
      state.progress.set(id, [ready, total]);
      renderQueue();
    },
  });
  $('reviewForm').replaceChildren(state.view.el);
  $('reviewForm').scrollTop = 0;
}

async function select(id) {
  state.activeId = id;
  renderQueue();
  if (state.forms.has(id)) {
    mount(id);
    return;
  }
  state.view = null;
  render($('reviewForm'), html`<p class="review-loading">Loading ${id}…</p>`);
  try {
    const form = await get(url(id));
    state.forms.set(id, form);
    if (state.activeId === id) mount(id);
  } catch (e) {
    if (state.activeId !== id) return;
    showMessage(e.status === 404 ? 'No longer waiting' : 'Could not load this agent', e.message);
  }
}

function settle(id) {
  state.settled.set(id, Date.now());
  state.queue = state.queue.filter(entry => entry.id !== id);
  state.forms.delete(id);
  state.drafts.delete(id);
  state.progress.delete(id);
  renderTrigger(false);
  const next = state.queue[0];
  if (next) select(next.id);
  else showEmpty(true);
  renderQueue();
  ($('reviewQueue').querySelector('[aria-current="true"]') || $('review').querySelector('[data-close]')).focus();
  refresh();
}

async function approve(id, answers) {
  const view = state.view;
  view.setBusy(true);
  try {
    await post(url(id, '/resolve'), { fields: answers });
    toast(`Approved ${id}. It is in the pairing pool now.`, 'ok');
    settle(id);
  } catch (e) {
    view.setBusy(false);
    if (e.status === 422) view.applyServerErrors(e.fields, e.message);
    else if (e.status === 404) view.markGone(e.message);
    else view.setStatus(e.message, 'error');
  }
}

async function reject(id) {
  const view = state.view;
  view.setBusy(true);
  try {
    await post(url(id, '/reject'));
    toast(`Rejected ${id}. The aggregate stops listening to it.`, 'ok');
    settle(id);
  } catch (e) {
    view.setBusy(false);
    view.setStatus(e.message, 'error');
  }
}

function sync(review) {
  const now = Date.now();
  state.settled.forEach((at, id) => { if (now - at > SETTLE_MS) state.settled.delete(id); });

  const before = new Set(state.queue.map(entry => entry.id));
  state.autocomplete = review.autocomplete;
  state.queue = review.queue.filter(entry => !state.settled.has(entry.id));
  const arrived = state.seen && state.queue.some(entry => !before.has(entry.id));
  state.seen = true;
  renderTrigger(arrived);
  if (!state.open) return;

  renderQueue();
  const active = state.activeId;
  if (active && !state.queue.some(entry => entry.id === active)) {
    if (!state.queue.length) {
      showEmpty(true);
    } else if (state.view && !state.goneShown) {
      state.goneShown = true;
      state.view.markGone(`${active} is no longer waiting: it was completed another way or went silent. Pick another agent on the left.`);
    }
  } else if (!active) {
    if (state.queue.length) select(state.queue[0].id);
    else if (state.view || !$('reviewForm').firstChild) showEmpty();
  }
}

function focusables() {
  return $$('button:not([disabled]), input, textarea, select, [tabindex="0"]', $('review'))
    .filter(el => el.offsetParent !== null);
}

export function openReview(id) {
  if (!state.open) {
    state.open = true;
    state.returnFocus = document.activeElement;
    $('review').classList.add('open');
    $('review').setAttribute('aria-hidden', 'false');
    $('reviewTrigger').setAttribute('aria-expanded', 'true');
  }
  state.queueSignature = '';
  renderQueue();
  const target = id ?? (state.queue.some(e => e.id === state.activeId) ? state.activeId : state.queue[0]?.id);
  if (target) select(target);
  else showEmpty();
  requestAnimationFrame(() => $('review').querySelector('[data-close]').focus());
}

function closeReview() {
  state.open = false;
  $('review').classList.remove('open');
  $('review').setAttribute('aria-hidden', 'true');
  $('reviewTrigger').setAttribute('aria-expanded', 'false');
  state.returnFocus?.focus?.();
}

export function initReview() {
  $('reviewTrigger').addEventListener('click', () => openReview());
  $$('[data-close]', $('review')).forEach(el => el.addEventListener('click', closeReview));
  $('reviewQueue').addEventListener('click', event => {
    const item = event.target.closest('[data-id]');
    if (item && item.dataset.id !== state.activeId) select(item.dataset.id);
  });
  document.addEventListener('keydown', event => {
    if (!state.open) return;
    if (event.key === 'Escape') {
      closeReview();
    } else if (event.key === 'Tab') {
      const items = focusables();
      const first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { last.focus(); event.preventDefault(); }
      else if (!event.shiftKey && document.activeElement === last) { first.focus(); event.preventDefault(); }
    }
  });
  onSnapshot(({ review }) => { if (review) sync(review); });
}
