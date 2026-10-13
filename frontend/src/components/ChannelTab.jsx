import React from 'react';
import { groupChannels, matchChannel } from '../utils';

export default function ChannelTab({
  guilds,
  currentGid,
  config,
  status,
  query,
  setQuery,
  hi,
  setHi,
  onSelectChannel,
  onPause,
  onSkip,
  onClear,
  busy,
  searchInputRef,
}) {
  const gcfg = config && config.guilds && config.guilds[currentGid] ? config.guilds[currentGid] : { channel_id: null, enabled: true };
  const currentGuild = (guilds || []).find((g) => g.id === currentGid);
  const channels = currentGuild ? currentGuild.channels : [];
  const groups = groupChannels(channels);

  // Status line calculation
  const st = status && status[currentGid];
  let statusText = 'Paused';
  if (gcfg.enabled) {
    if (!st || !st.voice) statusText = 'Not in voice';
    else if (st.playing) statusText = 'Reading aloud';
    else statusText = 'Idle';
  }
  if (st && st.queue > 0) {
    statusText += ' • ' + st.queue + ' queued';
  }

  return (
    <div id="view-channel" role="tabpanel" aria-labelledby="tab-channel">
      <section className="controls" aria-label="Playback">
        <button id="pause" type="button" onClick={onPause} disabled={busy}>
          {gcfg.enabled ? 'Pause' : 'Resume'}
        </button>
        <button id="skip" type="button" onClick={onSkip} disabled={busy}>
          Skip
        </button>
        <button id="clear" type="button" onClick={onClear} disabled={busy}>
          Clear queue
        </button>
        <span id="status" className="muted" role="status">
          {statusText}
        </span>
      </section>

      <input
        ref={searchInputRef}
        id="search"
        type="search"
        placeholder="Search channels ( / )"
        aria-label="Search channels"
        role="combobox"
        aria-expanded="true"
        aria-controls="list"
        autoFocus
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      <ul id="list" role="listbox" tabIndex={0} aria-label="Text channels">
        {gcfg.channel_id && !channels.some((c) => c.id === gcfg.channel_id) && (
          <li id="missing-row" className="opt missing" role="note">
            Configured channel missing
          </li>
        )}
        {groups.map((grp, gIdx) => {
          const visibleChannels = grp.channels.filter((c) => matchChannel(c.name, grp.name, query));
          if (!visibleChannels.length) return null;
          return (
            <React.Fragment key={gIdx}>
              {grp.name && (
                <li className="cat" role="presentation">
                  {grp.name}
                </li>
              )}
              {visibleChannels.map((c) => {
                const isSelected = gcfg.channel_id === c.id;
                const isHi = hi === c.id;
                let cls = 'opt';
                if (isSelected) cls += ' sel';
                if (isHi) cls += ' hi';
                if (!gcfg.enabled && isSelected) cls += ' paused';

                return (
                  <li
                    key={c.id}
                    id={'ch-' + c.id}
                    data-id={c.id}
                    className={cls}
                    role="option"
                    aria-selected={isSelected ? 'true' : 'false'}
                    onClick={() => onSelectChannel(c.id)}
                  >
                    <span className="hash">#</span>
                    <span className="nm">{c.name}</span>
                    {isSelected && (
                      <span className="badge">
                        {gcfg.enabled ? 'Listening' : 'Paused'}
                      </span>
                    )}
                  </li>
                );
              })}
            </React.Fragment>
          );
        })}
      </ul>
      <p id="empty" className="muted" hidden={channels.some((c) => matchChannel(c.name, c.category, query))}>
        No channels match.
      </p>
    </div>
  );
}
