import React, { useState } from 'react';
import { normalizeEditor } from '../utils';

export default function NameForm({ initialName, onNameSubmit }) {
  const [name, setName] = useState(initialName || '');
  const [msg, setMsg] = useState('');

  function handleSubmit(e) {
    e.preventDefault();
    const clean = normalizeEditor(name.trim().slice(0, 40));
    if (!clean) {
      setMsg('A name is required.');
      return;
    }
    onNameSubmit(clean);
  }

  return (
    <form id="name-form" className="card" onSubmit={handleSubmit}>
      <h1>Who are you?</h1>
      {msg && <p id="name-msg" className="muted" role="alert">{msg}</p>}
      <label>
        Your name
        <input
          id="name-input"
          type="text"
          maxLength={40}
          autoComplete="nickname"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <button type="submit" className="primary">Continue</button>
    </form>
  );
}
