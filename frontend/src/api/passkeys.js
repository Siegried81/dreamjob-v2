/**
 * Passkeys in the browser (NFR-202).
 *
 * The server sends WebAuthn options as JSON with every byte string in
 * base64url, and expects the authenticator's answer in the same form; the
 * browser API wants and returns ArrayBuffers. These helpers do only that
 * conversion around `navigator.credentials.create` / `.get`, so the screens
 * stay unaware of the encoding. All verification happens on the server.
 */

import { api } from './client'

function toBuffer(b64url) {
  const b64 = b64url.replace(/-/g, '+').replace(/_/g, '/')
  const padded = b64 + '='.repeat((4 - (b64.length % 4)) % 4)
  const bin = atob(padded)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i += 1) out[i] = bin.charCodeAt(i)
  return out.buffer
}

function toB64url(buffer) {
  const bytes = new Uint8Array(buffer)
  let bin = ''
  for (let i = 0; i < bytes.length; i += 1) bin += String.fromCharCode(bytes[i])
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

const descriptors = (list) => (list || []).map((d) => ({ ...d, id: toBuffer(d.id) }))

/** True when this browser can use passkeys on this page at all. */
export function passkeysSupported() {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential && window.isSecureContext
}

/** Register a passkey for the signed-in account. Resolves to the stored entry. */
export async function registerPasskey(name) {
  const { ceremony_id, options } = await api.post('/auth/me/passkeys/options')
  const created = await navigator.credentials.create({
    publicKey: {
      ...options,
      challenge: toBuffer(options.challenge),
      user: { ...options.user, id: toBuffer(options.user.id) },
      excludeCredentials: descriptors(options.excludeCredentials),
    },
  })
  const credential = {
    id: created.id,
    rawId: toB64url(created.rawId),
    type: created.type,
    response: {
      clientDataJSON: toB64url(created.response.clientDataJSON),
      attestationObject: toB64url(created.response.attestationObject),
      transports: created.response.getTransports ? created.response.getTransports() : [],
    },
  }
  return api.post('/auth/me/passkeys', { ceremony_id, credential, name })
}

/** Sign in with a passkey; `email` is optional (discoverable credentials). */
export async function signInWithPasskey(email) {
  const { ceremony_id, options } = await api.post('/auth/passkey/options', {
    email: email || undefined,
  })
  const asserted = await navigator.credentials.get({
    publicKey: {
      ...options,
      challenge: toBuffer(options.challenge),
      allowCredentials: descriptors(options.allowCredentials),
    },
  })
  const credential = {
    id: asserted.id,
    rawId: toB64url(asserted.rawId),
    type: asserted.type,
    response: {
      clientDataJSON: toB64url(asserted.response.clientDataJSON),
      authenticatorData: toB64url(asserted.response.authenticatorData),
      signature: toB64url(asserted.response.signature),
      userHandle: asserted.response.userHandle ? toB64url(asserted.response.userHandle) : null,
    },
  }
  return api.post('/auth/passkey/login', { ceremony_id, credential })
}
