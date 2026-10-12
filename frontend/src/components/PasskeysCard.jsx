/**
 * The signed-in account's passkeys: list, add, remove (NFR-202).
 *
 * A passkey signs in without the password, so adding one is offered here,
 * beside the account's other private settings, and removing one takes effect
 * at once. Password + authenticator-code sign-in is unaffected either way.
 */

import { useState } from 'react'

import { api } from '../api/client'
import { passkeysSupported, registerPasskey } from '../api/passkeys'
import { ErrorBox, Loading, SectionCard, useFetch } from './ui'

export default function PasskeysCard() {
  const list = useFetch(() => api.get('/auth/me/passkeys'), [])
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function add() {
    setBusy(true)
    setError(null)
    try {
      await registerPasskey(name.trim() || undefined)
      setName('')
      list.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove(id) {
    setBusy(true)
    setError(null)
    try {
      await api.del(`/auth/me/passkeys/${encodeURIComponent(id)}`)
      list.reload()
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <SectionCard icon="lock" title="Sign-in passkeys">
      {error && <ErrorBox error={error} />}
      <p className="small muted" style={{ marginTop: 0 }}>
        A passkey lets you sign in with this device&apos;s fingerprint, face or PIN instead of
        your password. Open Dream Job at <code>http://localhost</code>, not an IP address, for
        passkeys to work.
      </p>
      {list.loading ? (
        <Loading rows={1} />
      ) : list.error ? (
        <ErrorBox error={list.error} onRetry={list.reload} />
      ) : (list.data || []).length === 0 ? (
        <p className="small muted">No passkey registered.</p>
      ) : (
        <ul style={{ paddingLeft: 18 }}>
          {list.data.map((p) => (
            <li key={p.id} style={{ marginBottom: 6 }}>
              <strong>{p.name}</strong>{' '}
              <span className="small muted">
                added {p.created_at?.slice(0, 10)}
                {p.last_used_at ? `, last used ${p.last_used_at.slice(0, 10)}` : ', never used'}
              </span>{' '}
              <button className="btn btn-sm btn-ghost" disabled={busy} onClick={() => remove(p.id)}>
                Remove
              </button>
            </li>
          ))}
        </ul>
      )}
      {passkeysSupported() ? (
        <div className="row row-wrap" style={{ gap: 8 }}>
          <input
            type="text"
            value={name}
            maxLength={80}
            placeholder="Name, e.g. Work laptop"
            onChange={(e) => setName(e.target.value)}
          />
          <button className="btn" disabled={busy} onClick={add}>
            Add a passkey
          </button>
        </div>
      ) : (
        <p className="small muted">This browser or address cannot use passkeys.</p>
      )}
    </SectionCard>
  )
}
