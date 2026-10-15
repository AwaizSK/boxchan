import React, { useState } from 'react';

// Type-to-filter server picker (native datalist). The caller remembers the pick in the browser.
export default function GuildPicker({ guilds, gid, onPick }) {
  const current = guilds.find((g) => g.id === gid);
  const [text, setText] = useState(null); // null = show the current server's name

  function commit(value) {
    const want = value.trim().toLowerCase();
    const hit = guilds.find((g) => g.name.toLowerCase() === want);
    if (hit) onPick(hit.id);
    setText(null);
  }

  return (
    <>
      <input
        id="guild"
        type="text"
        list="guild-list"
        aria-label="Server"
        placeholder="Type a server name"
        autoComplete="off"
        value={text === null ? (current ? current.name : '') : text}
        onFocus={(e) => { setText(''); e.target.select(); }}
        onChange={(e) => {
          setText(e.target.value);
          const hit = guilds.find((g) => g.name === e.target.value);
          if (hit) { onPick(hit.id); e.target.blur(); }
        }}
        onBlur={(e) => commit(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur(); }}
      />
      <datalist id="guild-list">
        {guilds.map((g) => (
          <option key={g.id} value={g.name} />
        ))}
      </datalist>
    </>
  );
}
