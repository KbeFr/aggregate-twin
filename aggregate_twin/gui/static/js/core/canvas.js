const tokens = new Map();

/** A theme.css custom property. Canvas silently ignores an empty colour, so always fall back. */
export function token(name, fallback = '#7fd1de') {
  if (!tokens.has(name)) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (!value) return fallback;
    tokens.set(name, value);
  }
  return tokens.get(name);
}

export function withAlpha(colour, alpha) {
  const hex = colour.trim().replace('#', '');
  if (!/^[0-9a-f]{6}$/i.test(hex)) return colour;
  const [r, g, b] = [0, 2, 4].map(i => parseInt(hex.slice(i, i + 2), 16));
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Device-pixel-ratio aware 2d context, cleared and sized to the element. */
export function surface(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

export const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
