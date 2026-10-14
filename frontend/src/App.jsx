import React, { useState, useEffect, useRef } from 'react';
import LoginForm from './components/LoginForm';
import NameForm from './components/NameForm';
import ChannelTab from './components/ChannelTab';
import VoiceTab from './components/VoiceTab';
import MessagesTab from './components/MessagesTab';
import FeaturesTab from './components/FeaturesTab';
import AdminTab from './components/AdminTab';
import {
  createSSEParser,
  normalizeEditor,
  shouldApplySnapshot,
  shouldRollback,
  mergeVoiceSummary,
  backoffDelay,
  voiceSwitchText,
  matchChannel,
} from './utils';

export default function App() {
  // Navigation & session state
  const [tab, setTab] = useState(() => sessionStorage.getItem('tab') || 'channel');
  const [editor, setEditor] = useState(() => localStorage.getItem('editor') || '');
  const [authRequired, setAuthRequired] = useState(false);
  const [authenticated, setAuthenticated] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const [started, setStarted] = useState(false);
  const [toasts, setToasts] = useState([]);

  // Live state
  const [conn, setConn] = useState('connecting');
  const [guilds, setGuilds] = useState([]);
  const [gid, setGid] = useState(() => sessionStorage.getItem('gid') || '');
  const [config, setConfig] = useState(null);
  const [status, setStatus] = useState({});
  const [voice, setVoice] = useState({ list: null, current: null, configured: null, loading: false, cuda: null });
  const [vsel, setVsel] = useState(null);
  const [vbusy, setVbusy] = useState(false);
  const [busy, setBusy] = useState(false);
  const [query, setQuery] = useState('');
  const [hi, setHi] = useState(null);

  // Features & Admin users state
  const [features, setFeatures] = useState([]);
  const [users, setUsers] = useState([]);

  const searchInputRef = useRef(null);
  const voiceSeqRef = useRef(0);

  function toast(msg, opts = {}) {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev.slice(-3), { id, msg, ...opts }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, opts.sticky ? 30000 : opts.undo ? 8000 : 5000);
  }

  // --- API wrapper ---
  async function api(method, path, body) {
    const headers = { 'X-Editor': encodeURIComponent(editor) };
    if (body) headers['Content-Type'] = 'application/json';
    const res = await fetch(path, {
      method,
      headers,
      body: body ? JSON.stringify(body) : undefined,
    });
    if (res.status === 401) {
      setAuthenticated(false);
      setStarted(false);
      throw new Error('unauthorized');
    }
    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const err = new Error(data && data.error ? data.error : 'http_' + res.status);
      err.status = res.status;
      err.body = data;
      throw err;
    }
    return data;
  }

  // Boot check
  useEffect(() => {
    async function boot() {
      try {
        const res = await fetch('/api/session', { cache: 'no-store' });
        const data = await res.json().catch(() => null);
        if (res.ok && data) {
          if (data.auth_required && !data.authenticated) {
            setAuthRequired(true);
            setAuthenticated(false);
            return;
          }
          if (data.is_admin != null) setIsAdmin(Boolean(data.is_admin));
          if (data.username && !editor) {
            const clean = normalizeEditor(data.username);
            setEditor(clean);
            localStorage.setItem('editor', clean);
          }
          setStarted(true);
        }
      } catch (e) {
        setStarted(true);
      }
    }
    boot();
  }, []);

  // Update tab in session storage
  function changeTab(nextTab) {
    setTab(nextTab);
    sessionStorage.setItem('tab', nextTab);
    if (nextTab === 'voice' && !voice.list) loadVoices(false);
    if (nextTab === 'features') loadFeatures();
    if (nextTab === 'admin') loadUsers();
  }

  // Load voices
  async function loadVoices(manual) {
    voiceSeqRef.current++;
    const seq = voiceSeqRef.current;
    try {
      const d = await api('GET', '/api/voices');
      setVoice((prev) => mergeVoiceSummary(prev, d, { seqAtRequest: seq, seqNow: voiceSeqRef.current, live: conn === 'live' }));
      if (manual && d.voices) {
        toast('Found ' + d.voices.length + ' voice' + (d.voices.length === 1 ? '' : 's'));
      }
    } catch (e) {}
  }

  // Load features
  async function loadFeatures() {
    try {
      const res = await fetch('/api/features');
      const data = await res.json().catch(() => null);
      if (res.ok && data && Array.isArray(data.features)) {
        setFeatures(data.features);
      }
    } catch (e) {}
  }

  // Load users
  async function loadUsers() {
    try {
      const res = await fetch('/api/admin/users');
      const data = await res.json().catch(() => null);
      if (res.ok && data && Array.isArray(data.users)) {
        setUsers(data.users);
      }
    } catch (e) {}
  }

  // Live SSE connection loop
  useEffect(() => {
    if (!started || !authenticated) return;
    let aborted = false;
    let attempt = 0;
    const ac = new AbortController();

    async function connectLoop() {
      while (!aborted) {
        try {
          const r = await fetch('/api/events', { signal: ac.signal, cache: 'no-store' });
          if (r.status === 401) {
            setAuthenticated(false);
            setStarted(false);
            return;
          }
          if (!r.ok || !r.body) throw new Error('http_' + r.status);

          const dec = new TextDecoder();
          const feed = createSSEParser((evName, rawData) => {
            if (evName === 'hello') {
              attempt = 0;
              setConn('live');
              try {
                const d = JSON.parse(rawData);
                if (d.snapshot) setConfig(d.snapshot);
                if (d.guilds) {
                  setGuilds(d.guilds);
                  if (!d.guilds.some((g) => g.id === gid) && d.guilds.length) {
                    setGid(d.guilds[0].id);
                    sessionStorage.setItem('gid', d.guilds[0].id);
                  }
                }
                if (d.voices) setVoice((prev) => mergeVoiceSummary(prev, d.voices, { seqAtRequest: 0, seqNow: 0, live: true }));
              } catch (e) {}
            } else if (evName === 'config') {
              try {
                const d = JSON.parse(rawData);
                if (d.snapshot) setConfig(d.snapshot);
                const ch = d.change || {};
                const mine = d.snapshot && d.snapshot.updated_by === normalizeEditor(editor);
                if (ch.guild_id === gid && !mine) {
                  if (ch.field === 'enabled') {
                    toast(d.snapshot.updated_by + (ch.new ? ' resumed' : ' paused') + ' the bot');
                  }
                }
                if (ch.field === 'voice' && !mine) {
                  toast(voiceSwitchText(d.snapshot.updated_by, ch.new));
                }
              } catch (e) {}
            } else if (evName === 'voice') {
              try {
                const d = JSON.parse(rawData);
                setVoice((prev) => ({
                  ...prev,
                  loading: !!d.loading,
                  current: 'current' in d ? d.current : prev.current,
                  cuda: 'cuda' in d ? d.cuda : prev.cuda,
                }));
              } catch (e) {}
            } else if (evName === 'status') {
              try {
                const d = JSON.parse(rawData);
                setStatus((prev) => ({ ...prev, ...d }));
              } catch (e) {}
            }
          });

          const reader = r.body.getReader();
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            feed(dec.decode(value, { stream: true }));
          }
        } catch (e) {
          if (aborted) return;
        }
        setConn('reconnecting');
        const wait = backoffDelay(attempt++, Math.random());
        await new Promise((ok) => setTimeout(ok, wait));
      }
    }

    connectLoop();
    return () => {
      aborted = true;
      ac.abort();
    };
  }, [started, authenticated, gid, editor]);

  // Initial fetch for state
  useEffect(() => {
    if (!started || !authenticated) return;
    async function fetchState() {
      try {
        const d = await api('GET', '/api/state');
        if (d.guilds) {
          setGuilds(d.guilds);
          if (!d.guilds.some((g) => g.id === gid) && d.guilds.length) {
            setGid(d.guilds[0].id);
            sessionStorage.setItem('gid', d.guilds[0].id);
          }
        }
        if (d.config) setConfig(d.config);
      } catch (e) {}
    }
    fetchState();
  }, [started, authenticated]);

  // Keyboard shortcut listener
  useEffect(() => {
    function handleKeyDown(e) {
      if (!started || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
      if (tab === 'messages' || tab === 'features' || tab === 'admin') return;

      const t = e.target;
      const typing = t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA');

      if (e.key === '/' && !typing) {
        e.preventDefault();
        searchInputRef.current?.focus();
        return;
      }
    }
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [started, tab]);

  // Actions
  async function selectChannel(id, opts = {}) {
    if (!id || busy || !config) return;
    const curGcfg = config.guilds && config.guilds[gid] ? config.guilds[gid] : {};
    const prev = curGcfg.channel_id;
    if (id === prev) return;
    const prevRev = config.rev;

    setBusy(true);
    setConfig((prevCfg) => ({
      ...prevCfg,
      guilds: { ...prevCfg.guilds, [gid]: { ...curGcfg, channel_id: id } },
    }));

    try {
      const d = await api('PUT', '/api/guilds/' + gid + '/channel', { channel_id: id, rev: prevRev });
      if (d.snapshot) setConfig(d.snapshot);
      if (!opts.silent) {
        toast('Switched channel', prev ? { undo: () => selectChannel(prev, { silent: true }) } : null);
      }
    } catch (e) {
      if (e.status === 409 && e.body && e.body.snapshot) {
        setConfig(e.body.snapshot);
        toast('Changed by ' + (e.body.snapshot.updated_by || 'someone') + ' — your change was not applied', { error: true });
      } else {
        toast('Could not switch channel', { error: true });
      }
    } finally {
      setBusy(false);
    }
  }

  async function togglePause() {
    if (busy || !config) return;
    const curGcfg = config.guilds && config.guilds[gid] ? config.guilds[gid] : { enabled: true };
    setBusy(true);
    try {
      const d = await api('PUT', '/api/guilds/' + gid + '/enabled', { enabled: !curGcfg.enabled, rev: config.rev });
      if (d.snapshot) setConfig(d.snapshot);
    } catch (e) {
      toast('Failed to change pause state', { error: true });
    } finally {
      setBusy(false);
    }
  }

  async function handleSkip() {
    setBusy(true);
    try {
      const d = await api('POST', '/api/guilds/' + gid + '/skip');
      toast(d.skipped ? 'Skipped current message' : 'Nothing is playing');
    } catch (e) {
      toast('Skip failed', { error: true });
    } finally {
      setBusy(false);
    }
  }

  async function handleClear() {
    setBusy(true);
    try {
      const d = await api('POST', '/api/guilds/' + gid + '/clear');
      toast('Cleared ' + d.cleared + ' queued message' + (d.cleared === 1 ? '' : 's'));
    } catch (e) {
      toast('Clear failed', { error: true });
    } finally {
      setBusy(false);
    }
  }

  async function handleApplyVoice(voiceName) {
    if (vbusy) return;
    setVbusy(true);
    try {
      const d = await api('PUT', '/api/voice', { voice: voiceName, rev: config?.rev });
      if (d.snapshot) setConfig(d.snapshot);
      toast('Applied voice');
    } catch (e) {
      toast('Failed to apply voice', { error: true });
    } finally {
      setVbusy(false);
      await loadVoices(false);
    }
  }

  async function handleSay(text) {
    if (!text || vbusy) return;
    setVbusy(true);
    try {
      await api('POST', '/api/guilds/' + gid + '/say', { text });
      toast('Test phrase queued');
    } catch (e) {
      toast('Could not play test phrase: ' + (e.message || 'error'), { error: true });
    } finally {
      setVbusy(false);
    }
  }

  async function handleSendMessage(content, channelId) {
    try {
      const payload = { content };
      if (channelId) payload.channel_id = channelId;
      await api('POST', '/api/guilds/' + gid + '/send', payload);
      toast('Message sent as the bot');
      return true;
    } catch (e) {
      toast('Could not send message: ' + (e.message || 'error'), { error: true });
      return false;
    }
  }

  async function handleRequestFeature(name, description) {
    try {
      const res = await fetch('/api/features', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description }),
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.ok) {
        toast('Feature request added.');
        loadFeatures();
        return true;
      }
    } catch (e) {}
    return false;
  }

  async function handleDeleteFeature(id) {
    try {
      const res = await fetch('/api/features/' + encodeURIComponent(id), { method: 'DELETE' });
      if (res.ok) {
        setFeatures((prev) => prev.filter((x) => x.id !== id));
        toast('Feature deleted.');
      } else {
        toast('Failed to delete feature.', { error: true });
      }
    } catch (e) {
      toast('Failed to delete feature.', { error: true });
    }
  }

  async function handleSaveUser(uname, pass, groups) {
    try {
      const body = { username: uname, groups };
      if (pass) body.password = pass;
      const res = await fetch('/api/admin/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const data = await res.json().catch(() => null);
      if (res.ok && data && data.ok) {
        toast('User saved.');
        loadUsers();
        return true;
      }
    } catch (e) {}
    return false;
  }

  async function handleDeleteUser(uname) {
    try {
      const res = await fetch('/api/admin/users/' + encodeURIComponent(uname), { method: 'DELETE' });
      if (res.ok) {
        setUsers((prev) => prev.filter((x) => x.username !== uname));
        toast('User deleted.');
      } else {
        toast('Failed to delete user.', { error: true });
      }
    } catch (e) {
      toast('Failed to delete user.', { error: true });
    }
  }

  async function handleLogout() {
    try {
      await fetch('/api/logout', { method: 'POST' });
    } catch (e) {}
    setAuthenticated(false);
    setStarted(false);
  }

  // Current guild name
  const curGuildObj = (guilds || []).find((g) => g.id === gid);
  const curGcfg = config && config.guilds && config.guilds[gid] ? config.guilds[gid] : {};
  const currentChannelObj = curGuildObj && curGuildObj.channels ? curGuildObj.channels.find((c) => c.id === curGcfg.channel_id) : null;
  const channelName = currentChannelObj ? '#' + currentChannelObj.name : 'no channel';
  const hasChannel = Boolean(currentChannelObj);

  return (
    <main id="app">
      {!authenticated ? (
        <LoginForm
          onLoginSuccess={(u, admin) => {
            setAuthenticated(true);
            setIsAdmin(admin);
            if (!editor) {
              const clean = normalizeEditor(u);
              setEditor(clean);
              localStorage.setItem('editor', clean);
            }
            setStarted(true);
          }}
        />
      ) : !editor ? (
        <NameForm
          initialName={editor}
          onNameSubmit={(name) => {
            setEditor(name);
            localStorage.setItem('editor', name);
            setStarted(true);
          }}
        />
      ) : (
        <div id="ui">
          <header>
            <div className="row top">
              <h1 id="title">Listen channel</h1>
              <span id="conn" className="conn" data-state={conn}>
                <span className="dot" aria-hidden="true"></span>
                <span id="conn-text">{conn === 'live' ? 'Connected' : 'Connecting…'}</span>
              </span>
            </div>
            <div className="row">
              {guilds.length > 1 && (
                <select
                  id="guild"
                  aria-label="Server"
                  value={gid}
                  onChange={(e) => {
                    setGid(e.target.value);
                    sessionStorage.setItem('gid', e.target.value);
                  }}
                >
                  {guilds.map((g) => (
                    <option key={g.id} value={g.id}>{g.name}</option>
                  ))}
                </select>
              )}
              <span id="meta" className="muted"></span>
              <button id="who" type="button" className="link" title="Change your name" onClick={() => setEditor('')}>
                Editing as {editor}
              </button>
              <button id="logout-btn" type="button" className="link" title="Sign out" onClick={handleLogout}>
                Sign out
              </button>
            </div>
          </header>

          <nav className="tabs" role="tablist" aria-label="Sections">
            <button
              id="tab-channel"
              type="button"
              role="tab"
              aria-selected={tab === 'channel' ? 'true' : 'false'}
              aria-controls="view-channel"
              onClick={() => changeTab('channel')}
            >
              Controls
            </button>
            <button
              id="tab-voice"
              type="button"
              role="tab"
              aria-selected={tab === 'voice' ? 'true' : 'false'}
              aria-controls="view-voice"
              onClick={() => changeTab('voice')}
            >
              Voice
            </button>
            <button
              id="tab-messages"
              type="button"
              role="tab"
              aria-selected={tab === 'messages' ? 'true' : 'false'}
              aria-controls="view-messages"
              onClick={() => changeTab('messages')}
            >
              Messages
            </button>
            <button
              id="tab-features"
              type="button"
              role="tab"
              aria-selected={tab === 'features' ? 'true' : 'false'}
              aria-controls="view-features"
              onClick={() => changeTab('features')}
            >
              Features
            </button>
            {isAdmin && (
              <button
                id="tab-admin"
                type="button"
                role="tab"
                aria-selected={tab === 'admin' ? 'true' : 'false'}
                aria-controls="view-admin"
                onClick={() => changeTab('admin')}
              >
                Admin
              </button>
            )}
          </nav>

          {tab === 'channel' && (
            <ChannelTab
              guilds={guilds}
              currentGid={gid}
              config={config}
              status={status}
              onPause={togglePause}
              onSkip={handleSkip}
              onClear={handleClear}
              busy={busy}
            />
          )}

          {tab === 'voice' && (
            <VoiceTab
              voiceState={voice}
              configVoice={config?.voice}
              currentStatus={status[gid]}
              isLive={conn === 'live'}
              vsel={vsel}
              setVsel={setVsel}
              onApplyVoice={handleApplyVoice}
              onResetVoice={() => handleApplyVoice(null)}
              onRescanVoices={() => loadVoices(true)}
              onSay={handleSay}
              vbusy={vbusy}
            />
          )}

          {tab === 'messages' && (
            <MessagesTab
              guild={curGuildObj}
              channelName={channelName}
              hasChannel={hasChannel}
              isLive={conn === 'live'}
              onSendMessage={handleSendMessage}
              editor={editor}
              toast={toast}
            />
          )}

          {tab === 'features' && (
            <FeaturesTab
              features={features}
              isAdmin={isAdmin}
              onRequestFeature={handleRequestFeature}
              onDeleteFeature={handleDeleteFeature}
            />
          )}

          {tab === 'admin' && isAdmin && (
            <AdminTab
              users={users}
              currentEditor={editor}
              onSaveUser={handleSaveUser}
              onDeleteUser={handleDeleteUser}
            />
          )}
        </div>
      )}

      <div id="toasts" aria-live="polite" aria-atomic="false">
        {toasts.map((t) => (
          <div key={t.id} className={'toast' + (t.error ? ' err' : '')}>
            <span>{t.msg}</span>
            {t.undo && (
              <button
                type="button"
                onClick={() => {
                  setToasts((prev) => prev.filter((x) => x.id !== t.id));
                  t.undo();
                }}
              >
                Undo
              </button>
            )}
          </div>
        ))}
      </div>
    </main>
  );
}
