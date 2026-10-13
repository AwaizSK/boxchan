import React, { useState } from 'react';

export default function AdminTab({
  users,
  currentEditor,
  onSaveUser,
  onDeleteUser,
}) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [groups, setGroups] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    const uname = username.trim();
    if (!uname) return;
    setBusy(true);
    setMsg('Saving…');
    try {
      const grpList = groups.split(',').map((x) => x.trim()).filter(Boolean);
      const ok = await onSaveUser(uname, password, grpList);
      if (ok) {
        setUsername('');
        setPassword('');
        setGroups('');
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
        <label htmlFor="u-groups" className="visually">Groups</label>
        <input
          id="u-groups"
          type="text"
          placeholder="Groups (e.g. admin, tester)"
          value={groups}
          onChange={(e) => setGroups(e.target.value)}
        />
        <div className="row">
          <button id="u-submit" type="submit" className="primary" disabled={busy}>
            Save user
          </button>
          {msg && <span id="u-msg" className="muted" role="status">{msg}</span>}
        </div>
      </form>

      <div id="u-list" className="f-list" role="feed" aria-label="Users">
        {(users || []).map((u) => {
          const grpStr = Array.isArray(u.groups) ? u.groups.join(', ') : '';
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
              <p className="f-desc">Groups: {grpStr || 'none'}</p>
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
