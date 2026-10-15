import React, { useState } from 'react';

export default function AdminTab({
  users,
  guilds,
  currentEditor,
  onSaveUser,
  onDeleteUser,
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState('server');
  const [picked, setPicked] = useState([]);
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    const uname = username.trim();
    if (!uname) return;
    setBusy(true);
    setMsg('Saving…');
    try {
      if (role === 'server' && picked.length === 0) {
        setMsg('Pick at least one server.');
        setBusy(false);
        return;
      }
      const ok = await onSaveUser(uname, password, [role === 'platform' ? 'admin' : 'user'], role === 'platform' ? [] : picked);
      if (ok) {
        setUsername('');
        setPassword('');
        setPicked([]);
        setMsg('');
      } else {
        setMsg('Failed to save user.');
      }
    } catch (err) {
      setMsg('Request failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div id="view-admin" role="tabpanel" aria-labelledby="tab-admin">
      <form id="u-form" className="f-form" onSubmit={handleSubmit}>
        <label htmlFor="u-name" className="visually">Username</label>
        <input
          id="u-name"
          type="text"
          placeholder="Username"
          maxLength={40}
          required
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
        <label htmlFor="u-pass" className="visually">Password</label>
        <input
          id="u-pass"
          type="password"
          placeholder="Password (blank to keep)"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
        <label htmlFor="u-role" className="visually">Account type</label>
        <select id="u-role" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="server">Server admin (selected servers only)</option>
          <option value="platform">Platform admin (all servers, voice, users)</option>
        </select>
        {role === 'server' && (
          <fieldset id="u-guilds" className="f-form">
            <legend className="muted">Servers this account can manage</legend>
            {(guilds || []).map((g) => (
              <label key={g.id} className="row">
                <input
                  type="checkbox"
                  checked={picked.includes(g.id)}
                  onChange={(e) => setPicked((prev) => (e.target.checked ? [...prev, g.id] : prev.filter((x) => x !== g.id)))}
                />
                {g.name}
              </label>
            ))}
          </fieldset>
        )}
        <div className="row">
          <button id="u-submit" type="submit" className="primary" disabled={busy}>
            Save user
          </button>
          {msg && <span id="u-msg" className="muted" role="status">{msg}</span>}
        </div>
      </form>

      <div id="u-list" className="f-list" role="feed" aria-label="Users">
        {(users || []).map((u) => {
          const isPlat = Array.isArray(u.groups) && u.groups.includes('admin');
          const names = (u.guilds || []).map((id) => {
            const g = (guilds || []).find((x) => x.id === id);
            return g ? g.name : id;
          });
          const canDelete = u.username !== 'admin' && u.username !== currentEditor;
          return (
            <article key={u.username} className="f-card">
              <div className="f-head">
                <h2 className="f-title">{u.username}</h2>
                {canDelete && (
                  <button
                    className="f-del"
                    type="button"
                    aria-label={'Delete user ' + u.username}
                    onClick={() => onDeleteUser(u.username)}
                  >
                    Delete
                  </button>
                )}
              </div>
              <p className="f-desc">{isPlat ? 'Platform admin (all servers)' : 'Server admin: ' + (names.join(', ') || 'no servers')}</p>
            </article>
          );
        })}
      </div>
      <p id="u-empty" className="muted" hidden={(users || []).length > 0}>
        No users found.
      </p>
    </div>
  );
}
