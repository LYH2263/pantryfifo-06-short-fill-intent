export async function api(path, opts = {}) {
  const r = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!r.ok) {
    let detail = r.statusText
    try { const j = await r.json(); detail = j.detail ?? JSON.stringify(j) } catch {}
    const message = typeof detail === 'string' ? detail : JSON.stringify(detail)
    const err = new Error(message)
    err.status = r.status
    err.data = typeof detail === 'string' ? null : detail
    throw err
  }
  if (r.status === 204) return null
  return r.json()
}
