import React, { useState } from 'react';

export default function FeaturesTab({
  features,
  isAdmin,
  onRequestFeature,
  onDeleteFeature,
}) {
  const [name, setName] = useState('');
  const [desc, setDesc] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    if (!name.trim() || !desc.trim()) return;
    setBusy(true);
    setMsg('Submitting…');
    try {
      const ok = await onRequestFeature(name.trim(), desc.trim());
      if (ok) {
        setName('');
        setDesc('');
        setMsg('');
      } else {
        setMsg('Failed to submit feature.');
      }
    } catch (err) {
      setMsg('Request failed.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div id="view-features" role="tabpanel" aria-labelledby="tab-features">
      <form id="f-form" className="f-form" onSubmit={handleSubmit}>
        <label htmlFor="f-name" className="visually">Feature name</label>
        <input
          id="f-name"
          type="text"
          placeholder="Feature name"
          maxLength={100}
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <label htmlFor="f-desc" className="visually">Feature description</label>
        <textarea
          id="f-desc"
          rows={3}
          placeholder="Describe feature request..."
          maxLength={1000}
          required
          value={desc}
          onChange={(e) => setDesc(e.target.value)}
        />
        <div className="row">
          <button id="f-submit" type="submit" className="primary" disabled={busy}>
            Request feature
          </button>
          {msg && <span id="f-msg" className="muted" role="status">{msg}</span>}
        </div>
      </form>

      <div id="f-list" className="f-list" role="feed" aria-label="Feature requests">
        {(features || []).map((f) => {
          const dateStr = f.created_at ? new Date(f.created_at * 1000).toLocaleDateString() : '';
          return (
            <article key={f.id} className="f-card">
              <div className="f-head">
                <h2 className="f-title">{f.name}</h2>
                {isAdmin && (
                  <button
                    className="f-del"
                    type="button"
                    aria-label={'Delete feature ' + f.name}
                    onClick={() => onDeleteFeature(f.id)}
                  >
                    Delete
                  </button>
                )}
              </div>
              <p className="f-desc">{f.description}</p>
              <div className="muted">
                Added by {f.author || 'anonymous'} {dateStr && '• ' + dateStr}
              </div>
            </article>
          );
        })}
      </div>
      <p id="f-empty" className="muted" hidden={(features || []).length > 0}>
        No feature requests yet.
      </p>
    </div>
  );
}
