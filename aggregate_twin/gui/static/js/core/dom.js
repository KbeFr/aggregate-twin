export const $ = id => document.getElementById(id);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ESCAPES[c]);

class Markup {
  constructor(text) { this.text = text; }
  toString() { return this.text; }
}

function fragment(value) {
  if (value instanceof Markup) return value.text;
  if (Array.isArray(value)) return value.map(fragment).join('');
  if (value === null || value === undefined || value === false) return '';
  return esc(value);
}

/** Template tag: interpolations are escaped unless they are markup themselves. */
export function html(strings, ...values) {
  return new Markup(strings.reduce((out, s, i) => out + s + (i < values.length ? fragment(values[i]) : ''), ''));
}

export function render(target, markup) {
  target.innerHTML = fragment(markup);
}

export function setText(target, text) {
  if (target.textContent !== text) target.textContent = text;
}
