const absent = v => v === null || v === undefined || Number.isNaN(Number(v));

export const num = (v, digits = 2) => (absent(v) ? '—' : Number(v).toFixed(digits));
export const pct = v => (absent(v) ? '—' : `${Number(v).toFixed(0)}%`);
export const secs = v => (absent(v) ? '—' : `${Number(v).toFixed(1)}s`);
export const clock = t => new Date(t * 1000).toTimeString().slice(0, 8);
export const plural = (count, one, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;
