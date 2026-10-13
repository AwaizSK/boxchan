'use strict';
/* ---------- pure helpers (importable under node) ---------- */

// Incremental SSE parser. feed(text) calls onEvent(name, dataString) per dispatched event.
function createSSEParser(onEvent) {
  let buf = '', ev = '', data = [];
  return function feed(chunk) {
    buf += chunk;
    let m;
    while ((m = /\r\n|\n|\r/.exec(buf))) {
      // a lone trailing \r may be half of \r\n: wait for more input
      if (m[0] === '\r' && m.index + 1 === buf.length) return;
      const line = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      if (line === '') {
        if (data.length) onEvent(ev || 'message', data.join('\n'));
        ev = ''; data = [];
      } else if (line[0] !== ':') {
        const i = line.indexOf(':');
        const field = i < 0 ? line : line.slice(0, i);
        let val = i < 0 ? '' : line.slice(i + 1);
        if (val[0] === ' ') val = val.slice(1);
        if (field === 'event') ev = val;
        else if (field === 'data') data.push(val);
      }
    }
  };
}

// Case-insensitive match on name or category. Returns null (hide) or {start,end} of the name match
// (start=end=0 when only the category matched or the query is empty).
function matchChannel(name, category, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return { start: 0, end: 0 };
  const i = String(name).toLowerCase().indexOf(q);
  if (i >= 0) return { start: i, end: i + q.length };
  if (category && String(category).toLowerCase().indexOf(q) >= 0) return { start: 0, end: 0 };
  return null;
}

function relTime(ts, now) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.round(now - ts));
  if (s < 10) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

// Exponential backoff 1s -> 10s with +-20% jitter. rand in [0,1).
function backoffDelay(attempt, rand) {
  const base = Math.min(10000, 1000 * Math.pow(2, attempt));
  return Math.round(base * (0.8 + 0.4 * rand));
}

// Group channels by category in first-appearance order.
function groupChannels(channels) {
  const groups = [], idx = new Map();
  for (const c of channels) {
    const k = c.category || '';
    if (!idx.has(k)) { idx.set(k, groups.length); groups.push({ name: c.category || null, channels: [] }); }
    groups[idx.get(k)].channels.push(c);
  }
  return groups;
}

if (typeof module !== 'undefined') {
  module.exports = { createSSEParser, matchChannel, relTime, backoffDelay, groupChannels };
}

