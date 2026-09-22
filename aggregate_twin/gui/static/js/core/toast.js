import { $ } from './dom.js';

let timer = null;

/** Short confirmation of something the operator did. tone: info | ok | bad */
export function toast(message, tone = 'info') {
  const el = $('toast');
  el.textContent = message;
  el.dataset.tone = tone;
  el.classList.add('show');
  clearTimeout(timer);
  timer = setTimeout(() => el.classList.remove('show'), tone === 'bad' ? 5200 : 3200);
}
