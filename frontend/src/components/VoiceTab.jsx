import React, { useState } from 'react';
import { voiceTitle, voiceWarning, sayBlocker } from '../utils';

export default function VoiceTab({
  voiceState,
  configVoice,
  currentStatus,
  isLive,
  vsel,
  setVsel,
  onApplyVoice,
  onResetVoice,
  onRescanVoices,
  onSay,
  vbusy,
}) {
  const [testText, setTestText] = useState('Hello! This is how I sound.');
  const names = voiceState.list ? voiceState.list.map((x) => x.name) : null;
  const warn = voiceWarning(configVoice, voiceState.current, names);

  const curVoiceTitle = voiceState.current ? voiceTitle(voiceState.current) : 'default';
  let statusText = voiceState.loading ? 'Loading voice…' : 'Active: ' + curVoiceTitle;
  if (voiceState.cuda != null) {
    statusText += ' (' + (voiceState.cuda ? 'CUDA' : 'CPU') + ')';
  }

  const blocker = sayBlocker(isLive, currentStatus, voiceState.loading);

  return (
    <div id="view-voice" role="tabpanel" aria-labelledby="tab-voice">
      <section className="controls" aria-label="Voice">
        <button id="v-apply" type="button" className="primary" onClick={() => onApplyVoice(vsel)} disabled={vbusy || !vsel}>
          Apply
        </button>
        <button id="v-reset" type="button" onClick={onResetVoice} disabled={vbusy}>
          Reset to default
        </button>
        <button id="v-rescan" type="button" onClick={onRescanVoices} disabled={vbusy}>
          Rescan
        </button>
        <span id="v-status" className="muted" role="status">
          {statusText}
        </span>
      </section>

      {warn && (
        <p id="v-warn" className="warn">
          {warn}
        </p>
      )}

      <ul id="v-list" role="listbox" tabIndex={0} aria-label="Voices">
        {(voiceState.list || []).map((vx) => {
          const isSelected = (vsel || voiceState.current) === vx.name;
          const isSaved = configVoice === vx.name;
          let cls = 'opt';
          if (isSelected) cls += ' sel';

          return (
            <li
              key={vx.name}
              data-name={vx.name}
              className={cls}
              role="option"
              aria-selected={isSelected ? 'true' : 'false'}
              onClick={() => setVsel(vx.name)}
            >
              <span className="nm">{voiceTitle(vx.name)}</span>
              {isSaved && <span className="badge saved">Saved</span>}
              {isSelected && <span className="badge">Active</span>}
            </li>
          );
        })}
      </ul>
      <p id="v-empty" className="muted" hidden={(voiceState.list || []).length > 0}>
        No voices found in the models folder.
      </p>

      <section className="say" aria-label="Test voice">
        <input
          id="say-text"
          type="text"
          maxLength={200}
          value={testText}
          aria-label="Test phrase"
          onChange={(e) => setTestText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.isComposing) {
              e.preventDefault();
              onSay(testText);
            }
          }}
        />
        <button id="say-btn" type="button" onClick={() => onSay(testText)} disabled={vbusy || Boolean(blocker)}>
          Say it
        </button>
        {blocker && <span id="say-note" className="muted" role="status">{blocker}</span>}
      </section>
    </div>
  );
}
