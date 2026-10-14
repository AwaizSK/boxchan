import React, { useState, useEffect, useRef } from 'react';
import {
  cpLen,
  counterText,
  counterState,
  substituteTags,
  activeTagIds,
  chipText,
  pingTargets,
  pingSummary,
  messageBlocker,
  uniqueLabel,
  insertAtCursor,
  READY_PRESET,
} from '../utils';

export default function MessagesTab({
  guild,
  channelName,
  hasChannel,
  isLive,
  onSendMessage,
  editor,
  toast,
}) {
  const [text, setText] = useState('');
  const [tags, setTags] = useState([]);
  const [searchQuery, setSearchQuery] = useState('');
  const [items, setItems] = useState([]);
  const [itemHi, setItemHi] = useState(-1);
  const [busy, setBusy] = useState(false);
  const [searchFocused, setSearchFocused] = useState(false);

  const searchInputRef = useRef(null);
  const textareaRef = useRef(null);

  const substituted = substituteTags(text, tags);
  const charCount = cpLen(substituted);
  const blocker = messageBlocker(isLive, hasChannel, substituted, busy);
  const actTags = activeTagIds(text, tags);
  const pings = pingTargets(substituted, tags);

  useEffect(() => {
    if (!guild) return;
    let cancelled = false;
    async function fetchResults() {
      try {
        const q = encodeURIComponent(searchQuery);
        const [mRes, rRes] = await Promise.all([
          fetch('/api/guilds/' + guild.id + '/members?q=' + q),
          fetch('/api/guilds/' + guild.id + '/roles?q=' + q),
        ]);
        const mData = mRes.ok ? await mRes.json() : null;
        const rData = rRes.ok ? await rRes.json() : null;
        if (!cancelled) {
          const roles = (rData && Array.isArray(rData.roles)) ? rData.roles : [];
          const members = (mData && Array.isArray(mData.members)) ? mData.members : [];
          setItems([...roles, ...members]);
          setItemHi(-1);
        }
      } catch (e) {}
    }
    const t = setTimeout(fetchResults, 200);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [guild, searchQuery]);

  function pickItem(item) {
    let label;
    let newTag;
    if (item.is_role) {
      label = uniqueLabel(item.name, 'role', item.id, tags);
      newTag = { id: item.id, label, is_role: true };
    } else {
      const displayName = item.display_name || item.name || item.username;
      label = uniqueLabel(displayName, item.username, item.id, tags);
      newTag = { id: item.id, label, username: item.username, is_role: false };
    }
    setTags((prev) => [...prev, newTag]);

    // Insert @label into textarea
    const ta = textareaRef.current;
    if (ta) {
      const ins = insertAtCursor(text, ta.selectionStart || text.length, ta.selectionEnd || text.length, '@' + label);
      setText(ins.value);
    } else {
      setText((prev) => prev + (prev && !/\s$/.test(prev) ? ' ' : '') + '@' + label + ' ');
    }
    setSearchQuery('');
    setSearchFocused(false);
  }

  function removeChip(id) {
    setTags((prev) => prev.filter((t) => t.id !== id));
  }

  async function handleSend() {
    if (blocker) return;
    setBusy(true);
    try {
      const ok = await onSendMessage(substituted);
      if (ok) {
        setText('');
        setTags([]);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div id="view-messages" role="tabpanel" aria-labelledby="tab-messages">
      <p id="m-dest" className={'dest' + (!hasChannel ? ' off' : '')} role="status">
        {hasChannel
          ? 'Posting to ' + channelName + ' as the bot'
          : 'No channel is configured — pick one in the Channel tab to enable messages'}
      </p>

      <section aria-label="Presets" className="controls">
        <button id="m-preset-ready" type="button" onClick={() => { setText(READY_PRESET); searchInputRef.current?.focus(); }}>
          Ready preset
        </button>
        <button id="m-preset-clear" type="button" onClick={() => { setText(''); setTags([]); }}>
          Clear
        </button>
      </section>

      <label htmlFor="m-text" className="visually">Message</label>
      <textarea
        ref={textareaRef}
        id="m-text"
        rows={5}
        placeholder="Write a message the bot will post. Ctrl+Enter sends."
        maxLength={2000}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) {
            e.preventDefault();
            handleSend();
          }
        }}
      />
      <div className="m-count">
        <span id="m-count" className={'muted ' + counterState(charCount)} aria-live="off">
          {counterText(charCount)}
        </span>
      </div>

      <section className="tagger" aria-label="Tag someone">
        <input
          ref={searchInputRef}
          id="m-search"
          type="search"
          placeholder="Search members or roles"
          aria-label="Search members or roles"
          role="combobox"
          aria-expanded="true"
          aria-controls="m-list"
          value={searchQuery}
          onFocus={() => setSearchFocused(true)}
          onChange={(e) => setSearchQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.isComposing) return;
            if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
              e.preventDefault();
              if (items.length) {
                setItemHi((prev) =>
                  Math.min(items.length - 1, Math.max(0, prev + (e.key === 'ArrowDown' ? 1 : -1)))
                );
              }
            } else if (e.key === 'Enter') {
              e.preventDefault();
              if (items[itemHi]) pickItem(items[itemHi]);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              setSearchQuery('');
            }
          }}
        />
        {searchFocused && items.length > 0 && (
          <ul id="m-list" role="listbox" aria-label="Members and roles">
            {items.map((item, i) => (
              <li
                key={(item.is_role ? 'r-' : 'm-') + item.id}
                className={'opt' + (itemHi === i ? ' hi' : '')}
                onClick={() => pickItem(item)}
              >
                {item.is_role ? (
                  <>
                    <span className="nm">@{item.name}</span>
                    <span className="meta">role</span>
                  </>
                ) : (
                  <>
                    <span className="nm">{item.display_name || item.name || item.username}</span>
                    <span className="meta">@{item.username}</span>
                  </>
                )}
              </li>
            ))}
          </ul>
        )}
        <p id="m-note" className="muted" hidden></p>
      </section>

      <div id="m-chips" className="chips" aria-label="Tagging">
        {tags.map((t) => {
          const on = actTags.has(t.id);
          return (
            <span key={(t.is_role ? 'r-' : 'm-') + t.id} className={'chip' + (t.is_role ? ' role' : '') + (on ? '' : ' off')} title={chipText(t)}>
              {t.is_role ? '@' + t.label : t.label}
              <button type="button" onClick={() => removeChip(t.id)}>×</button>
            </span>
          );
        })}
      </div>

      <div className="muted">Preview</div>
      <div id="m-preview" className="preview" aria-label="Preview">
        {substituted || <span className="muted">No preview</span>}
      </div>

      <div className="sendrow">
        <span id="m-ping" className="muted" role="status">
          {pingSummary(pings)}
        </span>
        {blocker && <span id="m-why" className="muted">{blocker}</span>}
        <button id="m-send" type="button" className="primary" onClick={handleSend} disabled={Boolean(blocker)}>
          Send
        </button>
      </div>
    </div>
  );
}
