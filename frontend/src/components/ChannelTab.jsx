import React from 'react';

export default function ChannelTab({
  guilds,
  currentGid,
  config,
  status,
  onPause,
  onSkip,
  onClear,
  busy,
}) {
  const gcfg = config && config.guilds && config.guilds[currentGid] ? config.guilds[currentGid] : { channel_id: null, enabled: true };
  const currentGuild = (guilds || []).find((g) => g.id === currentGid);

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

      <div className="dashboard-grid">
        <div className="dash-card">
          <div className="dash-card-header">
            <span className="dash-card-icon">🔊</span>
            <span className="dash-card-title">Voice Connection</span>
          </div>
          <div className="dash-card-body">
            {st && st.voice ? (
              <p className="dash-card-stat ok">
                Connected to <strong>{st.voice}</strong>
              </p>
            ) : (
              <p className="dash-card-stat muted">
                Not connected to a voice channel
              </p>
            )}
            <p className="dash-card-hint">
              Use <code>/join</code> or <code>!join</code> in Discord while in a voice channel to connect the bot.
            </p>
          </div>
        </div>

        <div className="dash-card">
          <div className="dash-card-header">
            <span className="dash-card-icon">👀</span>
            <span className="dash-card-title">Watched Users</span>
          </div>
          <div className="dash-card-body">
            <p className="dash-card-stat">
              <strong>{st?.watched || 0}</strong> user{(st?.watched || 0) === 1 ? '' : 's'} being read aloud
            </p>
            <p className="dash-card-hint">
              The bot monitors all channels for watched users. Use <code>/watch @user</code> in Discord to start watching, or <code>/stop @user</code> to stop.
            </p>
          </div>
        </div>

        <div className="dash-card">
          <div className="dash-card-header">
            <span className="dash-card-icon">💬</span>
            <span className="dash-card-title">Post Messages</span>
          </div>
          <div className="dash-card-body">
            <p className="dash-card-stat">
              Available on the <strong>Messages</strong> tab
            </p>
            <p className="dash-card-hint">
              Switch to the Messages tab to select any text channel in this server and post announcements with @mentions.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
