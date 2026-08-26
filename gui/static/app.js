/* app.js -- shell: sheet routing, polling, shared helpers. */
const App = (() => {
  const $ = id => document.getElementById(id);
  const el = (sel, root = document) => root.querySelector(sel);

  let sheet = 'world';
  let lost = 0;
  const sheets = {};

  /* -- formatting ------------------------------------------------------ */
  const n = (v, d = 2) => (v === null || v === undefined || isNaN(v)) ? '—' : Number(v).toFixed(d);
  const pct = v => (v === null || v === undefined) ? '—' : Number(v).toFixed(0) + '%';
  const secs = v => (v === null || v === undefined) ? '—' : Number(v).toFixed(1) + 's';
  const clock = t => new Date(t * 1000).toTimeString().slice(0, 8);
  const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const kv = pairs => '<dl>' + pairs.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('') + '</dl>';

  function banner(msg, ok) {
    const b = $('banner');
    b.className = 'banner' + (msg ? ' show' : '') + (ok ? ' ok' : '');
    if (msg) b.textContent = msg;
  }

  function header(t) {
    $('twinName').innerHTML = `${esc(t.name)} <span>· ${esc(t.namespace)}</span>`;
    $('simTime').textContent = t.sim_time.toFixed(1);
    $('step').textContent = t.step;
    document.querySelectorAll('#rail b').forEach(b => {
      b.className = b.dataset.s === t.lifecycle
        ? 'on' + (t.lifecycle === 'BINDING' ? ' warn' : '') : '';
    });
    $('footStat').textContent =
      `${t.linked} linked · ${t.pending} pending · ${t.planning ? 'planning…' : 'planner idle'}`;
  }

  /* -- device-pixel-ratio aware canvas --------------------------------- */
  function surface(canvas) {
    const dpr = window.devicePixelRatio || 1;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  /* -- polling --------------------------------------------------------- */
  async function poll() {
    const active = sheets[sheet];
    try {
      const r = await fetch(active.endpoint, { cache: 'no-store' });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      header(data.twin);
      active.render(data);
      if (lost) { banner(''); lost = 0; }
    } catch (e) {
      lost = 1;
      banner('Lost the aggregate twin — the container may have stopped.');
    }
  }

  async function post(path, body) {
    const r = await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {})
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `request failed (${r.status})`);
    return data;
  }

  function select(name) {
    sheet = name;
    document.querySelectorAll('main').forEach(m => m.classList.toggle('active', m.id === name));
    document.querySelectorAll('.tab').forEach(t =>
      t.setAttribute('aria-selected', String(t.dataset.sheet === name)));
    sheets[name].resize?.();
    poll();
  }

  function register(name, spec) { sheets[name] = spec; }

  function boot() {
    document.querySelectorAll('.tab').forEach(t =>
      t.addEventListener('click', () => select(t.dataset.sheet)));
    addEventListener('resize', () => sheets[sheet].resize?.());
    select('world');
    setInterval(poll, 250);
  }

  return { $, el, n, pct, secs, clock, esc, kv, surface, post, register, boot, banner };
})();
