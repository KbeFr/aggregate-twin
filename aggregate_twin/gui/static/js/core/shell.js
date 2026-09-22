import { $, $$ } from './dom.js';
import { get } from './api.js';
import { renderHeader } from './header.js';

const POLL_MS = 250;
const sheets = new Map();
const listeners = new Set();
let current = null;
let lost = false;
let inFlight = false;

export function registerSheet(name, sheet) {
  sheets.set(name, sheet);
}

/** Called with every snapshot, whichever sheet fetched it. */
export function onSnapshot(listener) {
  listeners.add(listener);
}

export const isActive = name => current === name;

export function banner(message = '', tone = 'error') {
  const el = $('banner');
  el.className = `banner${message ? ' show' : ''}${tone === 'ok' ? ' ok' : ''}`;
  if (message) el.textContent = message;
}

export async function refresh() {
  const sheet = sheets.get(current);
  if (!sheet || inFlight) return;
  inFlight = true;
  try {
    let data;
    try {
      data = await get(sheet.endpoint);
    } catch (e) {
      lost = true;
      banner(`Cannot reach ${sheet.endpoint}: ${e.message}`);
      return;
    }
    if (lost) {
      lost = false;
      banner('');
    }
    try {
      renderHeader(data.twin);
      listeners.forEach(listener => listener(data));
      sheet.render(data);
    } catch (e) {
      console.error('[console] render failed on', sheet.endpoint, e);
      banner(`Console render error: ${e.message} (details in the browser console)`);
    }
  } finally {
    inFlight = false;
  }
}

export function select(name) {
  current = name;
  $$('main').forEach(m => m.classList.toggle('active', m.id === name));
  $$('.sheets .tab').forEach(t => t.setAttribute('aria-selected', String(t.dataset.sheet === name)));
  sheets.get(name)?.activate?.();
  refresh();
}

export function boot(initial) {
  $$('.sheets .tab').forEach(t => t.addEventListener('click', () => select(t.dataset.sheet)));
  addEventListener('resize', () => sheets.get(current)?.resize?.());
  select(initial);
  setInterval(refresh, POLL_MS);
}
