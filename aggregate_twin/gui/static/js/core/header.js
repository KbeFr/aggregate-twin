import { $, $$, html, render, setText } from './dom.js';
import { num } from './format.js';

export function renderHeader(twin = {}) {
  render($('twinName'), html`${twin.name ?? '—'} <span>${twin.namespace ?? ''}</span>`);
  setText($('simTime'), num(twin.sim_time, 1));
  setText($('step'), String(twin.step ?? 0));
  $$('#rail b').forEach(b => {
    b.className = b.dataset.s === twin.lifecycle ? `on${twin.lifecycle === 'BINDING' ? ' warn' : ''}` : '';
  });
  const parts = [`${twin.linked ?? 0} linked`, `${twin.pooled ?? 0} waiting to pair`];
  if (twin.review) parts.push(`${twin.review} to review`);
  setText($('footStat'), parts.join(', '));
}
