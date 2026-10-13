import React, { useState } from 'react';

export default function LoginForm({ onLoginSuccess }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    const u = username.trim();
    const p = password;
    if (!u || !p) {
      setMsg('Username and password required.');
      return;
    }
    setBusy(true);
    setMsg('');
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: u, password: p }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setMsg((data && data.error === 'rate_limited')
          ? 'Too many failed attempts. Please wait.'
          : 'Invalid username or password.');
        return;
      }
      onLoginSuccess(u, data && data.is_admin != null ? Boolean(data.is_admin) : false);
    } catch (err) {
      setMsg('Sign in request failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <form id="login-form" className="card" onSubmit={handleSubmit}>
      <h1>Sign in</h1>
      {msg && <p id="login-msg" className="warn" role="alert">{msg}</p>}
      <label>
        Username
        <input
          id="login-user"
          type="text"
          autoComplete="username"
          required
          autoFocus
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
      </label>
      <label>
        Password
        <input
          id="login-pass"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button type="submit" className="primary" disabled={busy}>Sign in</button>
    </form>
  );
}