/* ---------- app ---------- */
if (typeof document !== 'undefined') (function () {
  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const store = {
    get(s, k) { try { return s.getItem(k); } catch (e) { return null; } },
    set(s, k, v) { try { s.setItem(k, v); } catch (e) { /* ignore */ } },
    del(s, k) { try { s.removeItem(k); } catch (e) { /* ignore */ } },
  };

  const S = {
    token: store.get(sessionStorage, 'token') || '',
    editor: store.get(localStorage, 'editor') || '',
    cfg: null,            // {rev, updated_by, updated_at, guilds:{gid:{channel_id,enabled}}}
    guilds: [],           // [{id,name,channels,voice,queue,playing,watched}]
    status: {},           // gid -> {voice,queue,playing,watched}
    gid: store.get(sessionStorage, 'gid') || '',
    conn: 'connecting',
    query: '',
    hi: null,             // highlighted channel id
    busy: false,
    rows: new Map(),      // channel id -> {li, nm, badge, name, category}
    cats: [],             // [{li, rows:[...]}]
    sig: '',              // signature of the rendered channel list
    visible: [],
    abort: null,
    started: false,
  };

  /* --- toasts --- */
  function toast(msg, opts) {
    opts = opts || {};
    const t = el('div', 'toast' + (opts.error ? ' err' : ''));
    t.appendChild(el('span', null, msg));
    if (opts.undo) {
      const b = el('button', null, 'Undo');
      b.type = 'button';
      b.addEventListener('click', () => { t.remove(); opts.undo(); });
      t.appendChild(b);
    }
    const box = $('toasts');
    box.appendChild(t);
    while (box.children.length > 3) box.firstChild.remove();
    setTimeout(() => t.remove(), opts.undo ? 8000 : 5000);
  }

  /* --- auth / api --- */
  function showAuth(msg) {
    if (S.abort) S.abort.abort();
    S.started = false;
    $('ui').hidden = true;
    $('auth').hidden = false;
    $('auth-msg').textContent = msg || '';
    $('auth-token').value = '';
    $('auth-token').placeholder = S.token ? 'leave blank to keep current' : '';
    $('auth-name').value = S.editor;
    (S.token ? $('auth-name') : $('auth-token')).focus();
  }

  class ApiError extends Error {
    constructor(status, body) { super((body && body.error) || ('http_' + status)); this.status = status; this.body = body; }
  }

  async function api(method, path, body) {
    const headers = { Authorization: 'Bearer ' + S.token, 'X-Editor': S.editor };
    const init = { method, headers };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    const r = await fetch(path, init);
    let data = null;
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (r.status === 401) { showAuth('Token rejected. Enter a valid token.'); throw new ApiError(401, data); }
    if (r.status === 429) { showAuth('Too many failed attempts. Wait a minute and try again.'); throw new ApiError(429, data); }
    if (!r.ok) throw new ApiError(r.status, data);
    return data;
  }

  /* --- model helpers --- */
  const guild = () => S.guilds.find((g) => g.id === S.gid) || null;
  const gcfg = () => (S.cfg && S.cfg.guilds[S.gid]) || { channel_id: null, enabled: true };
  const chName = (id) => {
    const g = guild();
    const c = g && g.channels.find((x) => x.id === id);
    return c ? '#' + c.name : null;
  };

  function applySnapshot(snap, force) {
    if (!snap) return;
    if (!force && S.cfg && snap.rev < S.cfg.rev) return;
    S.cfg = snap;
    renderConfig();
  }

  function applyState(d) {
    S.guilds = d.guilds || [];
    for (const g of S.guilds) S.status[g.id] = { voice: g.voice, queue: g.queue, playing: g.playing, watched: g.watched };
    if (!S.guilds.some((g) => g.id === S.gid)) S.gid = S.guilds.length ? S.guilds[0].id : '';
    S.cfg = d.config || d.snapshot;
    renderGuildSelect();
    renderList();
    renderConfig();
    renderStatus();
  }

  async function refetch() {
    try { applyState(await api('GET', '/api/state')); } catch (e) { /* surfaced via auth or reconnect */ }
  }

  /* --- rendering --- */
  function renderGuildSelect() {
    const sel = $('guild');
    sel.textContent = '';
    for (const g of S.guilds) { const o = el('option', null, g.name); o.value = g.id; sel.appendChild(o); }
    sel.value = S.gid;
    sel.hidden = S.guilds.length <= 1;
  }

  function renderList() {
    const g = guild();
    const chans = g ? g.channels : [];
    const sig = S.gid + '|' + JSON.stringify(chans);
    if (sig === S.sig) return;
    S.sig = sig;
    const list = $('list');
    list.textContent = '';
    S.rows.clear(); S.cats = [];
    const frag = document.createDocumentFragment();
    const missing = el('li', 'opt missing', 'Configured channel missing');
    missing.id = 'missing-row';
    missing.setAttribute('role', 'note');
    missing.hidden = true;
    frag.appendChild(missing);
    for (const grp of groupChannels(chans)) {
      const cat = { li: null, rows: [] };
      if (grp.name) {
        cat.li = el('li', 'cat', grp.name);
        cat.li.setAttribute('role', 'presentation');
        frag.appendChild(cat.li);
      }
      for (const c of grp.channels) {
        const li = el('li', 'opt');
        li.id = 'ch-' + c.id;
        li.dataset.id = c.id;
        li.setAttribute('role', 'option');
        li.setAttribute('aria-selected', 'false');
        li.appendChild(el('span', 'hash', '#'));
        const nm = el('span', 'nm', c.name);
        li.appendChild(nm);
        const badge = el('span', 'badge', 'Listening');
        badge.hidden = true;
        li.appendChild(badge);
        const row = { id: c.id, li, nm, badge, name: c.name, category: grp.name };
        S.rows.set(c.id, row);
        cat.rows.push(row);
        frag.appendChild(li);
      }
      S.cats.push(cat);
    }
    list.appendChild(frag);
    applyFilter();
  }

  function applyFilter() {
    const q = S.query;
    S.visible = [];
    for (const cat of S.cats) {
      let any = false;
      for (const r of cat.rows) {
        const m = matchChannel(r.name, r.category, q);
        r.li.hidden = !m;
        if (!m) continue;
        any = true;
        S.visible.push(r);
        const key = m.start + ':' + m.end;
        if (r.mkey !== key) {          // only touch the DOM when the highlight changes
          r.mkey = key;
          r.nm.textContent = '';
          if (m.end > m.start) {
            r.nm.appendChild(document.createTextNode(r.name.slice(0, m.start)));
            r.nm.appendChild(el('mark', null, r.name.slice(m.start, m.end)));
            r.nm.appendChild(document.createTextNode(r.name.slice(m.end)));
          } else r.nm.textContent = r.name;
        }
      }
      if (cat.li) cat.li.hidden = !any;
    }
    $('empty').hidden = S.visible.length > 0;
    $('empty').textContent = S.rows.size ? 'No channels match.' : 'No channels available.';
    const cur = gcfg().channel_id;
    const keep = S.visible.find((r) => r.id === S.hi);
    const pick = keep || (q ? S.visible[0] : S.visible.find((r) => r.id === cur) || S.visible[0]);
    setHi(pick ? pick.id : null, false);
  }

  function setHi(id, scroll) {
    if (S.hi && S.rows.has(S.hi)) S.rows.get(S.hi).li.classList.remove('hi');
    S.hi = id;
    const r = id && S.rows.get(id);
    const v = r ? r.li.id : '';
    for (const e of [$('list'), $('search')]) {
      if (v) e.setAttribute('aria-activedescendant', v); else e.removeAttribute('aria-activedescendant');
    }
    if (r) { r.li.classList.add('hi'); if (scroll) r.li.scrollIntoView({ block: 'nearest' }); }
  }

  function renderConfig() {
    if (!S.cfg) return;
    const c = gcfg(), cur = c.channel_id;
    for (const r of S.rows.values()) {
      const on = r.id === cur;
      r.li.classList.toggle('sel', on);
      r.li.classList.toggle('paused', on && !c.enabled);
      r.li.setAttribute('aria-selected', on ? 'true' : 'false');
      r.badge.hidden = !on;
      r.badge.textContent = c.enabled ? 'Listening' : 'Paused';
    }
    const missing = $('missing-row');
    if (missing) missing.hidden = !(cur && !S.rows.has(cur));
    const name = cur ? chName(cur) : null;
    document.title = name ? name : 'TTS Bot';
    $('title').textContent = name || (cur ? 'Channel missing' : 'No channel set');
    $('pause').textContent = c.enabled ? 'Pause' : 'Resume';
    renderMeta();
    renderControls();
  }

  function renderMeta() {
    if (!S.cfg) return;
    const by = S.cfg.updated_by;
    $('meta').textContent = 'rev ' + S.cfg.rev + (by ? ' · Last changed by ' + by + ' · ' + relTime(S.cfg.updated_at, Date.now() / 1000) : '');
  }

  function renderStatus() {
    const st = S.status[S.gid];
    $('status').textContent = st
      ? 'Voice: ' + (st.voice || 'not connected') + ' · Queue: ' + st.queue + ' · Watched: ' + st.watched + (st.playing ? ' · Playing' : '')
      : '';
  }

  const CONN_TEXT = { live: 'Live', connecting: 'Connecting…', reconnecting: 'Reconnecting…', down: 'Offline' };
  function setConn(state) {
    S.conn = state;
    $('conn').dataset.state = state;
    $('conn-text').textContent = CONN_TEXT[state];
    renderControls();
  }

  function renderControls() {
    const off = S.conn !== 'live' || S.busy || !S.cfg;
    for (const id of ['pause', 'skip', 'clear']) $(id).disabled = off;
    $('list').setAttribute('aria-disabled', off ? 'true' : 'false');
  }

  /* --- actions --- */
  async function selectChannel(id, opts) {
    opts = opts || {};
    if (!id || S.busy || S.conn !== 'live' || !S.cfg) return;
    const prev = gcfg().channel_id;
    if (id === prev) return;
    const prevRev = S.cfg.rev;
    S.busy = true;
    S.cfg.guilds[S.gid] = { ...gcfg(), channel_id: id };   // optimistic
    renderConfig();
    try {
      const d = await api('PUT', '/api/guilds/' + S.gid + '/channel', { channel_id: id, rev: prevRev });
      applySnapshot(d.snapshot, true);
      if (!opts.silent) {
        toast('Switched to ' + (chName(id) || 'channel') + (prev && chName(prev) ? ' (was ' + chName(prev) + ')' : ''),
          prev && chName(prev) ? { undo: () => selectChannel(prev, { silent: true }) } : null);
      }
    } catch (e) {
      if (e.status === 409 && e.body && e.body.snapshot) {
        applySnapshot(e.body.snapshot, true);
        toast('Changed by ' + (e.body.snapshot.updated_by || 'someone') + ' — your change was not applied', { error: true });
      } else if (e.status !== 401 && e.status !== 429) {
        S.cfg.guilds[S.gid] = { ...gcfg(), channel_id: prev };
        renderConfig();
        toast('Could not switch channel: ' + e.message, { error: true });
      }
    } finally {
      S.busy = false;
      renderControls();
    }
  }

  async function act(fn) {
    if (S.busy || S.conn !== 'live') return;
    S.busy = true; renderControls();
    try { await fn(); } catch (e) {
      if (e.status === 409 && e.body && e.body.snapshot) {
        applySnapshot(e.body.snapshot, true);
        toast('Changed by ' + (e.body.snapshot.updated_by || 'someone') + ' — your change was not applied', { error: true });
      } else if (e.status !== 401 && e.status !== 429) toast('Request failed: ' + e.message, { error: true });
    } finally { S.busy = false; renderControls(); }
  }

  const doPause = () => act(async () => {
    const d = await api('PUT', '/api/guilds/' + S.gid + '/enabled', { enabled: !gcfg().enabled, rev: S.cfg.rev });
    applySnapshot(d.snapshot, true);
  });
  const doSkip = () => act(async () => {
    const d = await api('POST', '/api/guilds/' + S.gid + '/skip');
    toast(d.skipped ? 'Skipped current message' : 'Nothing is playing');
  });
  const doClear = () => act(async () => {
    const d = await api('POST', '/api/guilds/' + S.gid + '/clear');
    toast('Cleared ' + d.cleared + ' queued message' + (d.cleared === 1 ? '' : 's'));
  });

  /* --- live events --- */
  function flash(id) {
    const r = S.rows.get(id);
    if (!r) return;
    r.li.classList.remove('flash');
    void r.li.offsetWidth;
    r.li.classList.add('flash');
  }

  function onEvent(name, raw) {
    let d;
    try { d = JSON.parse(raw); } catch (e) { return; }
    if (name === 'hello') {
      applyState({ config: d.snapshot, guilds: d.guilds });
    } else if (name === 'config') {
      const ch = d.change || {};
      applySnapshot(d.snapshot, false);
      const mine = d.snapshot.updated_by === S.editor;
      if (ch.guild_id === S.gid && !mine) {
        if (ch.field === 'channel_id') {
          flash(ch.new);
          const prev = ch.old && chName(ch.old);
          toast(d.snapshot.updated_by + ' switched to ' + (chName(ch.new) || 'no channel') + (prev ? ' (was ' + prev + ')' : ''));
        } else if (ch.field === 'enabled') {
          toast(d.snapshot.updated_by + (ch.new ? ' resumed' : ' paused') + ' the bot');
        }
      }
    } else if (name === 'channels') {
      refetch();
    } else if (name === 'status') {
      S.status = { ...S.status, ...d };
      renderStatus();
    }
  }

  async function streamOnce(signal) {
    const r = await fetch('/api/events', { headers: { Authorization: 'Bearer ' + S.token }, signal, cache: 'no-store' });
    if (r.status === 401) { showAuth('Token rejected. Enter a valid token.'); return 'auth'; }
    if (r.status === 429) { showAuth('Too many failed attempts. Wait a minute and try again.'); return 'auth'; }
    if (!r.ok || !r.body) throw new Error('http_' + r.status);
    const dec = new TextDecoder();
    const feed = createSSEParser((n, d) => { if (n === 'hello') { attempt = 0; setConn('live'); } onEvent(n, d); });
    const reader = r.body.getReader();
    let timer;
    const arm = () => { clearTimeout(timer); timer = setTimeout(() => reader.cancel().catch(() => {}), 45000); };
    arm();
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        arm();
        feed(dec.decode(value, { stream: true }));
      }
    } finally { clearTimeout(timer); }
    return 'closed';
  }

  let attempt = 0;
  async function connectLoop() {
    S.abort = new AbortController();
    const signal = S.abort.signal;
    while (!signal.aborted) {
      let res = '';
      try { res = await streamOnce(signal); } catch (e) { if (signal.aborted) return; }
      if (res === 'auth' || signal.aborted) return;
      setConn(attempt >= 3 ? 'down' : 'reconnecting');
      const wait = backoffDelay(attempt++, Math.random());
      await new Promise((ok) => setTimeout(ok, wait));
    }
  }

  function start() {
    if (S.started) return;
    S.started = true;
    $('auth').hidden = true;
    $('ui').hidden = false;
    $('who').textContent = 'Editing as ' + S.editor;
    setConn('connecting');
    attempt = 0;
    refetch().then(() => $('search').focus());
    connectLoop();
  }

  /* --- wiring --- */
  $('auth').addEventListener('submit', (e) => {
    e.preventDefault();
    const tok = $('auth-token').value.trim();
    const name = $('auth-name').value.trim().slice(0, 40);
    if (tok) { S.token = tok; store.set(sessionStorage, 'token', tok); }
    if (!S.token) { $('auth-msg').textContent = 'A token is required.'; return; }
    if (!name) { $('auth-msg').textContent = 'A name is required.'; return; }
    S.editor = name;
    store.set(localStorage, 'editor', name);
    S.started = false;
    start();
  });
  $('who').addEventListener('click', () => showAuth(''));
  $('guild').addEventListener('change', (e) => {
    S.gid = e.target.value;
    store.set(sessionStorage, 'gid', S.gid);
    S.hi = null;
    renderList(); renderConfig(); renderStatus();
  });
  $('pause').addEventListener('click', doPause);
  $('skip').addEventListener('click', doSkip);
  $('clear').addEventListener('click', doClear);
  $('search').addEventListener('input', (e) => { S.query = e.target.value; applyFilter(); });
  $('list').addEventListener('click', (e) => {
    const li = e.target.closest('.opt[data-id]');
    if (li) { setHi(li.dataset.id, false); selectChannel(li.dataset.id); }
  });

  function move(delta) {
    const v = S.visible;
    if (!v.length) return;
    let i = v.findIndex((r) => r.id === S.hi);
    i = i < 0 ? 0 : Math.min(v.length - 1, Math.max(0, i + delta));
    setHi(v[i].id, true);
  }

  document.addEventListener('keydown', (e) => {
    if ($('ui').hidden || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA');
    if (e.key === '/' && !typing) { e.preventDefault(); $('search').focus(); return; }
    const onNav = t === $('search') || t === $('list') || t === document.body;
    if (e.key === 'ArrowDown' && onNav) { e.preventDefault(); move(1); }
    else if (e.key === 'ArrowUp' && onNav) { e.preventDefault(); move(-1); }
    else if (e.key === 'Enter' && onNav && S.hi) { e.preventDefault(); selectChannel(S.hi); }
    else if (e.key === 'Escape' && t === $('search')) {
      if ($('search').value) { $('search').value = ''; S.query = ''; applyFilter(); } else $('search').blur();
    }
  });

  setInterval(renderMeta, 30000);
  if (S.token && S.editor) start(); else showAuth('');
})();
