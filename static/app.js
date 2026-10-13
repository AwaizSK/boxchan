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

// Mirror of server-side sanitize_editor: collapse whitespace, drop control chars, trim, max 40.
function normalizeEditor(v) {
  let s = String(v == null ? '' : v).split(/\s+/).join(' ');
  s = s.replace(/[\p{C}]/gu, '');
  s = s.split(/\s+/).join(' ').trim();
  return Array.from(s).slice(0, 40).join('').trim();
}

// Should snapshot `snap` replace the current config (`cur`, may be null)? Older revs never do,
// unless forced (hello after a (re)connect, or the server's authoritative 409 snapshot).
function shouldApplySnapshot(cur, snap, force) {
  if (!snap) return false;
  if (force || !cur) return true;
  return snap.rev >= cur.rev;
}

// Roll back an optimistic channel change only if nothing newer replaced it.
function shouldRollback(currentId, optimisticId) {
  return currentId === optimisticId;
}

// 'en_US-hfc_female-medium' -> 'Hfc Female' (language prefix and quality suffix dropped).
function voiceTitle(stem) {
  const p = String(stem).split('-');
  if (p.length > 1 && /^[a-z]{2,3}(_[A-Za-z0-9]+)?$/.test(p[0])) p.shift();
  if (p.length > 1 && /^(x_low|low|medium|high)$/.test(p[p.length - 1])) p.pop();
  const t = p.join(' ').split(/[_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
  return t || String(stem);
}

// Warning for a saved voice that is not the active one. names = installed stems, or null if not scanned yet.
function voiceWarning(configured, current, names) {
  if (!configured || configured === current) return null;
  const now = current ? voiceTitle(current) : 'no voice';
  if (!names) return 'Saved voice is not active \u2014 using ' + now;
  if (names.indexOf(configured) < 0) return 'Saved voice missing \u2014 using default';
  return 'Saved voice could not be loaded \u2014 using ' + now;
}

// Why the test phrase cannot be sent right now ('' = it can).
function sayBlocker(live, st, loading) {
  if (!live) return 'Offline';
  if (!st || !st.voice) return 'Bot is not in a voice channel in this server (use !join)';
  if (loading) return 'Voice is loading';
  return '';
}

function voiceSwitchText(editor, name) {
  return editor + ' switched voice to ' + (name ? voiceTitle(name) : 'the default voice');
}

/* --- Messages tab helpers --- */
const MSG_MAX = 2000;
const READY_PRESET = "The voice bot is ready to use! Join a voice channel, type !join here, and I'll read watched users' messages aloud.";
const cpLen = (s) => Array.from(String(s)).length;

// Split text into plain and tagged parts. Only the exact "@label" of a tagged chip matches
// (longest label first, single pass, not glued to word characters); nothing is ever guessed.
function segmentText(text, tags) {
  const list = (tags || []).filter((t) => t && t.label).sort((a, b) => b.label.length - a.label.length);
  text = String(text);
  if (!list.length) return [{ text, tag: null }];
  const by = new Map();
  for (const t of list) if (!by.has('@' + t.label)) by.set('@' + t.label, t);
  const alt = Array.from(by.keys()).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  let re, useFallback = false;
  try {
    re = new RegExp('(?<![\\p{L}\\p{N}_])(?:' + alt + ')(?![\\p{L}\\p{N}_])', 'gu');
  } catch (e) {
    useFallback = true;
    re = new RegExp('(?:' + alt + ')(?![\\p{L}\\p{N}_])', 'gu');
  }
  const isWord = (c) => /[\p{L}\p{N}_]/u.test(c);
  const out = [];
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (useFallback && m.index > 0 && isWord(text[m.index - 1])) continue;
    if (m.index > last) out.push({ text: text.slice(last, m.index), tag: null });
    out.push({ text: m[0], tag: by.get(m[0]) });
    last = m.index + m[0].length;
  }
  if (last < text.length || !out.length) out.push({ text: text.slice(last), tag: null });
  return out;
}

// The content sent to the server: tagged "@label" -> "<@id>", trimmed.
function substituteTags(text, tags) {
  return segmentText(text, tags).map((s) => (s.tag ? '<@' + s.tag.id + '>' : s.text)).join('').trim();
}

// Set of tag ids whose "@label" currently appears in the text.
function activeTagIds(text, tags) {
  const ids = new Set();
  for (const s of segmentText(text, tags)) if (s.tag) ids.add(s.tag.id);
  return ids;
}

// A label that is not already used by a different member.
function uniqueLabel(name, username, id, tags) {
  const taken = (l) => (tags || []).some((t) => t.label === l && t.id !== id);
  for (const l of [name, name + ' (' + username + ')', name + ' (' + id + ')']) if (!taken(l)) return l;
  return name + ' (' + id + ')';
}

// Insert `ins` at the selection, with a space before/after when it would touch other text.
function insertAtCursor(value, start, end, ins) {
  start = Math.max(0, Math.min(start, value.length));
  end = Math.max(start, Math.min(end, value.length));
  const pre = value.slice(0, start), post = value.slice(end);
  const lead = pre && !/\s$/.test(pre) ? ' ' : '';
  const trail = post && /^\S/.test(post) ? ' ' : (post ? '' : ' ');
  const text = pre + lead + ins + trail;
  return { value: text + post, pos: text.length };
}

function counterText(n) { return n + '/' + MSG_MAX; }
function counterState(n) { return n > MSG_MAX ? 'over' : n >= 1900 ? 'near' : ''; }

function pingSummary(names) { return names.length ? 'Will ping: ' + names.join(', ') : 'No pings'; }

// Why Send is disabled right now ('' = it can send).
function messageBlocker(live, hasChannel, content, busy) {
  if (!live) return 'Offline';
  if (!hasChannel) return 'No channel configured';
  if (!content) return 'Write a message first';
  if (cpLen(content) > MSG_MAX) return 'Message is too long';
  if (busy) return 'Please wait…';
  return '';
}

if (typeof module !== 'undefined') {
  module.exports = { shouldApplySnapshot, normalizeEditor, shouldRollback, createSSEParser, matchChannel, relTime, backoffDelay, groupChannels,
    voiceTitle, voiceWarning, sayBlocker, voiceSwitchText,
    segmentText, substituteTags, activeTagIds, uniqueLabel, insertAtCursor, counterText, counterState, pingSummary, messageBlocker, cpLen, MSG_MAX, READY_PRESET };
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
    tab: ['voice', 'messages'].includes(store.get(sessionStorage, 'tab')) ? store.get(sessionStorage, 'tab') : 'channel',
    voice: { list: null, current: null, configured: null, loading: false, cuda: null },
    vsel: null,           // highlighted (not yet applied) voice stem
    vbusy: false,         // our own apply/say request in flight
    vsig: '',
  };
  const M = { tags: [], members: [], hi: -1, inflight: false, again: false, timer: null, busy: false, cool: false, note: '', gid: '', q: null };

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
    setTimeout(() => t.remove(), opts.sticky ? 30000 : opts.undo ? 8000 : 5000);
  }

  /* --- name prompt / api --- */
  function showName(msg) {
    if (S.abort) S.abort.abort();
    S.started = false;
    $('ui').hidden = true;
    $('name-form').hidden = false;
    $('name-msg').textContent = msg || '';
    $('name-input').value = S.editor;
    $('name-input').focus();
  }

  // The server rejected our Host/Origin (allowlist). Retrying cannot help: say so once, stop the stream.
  let lastDenied = 0;
  const DENIED_TEXT = {
    bad_host: 'This hostname is not allowed \u2014 add it to WEBUI_ALLOWED_HOSTS on the server',
    bad_origin: 'Request blocked: this page\u2019s origin is not allowed \u2014 open the GUI via an allowed hostname (WEBUI_ALLOWED_HOSTS)',
    bad_peer: 'This connection is not allowed (only loopback and Tailscale clients may connect)',
  };
  function hostDenied(code) {
    const now = Date.now();
    if (now - lastDenied < 10000) return;
    lastDenied = now;
    toast(DENIED_TEXT[code] || DENIED_TEXT.bad_host, { error: true, sticky: true });
  }
  const isDenied = (status, data) => status === 403 && !!data && (data.error === 'bad_host' || data.error === 'bad_origin' || data.error === 'bad_peer');

  const friendly = (e) => (e.message === 'persist_failed'
    ? 'the server could not save the change (storage error)' : e.message);

  class ApiError extends Error {
    constructor(status, body) {
      super((body && body.error) || ('http_' + status));
      this.status = status; this.body = body; this.denied = isDenied(status, body);
    }
  }

  async function api(method, path, body) {
    const headers = { 'X-Editor': encodeURIComponent(S.editor) };
    const init = { method, headers };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    const r = await fetch(path, init);
    let data = null;
    try { data = await r.json(); } catch (e) { /* no body */ }
    if (isDenied(r.status, data)) hostDenied(data.error);
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
    if (!shouldApplySnapshot(S.cfg, snap, force)) return;
    S.cfg = snap;
    renderConfig();
  }

  function applyState(d, force) {
    S.guilds = d.guilds || [];
    for (const g of S.guilds) S.status[g.id] = { voice: g.voice, queue: g.queue, playing: g.playing, watched: g.watched };
    if (!S.guilds.some((g) => g.id === S.gid)) S.gid = S.guilds.length ? S.guilds[0].id : '';
    const snap = d.config || d.snapshot;
    if (shouldApplySnapshot(S.cfg, snap, force)) S.cfg = snap;
    renderGuildSelect();
    renderList();
    renderConfig();
    renderStatus();
  }

  async function refetch() {
    try { applyState(await api('GET', '/api/state'), false); } catch (e) {
      if (!e.denied) toast('Could not load state: ' + friendly(e), { error: true });
    }
  }

  // Coalesce bursts of `channels` events: ~250 ms trailing, at most one request in flight.
  let rfTimer = null, rfBusy = false, rfAgain = false;
  function scheduleRefetch() {
    if (rfBusy) { rfAgain = true; return; }
    clearTimeout(rfTimer);
    rfTimer = setTimeout(async () => {
      rfTimer = null; rfBusy = true; rfAgain = false;
      try { await refetch(); } finally {
        rfBusy = false;
        if (rfAgain) scheduleRefetch();
      }
    }, 250);
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
    renderVoice();
    renderMessages();
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
    renderVoiceControls();
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
    renderVoiceControls();
    renderMessages();
  }

  /* --- voice tab --- */
  const V = () => S.voice;
  const configuredVoice = () => (S.cfg ? S.cfg.voice || null : V().configured);

  // Monotonic sequence counter for voice SSE events: prevents a stale GET response from overwriting
  // a newer SSE loading state. Incremented when a voice SSE event arrives; recorded when GET starts.
  let voiceEventSeq = 0;
  function nextVoiceEventSeq() { return voiceEventSeq; }
  function onVoiceEvent() { voiceEventSeq++; }

  function setTab(tab) {
    S.tab = tab;
    store.set(sessionStorage, 'tab', tab);
    for (const t of ['channel', 'voice', 'messages']) {
      $('view-' + t).hidden = t !== tab;
      $('tab-' + t).setAttribute('aria-selected', t === tab ? 'true' : 'false');
    }
    if (tab === 'voice' && !V().list) loadVoices(false);
    if (tab === 'messages') { renderMessages(); fetchMembers(); }
    renderVoice();
  }

  function mergeVoiceSummary(v, fromSse, seqAtRequest) {
    if (!v) return;
    V().current = v.current == null ? null : v.current;
    V().configured = v.configured == null ? null : v.configured;
    // Only apply loading from GET if the SSE sequence hasn't advanced since the request started.
    // SSE events always update loading (fromSse=true) or if offline (conn !== 'live').
    if (fromSse || S.conn !== 'live' || seqAtRequest === undefined || voiceEventSeq === seqAtRequest) {
      V().loading = !!v.loading;
    }
    renderVoice();
  }

  let vlBusy = false;
  async function loadVoices(manual) {
    if (vlBusy) return;
    vlBusy = true;
    const seqAtRequest = nextVoiceEventSeq();  // Record sequence at request start
    try {
      const d = await api('GET', '/api/voices');
      V().list = d.voices || [];
      mergeVoiceSummary(d, false, seqAtRequest);
      if (manual) toast('Found ' + V().list.length + ' voice' + (V().list.length === 1 ? '' : 's'));
    } catch (e) {
      if (!e.denied) toast('Could not load voices: ' + friendly(e), { error: true });
    } finally { vlBusy = false; }
  }

  function renderVoice() {
    renderVoiceList();
    renderVoiceControls();
  }

  function renderVoiceList() {
    const v = V(), names = v.list ? v.list.map((x) => x.name) : null, conf = configuredVoice();
    const sig = JSON.stringify([v.list, v.current, conf, S.vsel, v.cuda]);
    const warn = voiceWarning(conf, v.current, names);
    $('v-warn').hidden = !warn;
    $('v-warn').textContent = warn || '';
    if (sig === S.vsig) return;
    S.vsig = sig;
    const ul = $('v-list'), top = ul.scrollTop;
    ul.textContent = '';
    for (const x of v.list || []) {
      const li = el('li', 'opt' + (x.name === v.current ? ' sel' : '') + (x.name === S.vsel ? ' hi' : ''));
      li.dataset.name = x.name;
      li.title = x.name;
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', x.name === (S.vsel || v.current) ? 'true' : 'false');
      li.appendChild(el('span', 'nm', voiceTitle(x.name)));
      if (x.quality) li.appendChild(el('span', 'badge q', x.quality));
      li.appendChild(el('span', 'meta', [x.language, x.size_mb + ' MB'].filter(Boolean).join(' \u00b7 ')));
      if (x.name === v.current) {
        li.appendChild(el('span', 'badge', 'Active'));
        if (v.cuda === false) li.appendChild(el('span', 'badge', 'CPU'));
      } else if (x.name === conf) li.appendChild(el('span', 'badge saved', 'Saved'));
      ul.appendChild(li);
    }
    ul.scrollTop = top;
    $('v-empty').hidden = !v.list || v.list.length > 0;
  }

  function renderVoiceControls() {
    const v = V(), live = S.conn === 'live' && !!S.cfg;
    const off = !live || v.loading || S.vbusy, conf = configuredVoice();
    $('v-apply').disabled = off || !S.vsel || (S.vsel === v.current && S.vsel === conf);
    $('v-reset').disabled = off || !conf;
    $('v-rescan').disabled = S.conn !== 'live';
    $('v-list').setAttribute('aria-disabled', off ? 'true' : 'false');
    $('v-status').textContent = v.loading ? 'Loading voice\u2026 this takes a few seconds'
      : v.current ? 'Active: ' + voiceTitle(v.current) : 'No voice loaded';
    const why = sayBlocker(live, S.status[S.gid], v.loading);
    $('say-btn').disabled = !!why || S.vbusy;
    $('say-note').textContent = why;
  }

  function pickVoice(name) {
    S.vsel = name === V().current && name === configuredVoice() ? null : name;
    renderVoice();
  }

  async function applyVoice(name) {
    if (S.vbusy || V().loading || S.conn !== 'live' || !S.cfg) return;
    S.vbusy = true; renderVoiceControls();
    try {
      const d = await api('PUT', '/api/voice', { name, rev: S.cfg.rev });
      S.vsel = null;
      applySnapshot(d.snapshot, false);
      toast(name ? 'Voice changed to ' + voiceTitle(name) : 'Voice reset to default');
    } catch (e) {
      if (e.status === 409 && e.body && e.body.snapshot) {
        applySnapshot(e.body.snapshot, true);
        toast('Changed by ' + (e.body.snapshot.updated_by || 'someone') + ' \u2014 your change was not applied', { error: true });
      } else if (e.status === 422) {
        toast('Could not load that voice \u2014 the previous voice is still active', { error: true });
      } else if (e.message === 'bad_voice') {
        S.vsel = null;
        toast('That voice is no longer installed', { error: true });
      } else if (!e.denied) toast('Could not change voice: ' + friendly(e), { error: true });
    } finally {
      S.vbusy = false;
      await loadVoices(false);
      renderVoice();
    }
  }

  async function sayIt() {
    const text = $('say-text').value.trim();
    if (!text || S.vbusy || sayBlocker(S.conn === 'live' && !!S.cfg, S.status[S.gid], V().loading)) return;
    S.vbusy = true; renderVoiceControls();
    try {
      await api('POST', '/api/guilds/' + S.gid + '/say', { text });
      toast('Test phrase queued');
    } catch (e) {
      const msg = { not_connected: 'The bot is not in a voice channel in this server', queue_full: 'The queue is full, try again shortly',
        tts_unavailable: 'The TTS engine is not loaded', bad_request: 'Enter some speakable text', too_long: 'Text is too long (max 200)' }[e.message];
      if (!e.denied) toast(msg || 'Could not play the test phrase: ' + friendly(e), { error: true });
    } finally { S.vbusy = false; renderVoiceControls(); }
  }

  /* --- messages tab --- */
  const mText = () => $('m-text').value;
  const mContent = () => substituteTags(mText(), M.tags);
  const hasChannel = () => !!gcfg().channel_id && !!chName(gcfg().channel_id);

  function renderMessages() {
    if (!$('m-text')) return;
    const cid = gcfg().channel_id, name = cid ? chName(cid) : null;
    const dest = $('m-dest');
    dest.classList.toggle('off', !name);
    dest.textContent = name ? 'Posting to ' + name + ' as the bot'
      : cid ? 'The configured channel is missing — pick another in the Channel tab'
        : 'No channel is configured — pick one in the Channel tab to enable messages';
    const content = mContent(), n = cpLen(content);
    const cnt = $('m-count');
    cnt.textContent = counterText(n);
    cnt.className = 'muted ' + counterState(n);
    const act = activeTagIds(mText(), M.tags);
    // chips
    const chips = $('m-chips');
    chips.textContent = '';
    for (const t of M.tags) {
      const on = act.has(t.id);
      const c = el('span', 'chip' + (on ? '' : ' off'));
      c.appendChild(el('span', null, '@' + t.label));
      if (!on) c.appendChild(el('span', 'muted', 'not in message'));
      const b = el('button', null, '×');
      b.type = 'button';
      b.dataset.id = t.id;
      b.setAttribute('aria-label', 'Remove ' + t.label);
      c.appendChild(b);
      chips.appendChild(c);
    }
    // preview
    const pv = $('m-preview');
    pv.textContent = '';
    const text = mText();
    if (!text.trim()) pv.appendChild(el('span', 'muted', 'Nothing to send yet'));
    else for (const seg of segmentText(text, M.tags)) {
      if (seg.tag) pv.appendChild(el('span', 'tag', seg.text));
      else {
        const parts = seg.text.split(/(<@!?[0-9]{1,20}>)/g);
        for (const p of parts) {
          if (p.startsWith('<@') && p.endsWith('>')) pv.appendChild(el('span', 'tag', p));
          else if (p) pv.appendChild(document.createTextNode(p));
        }
      }
    }
    const tagById = new Map(M.tags.map((t) => [t.id, t.label]));
    const pingNames = [];
    const seenPing = new Set();
    const mentionRe = /<@!?([0-9]{1,20})>/g;
    let pm;
    while ((pm = mentionRe.exec(content))) {
      const pid = pm[1];
      if (!seenPing.has(pid)) {
        seenPing.add(pid);
        pingNames.push(tagById.get(pid) || ('user ' + pid));
      }
    }
    const live = S.conn === 'live' && !!S.cfg;
    const why = messageBlocker(live, hasChannel(), content, M.busy || M.cool);
    $('m-ping').textContent = pingSummary(pingNames);
    $('m-why').textContent = M.busy || M.cool ? '' : why;
    $('m-send').disabled = !!why;
    $('m-search').disabled = !hasChannel();
    $('m-note').hidden = !M.note;
    $('m-note').textContent = M.note;
    renderMemberList();
  }

  function renderMemberList() {
    const ul = $('m-list');
    ul.textContent = '';
    M.members.forEach((m, i) => {
      const li = el('li', 'opt' + (i === M.hi ? ' hi' : ''));
      li.dataset.i = String(i);
      li.setAttribute('role', 'option');
      li.setAttribute('aria-selected', i === M.hi ? 'true' : 'false');
      li.appendChild(el('span', 'nm', m.name));
      if (m.bot) li.appendChild(el('span', 'badge q', 'bot'));
      li.appendChild(el('span', 'meta', '@' + m.username));
      ul.appendChild(li);
    });
  }

  function scheduleMembers() {
    clearTimeout(M.timer);
    M.timer = setTimeout(fetchMembers, 200);
  }

  async function fetchMembers() {
    if (S.tab !== 'messages' || !S.gid || S.conn !== 'live' || !hasChannel()) return;
    if (M.inflight) { M.again = true; return; }
    const q = $('m-search').value.trim(), gid = S.gid;
    if (M.gid === gid && M.q === q) return;
    M.inflight = true; M.again = false;
    try {
      const d = await api('GET', '/api/guilds/' + gid + '/members?q=' + encodeURIComponent(q));
      if (gid === S.gid) {
        M.members = d.members || []; M.hi = M.members.length ? 0 : -1; M.gid = gid; M.q = q;
        M.note = d.truncated ? 'Showing the first 25 — type to narrow down' : (M.members.length ? '' : 'No matching members');
      }
    } catch (e) {
      M.note = e.message === 'no_channel' ? 'No channel configured' : 'Could not load members';
      M.members = [];
    } finally {
      M.inflight = false;
      renderMessages();
      if (M.again || $('m-search').value.trim() !== q) fetchMembers();
    }
  }

  function resetMessages() {   // switching server: tags belong to one server's members
    M.tags = []; M.members = []; M.hi = -1; M.gid = ''; M.q = null; M.note = '';
    $('m-search').value = '';
  }

  function pickMember(m) {
    let t = M.tags.find((x) => x.id === m.id);
    if (!t) { t = { id: m.id, label: uniqueLabel(m.name, m.username, m.id, M.tags) }; M.tags.push(t); }
    const ta = $('m-text');
    const r = insertAtCursor(ta.value, ta.selectionStart, ta.selectionEnd, '@' + t.label);
    ta.value = r.value;
    $('m-search').value = ''; M.q = null;
    renderMessages();
    ta.focus();
    ta.setSelectionRange(r.pos, r.pos);
    fetchMembers();
  }

  function preset(text, clearTags) {
    $('m-text').value = text;
    if (clearTags) M.tags = [];
    $('m-text').setSelectionRange(0, 0);   // a tag lands at the start: "@Name The voice bot is ready..."
    renderMessages();
    if (text) $('m-search').focus(); else $('m-text').focus();
  }

  const SEND_ERRORS = {
    bad_mention: 'Someone you tagged cannot see that channel (or a role mention was used). Remove the tag and retry',
    no_channel: 'No usable channel is configured for this server',
    bad_request: 'The message is empty or too long',
    discord_error: 'Discord refused the message. Try again in a moment',
  };

  async function sendMessage() {
    const content = mContent(), source = mText(), gid = S.gid;
    if (M.busy || M.cool || messageBlocker(S.conn === 'live' && !!S.cfg, hasChannel(), content, false)) return;
    M.busy = true; renderMessages();
    try {
      await api('POST', '/api/guilds/' + gid + '/send', { content });
      if (mText() === source) { $('m-text').value = ''; M.tags = []; }
      toast('Sent');
      M.cool = true;
      setTimeout(() => { M.cool = false; renderMessages(); }, 2000);
    } catch (e) {
      if (e.denied) { /* host guard already toasted */ }
      else if (e.status === 429) toast('Slow down: try again in ' + ((e.body && e.body.retry_after) || 2) + ' s', { error: true });
      else if (e.status === 403 && e.message === 'missing_permission') toast('Bot lacks Send Messages in that channel', { error: true });
      else toast(SEND_ERRORS[e.message] || 'Could not send: ' + friendly(e), { error: true });
    } finally { M.busy = false; renderMessages(); }
  }

  /* --- actions --- */
  async function selectChannel(id, opts) {
    opts = opts || {};
    if (!id || S.busy || S.conn !== 'live' || !S.cfg) return;
    const prev = gcfg().channel_id;
    if (id === prev) return;
    const prevRev = S.cfg.rev, gid = S.gid;
    S.busy = true;
    S.cfg.guilds[gid] = { ...gcfg(), channel_id: id };   // optimistic
    renderConfig();
    try {
      const d = await api('PUT', '/api/guilds/' + gid + '/channel', { channel_id: id, rev: prevRev });
      applySnapshot(d.snapshot, false);
      if (!opts.silent) {
        toast('Switched to ' + (chName(id) || 'channel') + (prev && chName(prev) ? ' (was ' + chName(prev) + ')' : ''),
          prev && chName(prev) ? { undo: () => selectChannel(prev, { silent: true }) } : null);
      }
    } catch (e) {
      if (e.status === 409 && e.body && e.body.snapshot) {
        applySnapshot(e.body.snapshot, true);
        toast('Changed by ' + (e.body.snapshot.updated_by || 'someone') + ' — your change was not applied', { error: true });
      } else if (!e.denied) {
        const cur = S.cfg && S.cfg.guilds[gid];
        if (cur && shouldRollback(cur.channel_id, id)) {
          S.cfg.guilds[gid] = { ...cur, channel_id: prev };
          renderConfig();
        }
        toast('Could not switch channel: ' + friendly(e), { error: true });
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
      } else if (!e.denied) toast('Request failed: ' + friendly(e), { error: true });
    } finally { S.busy = false; renderControls(); }
  }

  const doPause = () => act(async () => {
    const d = await api('PUT', '/api/guilds/' + S.gid + '/enabled', { enabled: !gcfg().enabled, rev: S.cfg.rev });
    applySnapshot(d.snapshot, false);
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
      applyState({ config: d.snapshot, guilds: d.guilds }, true);
      mergeVoiceSummary(d.voices, true);
      onVoiceEvent();  // hello carries a voice summary
    } else if (name === 'config') {
      const ch = d.change || {};
      applySnapshot(d.snapshot, false);
      const mine = d.snapshot.updated_by === normalizeEditor(S.editor);
      if (ch.guild_id === S.gid && !mine) {
        if (ch.field === 'channel_id') {
          flash(ch.new);
          const prev = ch.old && chName(ch.old);
          toast(d.snapshot.updated_by + ' switched to ' + (chName(ch.new) || 'no channel') + (prev ? ' (was ' + prev + ')' : ''));
        } else if (ch.field === 'enabled') {
          toast(d.snapshot.updated_by + (ch.new ? ' resumed' : ' paused') + ' the bot');
        }
      }
      if (ch.field === 'voice' && !mine) toast(voiceSwitchText(d.snapshot.updated_by, ch.new));
    } else if (name === 'voice') {
      onVoiceEvent();  // increment sequence counter for this SSE voice event
      V().loading = !!d.loading;
      if (!d.loading) {
        if ('current' in d) V().current = d.current;
        if ('cuda' in d) V().cuda = d.cuda;
        if (V().list) loadVoices(false);
      }
      renderVoice();
    } else if (name === 'channels') {
      scheduleRefetch();   // guild_id may be null (bot just became ready)
    } else if (name === 'status') {
      S.status = { ...S.status, ...d };
      renderStatus();
    }
  }

  async function streamOnce(signal) {
    const r = await fetch('/api/events', { signal, cache: 'no-store' });
    if (r.status === 403) {   // Host/Origin allowlist: retrying cannot help
      let d = null;
      try { d = await r.json(); } catch (e) { /* no body */ }
      if (isDenied(403, d)) { hostDenied(d.error); setConn('down'); return 'denied'; }
    }
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
      if (res === 'denied' || signal.aborted) return;
      setConn(attempt >= 3 ? 'down' : 'reconnecting');
      const wait = backoffDelay(attempt++, Math.random());
      await new Promise((ok) => setTimeout(ok, wait));
    }
  }

  function start() {
    if (S.started) return;
    S.started = true;
    $('name-form').hidden = true;
    $('ui').hidden = false;
    $('who').textContent = 'Editing as ' + S.editor;
    setConn('connecting');
    attempt = 0;
    setTab(S.tab);
    refetch().then(() => { if (S.tab === 'channel') $('search').focus(); else if (S.tab === 'messages') fetchMembers(); });
    connectLoop();
  }

  /* --- wiring --- */
  $('name-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const name = $('name-input').value.trim().slice(0, 40);
    S.editor = normalizeEditor(name);
    if (!S.editor) { $('name-msg').textContent = 'A name is required.'; return; }
    store.set(localStorage, 'editor', S.editor);
    S.started = false;
    start();
  });
  $('who').addEventListener('click', () => showName(''));
  $('guild').addEventListener('change', (e) => {
    S.gid = e.target.value;
    store.set(sessionStorage, 'gid', S.gid);
    S.hi = null;
    resetMessages();
    renderList(); renderConfig(); renderStatus();
    if (S.tab === 'messages') fetchMembers();
  });
  $('pause').addEventListener('click', doPause);
  $('skip').addEventListener('click', doSkip);
  $('clear').addEventListener('click', doClear);
  $('tab-channel').addEventListener('click', () => setTab('channel'));
  $('tab-voice').addEventListener('click', () => setTab('voice'));
  $('tab-messages').addEventListener('click', () => setTab('messages'));
  $('m-text').addEventListener('input', renderMessages);
  $('m-text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) { e.preventDefault(); sendMessage(); }
  });
  $('m-send').addEventListener('click', sendMessage);
  $('m-preset-ready').addEventListener('click', () => preset(READY_PRESET, false));
  $('m-preset-clear').addEventListener('click', () => preset('', true));
  $('m-chips').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-id]');
    if (b) { M.tags = M.tags.filter((t) => t.id !== b.dataset.id); renderMessages(); }
  });
  $('m-list').addEventListener('click', (e) => {
    const li = e.target.closest('.opt[data-i]');
    if (li && M.members[+li.dataset.i]) pickMember(M.members[+li.dataset.i]);
  });
  $('m-search').addEventListener('focus', fetchMembers);
  $('m-search').addEventListener('input', scheduleMembers);
  $('m-search').addEventListener('keydown', (e) => {
    if (e.isComposing) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      if (M.members.length) { M.hi = Math.min(M.members.length - 1, Math.max(0, M.hi + (e.key === 'ArrowDown' ? 1 : -1))); renderMemberList(); }
    } else if (e.key === 'Enter') {
      e.preventDefault();
      if (M.members[M.hi]) pickMember(M.members[M.hi]);
    } else if (e.key === 'Escape') {
      e.preventDefault();
      if ($('m-search').value) { $('m-search').value = ''; scheduleMembers(); }
    }
  });
  $('v-apply').addEventListener('click', () => { if (S.vsel) applyVoice(S.vsel); });
  $('v-reset').addEventListener('click', () => applyVoice(null));
  $('v-rescan').addEventListener('click', () => loadVoices(true));
  $('say-btn').addEventListener('click', sayIt);
  $('say-text').addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); sayIt(); } });
  $('v-list').addEventListener('click', (e) => {
    const li = e.target.closest('.opt[data-name]');
    if (li) pickVoice(li.dataset.name);
  });
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
    if ($('ui').hidden || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target, typing = t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA');
    if (S.tab === 'messages') return;   // typing areas own their keys; no global shortcuts here
    if (S.tab === 'voice') {
      const list = V().list || [];
      if (t === $('v-list') && (e.key === 'ArrowDown' || e.key === 'ArrowUp') && list.length) {
        e.preventDefault();
        const cur = list.findIndex((x) => x.name === (S.vsel || V().current));
        const i = Math.min(list.length - 1, Math.max(0, (cur < 0 ? 0 : cur) + (e.key === 'ArrowDown' ? 1 : -1)));
        pickVoice(list[i].name);
      }
      return;
    }
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
  if (S.editor) start(); else showName('');
})();
