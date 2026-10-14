export function createSSEParser(onEvent) {
  let buf = '', ev = '', data = [];
  return function feed(chunk) {
    buf += chunk;
    let m;
    while ((m = /\r\n|\n|\r/.exec(buf))) {
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

export function matchChannel(name, category, query) {
  const q = (query || '').trim().toLowerCase();
  if (!q) return { start: 0, end: 0 };
  const i = String(name).toLowerCase().indexOf(q);
  if (i >= 0) return { start: i, end: i + q.length };
  if (category && String(category).toLowerCase().indexOf(q) >= 0) return { start: 0, end: 0 };
  return null;
}

export function relTime(ts, now) {
  if (!ts) return 'never';
  const s = Math.max(0, Math.round(now - ts));
  if (s < 10) return 'just now';
  if (s < 60) return s + 's ago';
  if (s < 3600) return Math.floor(s / 60) + 'm ago';
  if (s < 86400) return Math.floor(s / 3600) + 'h ago';
  return Math.floor(s / 86400) + 'd ago';
}

export function backoffDelay(attempt, rand) {
  const base = Math.min(10000, 1000 * Math.pow(2, attempt));
  return Math.round(base * (0.8 + 0.4 * rand));
}

export function groupChannels(channels) {
  const groups = [], idx = new Map();
  for (const c of channels || []) {
    const k = c.category || '';
    if (!idx.has(k)) { idx.set(k, groups.length); groups.push({ name: c.category || null, channels: [] }); }
    groups[idx.get(k)].channels.push(c);
  }
  return groups;
}

export function normalizeEditor(v) {
  let s = String(v == null ? '' : v).split(/\s+/).join(' ');
  s = s.replace(/[\p{C}]/gu, '');
  s = s.split(/\s+/).join(' ').trim();
  return Array.from(s).slice(0, 40).join('').trim();
}

export function shouldApplySnapshot(cur, snap, force) {
  if (!snap) return false;
  if (force || !cur) return true;
  return snap.rev >= cur.rev;
}

export function shouldRollback(currentId, optimisticId) {
  return currentId === optimisticId;
}

export function voiceTitle(stem) {
  const p = String(stem).split('-');
  if (p.length > 1 && /^[a-z]{2,3}(_[A-Za-z0-9]+)?$/.test(p[0])) p.shift();
  if (p.length > 1 && /^(x_low|low|medium|high)$/.test(p[p.length - 1])) p.pop();
  const t = p.join(' ').split(/[_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(' ');
  return t || String(stem);
}

export function voiceWarning(configured, current, names) {
  if (!configured || configured === current) return null;
  const now = current ? voiceTitle(current) : 'no voice';
  if (!names) return 'Saved voice is not active \u2014 using ' + now;
  if (names.indexOf(configured) < 0) return 'Saved voice missing \u2014 using default';
  return 'Saved voice could not be loaded \u2014 using ' + now;
}

export function sayBlocker(live, st, loading) {
  if (!live) return 'Offline';
  if (!st || !st.voice) return 'Bot is not in a voice channel in this server (use !join)';
  if (loading) return 'Voice is loading';
  return '';
}

export function voiceSwitchText(editor, name) {
  return editor + ' switched voice to ' + (name ? voiceTitle(name) : 'the default voice');
}

export const MSG_MAX = 2000;
export const READY_PRESET = "The voice bot is ready to use! Join a voice channel, type !join here, and I'll read watched users' messages aloud.";
export const cpLen = (s) => Array.from(String(s)).length;
export const MENTION_SRC = '<@(?:!|&)?([0-9]{1,20})>';
export const mentionRe = () => new RegExp(MENTION_SRC, 'g');

export function tokenizeMentions(text) {
  text = String(text);
  const out = [];
  const re = mentionRe();
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push({ text: text.slice(last, m.index), id: null });
    out.push({ text: m[0], id: m[1] });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ text: text.slice(last), id: null });
  return out;
}

export function channelChanged(prevCfg, nextCfg, gid) {
  if (!prevCfg || !nextCfg) return false;
  const chanOf = (cfg) => {
    const g = cfg.guilds && cfg.guilds[gid];
    return g && g.channel_id != null ? g.channel_id : null;
  };
  return chanOf(prevCfg) !== chanOf(nextCfg);
}

export function segmentText(text, tags) {
  const list = (tags || []).filter((t) => t && t.label).sort((a, b) => b.label.length - a.label.length);
  text = String(text);
  if (!list.length) return [{ text, tag: null }];
  const by = new Map();
  for (const t of list) if (!by.has('@' + t.label)) by.set('@' + t.label, t);
  const alt = Array.from(by.keys()).map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|');
  const re = new RegExp('(?:' + alt + ')(?![\\p{L}\\p{N}_])', 'gu');
  const isWord = (c) => /[\p{L}\p{N}_]/u.test(c);
  const out = [];
  let last = 0, m;
  while ((m = re.exec(text))) {
    let prev = '';
    if (m.index > 0) {
      const code = text.charCodeAt(m.index - 1);
      if (code >= 0xDC00 && code <= 0xDFFF && m.index >= 2) prev = text.slice(m.index - 2, m.index);
      else prev = text.slice(m.index - 1, m.index);
    }
    if (prev && isWord(prev)) continue;
    if (m.index > last) out.push({ text: text.slice(last, m.index), tag: null });
    out.push({ text: m[0], tag: by.get(m[0]) });
    last = m.index + m[0].length;
  }
  if (last < text.length || !out.length) out.push({ text: text.slice(last), tag: null });
  return out;
}

export function substituteTags(text, tags) {
  return segmentText(text, tags)
    .map((s) => (s.tag ? (s.tag.is_role ? '<@&' : '<@') + s.tag.id + '>' : s.text))
    .join('')
    .trim();
}

export function activeTagIds(text, tags) {
  const ids = new Set();
  for (const s of segmentText(text, tags)) if (s.tag) ids.add(s.tag.id);
  return ids;
}

export function uniqueLabel(name, username, id, tags) {
  const taken = (l) => (tags || []).some((t) => t.label === l && t.id !== id);
  for (const l of [name, name + ' (' + username + ')', name + ' (' + id + ')']) if (!taken(l)) return l;
  return name + ' (' + id + ')';
}

export function insertAtCursor(value, start, end, ins) {
  start = Math.max(0, Math.min(start, value.length));
  end = Math.max(start, Math.min(end, value.length));
  const pre = value.slice(0, start), post = value.slice(end);
  const lead = pre && !/\s$/.test(pre) ? ' ' : '';
  const trail = post && /^\S/.test(post) ? ' ' : (post ? '' : ' ');
  const text = pre + lead + ins + trail;
  return { value: text + post, pos: text.length };
}

export function counterText(n) { return n + '/' + MSG_MAX; }
export function counterState(n) { return n > MSG_MAX ? 'over' : n >= 1900 ? 'near' : ''; }

export function shouldApplyMembers(reqGid, curGid, reqSeq, curSeq) {
  return reqGid === curGid && reqSeq === curSeq;
}

export function resetMemberCache(M) {
  M.members = [];
  M.hi = -1;
  M.gid = '';
  M.q = null;
  M.memberSeq++;
}

export function pingTargets(finalText, chips) {
  const byId = new Map((chips || []).map((chip) => [chip.id, chip]));
  const targets = [];
  const seen = new Set();
  for (const part of tokenizeMentions(finalText)) {
    if (part.id === null || seen.has(part.id)) continue;
    seen.add(part.id);
    const chip = byId.get(part.id);
    const isRole = part.text.startsWith('<@&') || Boolean(chip && chip.is_role);
    const item = {
      id: part.id,
      label: chip ? chip.label : null,
      username: chip && chip.username ? chip.username : null,
      known: !!chip,
    };
    if (isRole) item.is_role = true;
    targets.push(item);
  }
  return targets;
}

export function chipText(chip) {
  if (chip.is_role) return '@' + chip.label;
  if (!chip.username) return '@' + chip.label;
  if (chip.label.includes('(' + chip.username + ')')) return '@' + chip.label;
  return '@' + chip.label + ' (@' + chip.username + ')';
}

export function pingSummary(targets) {
  if (!targets.length) return 'No pings';
  const names = targets.map((t) => (t.known ? chipText(t) : (t.is_role ? 'unknown role ' : 'unknown user ') + t.id));
  return 'Will ping: ' + names.join(', ');
}

export function messageBlocker(live, hasChannel, content, busy) {
  if (!live) return 'Offline';
  if (!hasChannel) return 'No channel configured';
  if (!content) return 'Write a message first';
  if (cpLen(content) > MSG_MAX) return 'Message is too long';
  if (busy) return 'Please wait…';
  return '';
}

export function mergeVoiceSummary(prev, incoming, { seqAtRequest, seqNow, live }) {
  if (!incoming) return prev;
  const list = Array.isArray(incoming.voices) ? incoming.voices : prev.list;
  const extra = {};
  const engines = Array.isArray(incoming.engines) ? incoming.engines : prev.engines;
  const listEngine = incoming.list_engine || prev.listEngine;
  if (incoming.idle !== undefined) extra.idle = !!incoming.idle;
  if (engines !== undefined) extra.engines = engines;
  if (listEngine !== undefined) extra.listEngine = listEngine;
  if (live && seqNow > seqAtRequest) {
    return { list, current: prev.current, configured: prev.configured, loading: prev.loading, cuda: prev.cuda, ...(prev.engine !== undefined ? { engine: prev.engine } : {}), ...extra };
  }
  return {
    ...extra,
    ...((incoming.engine || prev.engine) !== undefined ? { engine: incoming.engine || prev.engine } : {}),
    list,
    current: incoming.current == null ? null : incoming.current,
    configured: incoming.configured == null ? null : incoming.configured,
    loading: !!incoming.loading,
    cuda: incoming.cuda == null ? null : incoming.cuda,
  };
}
