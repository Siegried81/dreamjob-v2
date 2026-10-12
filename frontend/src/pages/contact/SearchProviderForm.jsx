/**
 * The search API used for contact discovery and profile enrichment (FR-303, FR-122).
 *
 * Administrator only: the key is a deployment credential, not a job seeker's.
 * The server never sends the stored key back - GET answers only whether one is
 * configured - so the key field always starts empty, and leaving it empty on
 * save keeps the key already stored. Scraping a search engine's HTML page is
 * not offered because the engines disallow it in robots.txt (IR-101).
 */

import { useEffect, useState } from 'react'

import { api } from '../../api/client'
import { ErrorBox, Field, Loading, SectionCard, useFetch } from '../../components/ui'

const PROVIDERS = [
  { key: 'brave', label: 'Brave Search API' },
  { key: 'bing', label: 'Bing Web Search API' },
  { key: 'google_cse', label: 'Google Custom Search (CSE)' },
]

export default function SearchProviderForm() {
  const current = useFetch(() => api.get('/contacts/search-provider'), [])
  const [enabled, setEnabled] = useState(false)
  const [provider, setProvider] = useState('')
  const [url, setUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!current.data) return
    setEnabled(!!current.data.enabled)
    setProvider(current.data.provider || '')
    setUrl(current.data.url || '')
  }, [current.data])

  async function save(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setSaved(false)
    try {
      const answer = await api.put('/contacts/search-provider', {
        enabled,
        provider,
        api_key: apiKey,
        url: provider === 'google_cse' ? url : '',
      })
      current.setData(answer)
      setApiKey('')
      setSaved(true)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(false)
    }
  }

  if (current.loading) return <Loading rows={2} />
  if (current.error) return <ErrorBox error={current.error} onRetry={current.reload} />

  const configured = !!current.data?.configured
  return (
    <SectionCard icon="search" title="Search provider">
      <p className="small muted" style={{ marginTop: 0 }}>{current.data?.note}</p>
      {error && <ErrorBox error={error} />}
      {saved && <div className="alert alert-ok">Saved.</div>}
      <form onSubmit={save} style={{ maxWidth: 520 }}>
        <Field label="Provider">
          <select value={provider} onChange={(e) => setProvider(e.target.value)}>
            <option value="">None</option>
            {PROVIDERS.map((p) => (
              <option key={p.key} value={p.key}>{p.label}</option>
            ))}
          </select>
        </Field>
        <Field
          label="API key"
          hint={
            configured
              ? 'A key is stored. It is never shown; leave this empty to keep it.'
              : 'Required before search can run.'
          }
        >
          <input
            type="password"
            value={apiKey}
            autoComplete="off"
            maxLength={200}
            placeholder={configured ? 'stored - leave empty to keep' : ''}
            onChange={(e) => setApiKey(e.target.value)}
          />
        </Field>
        {provider === 'google_cse' && (
          <Field label="Endpoint URL" hint="The Custom Search JSON API URL, including cx=.">
            <input
              type="url"
              value={url}
              maxLength={500}
              placeholder="https://www.googleapis.com/customsearch/v1?cx=..."
              onChange={(e) => setUrl(e.target.value)}
            />
          </Field>
        )}
        <label className="row" style={{ gap: 8, margin: '10px 0' }}>
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          Use this provider for contact discovery and profile enrichment
        </label>
        <button className="btn btn-primary" disabled={busy}>
          {busy ? <span className="spinner" /> : 'Save'}
        </button>
      </form>
    </SectionCard>
  )
}
