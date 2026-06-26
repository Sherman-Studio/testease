// Small shared formatting helpers.

export function formatDate(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString()
}

// Compact timestamp for dense lists: "12:34:56" if today, "Apr 12 12:34" otherwise.
export function formatTimestamp(value) {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  if (sameDay) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  }
  return d.toLocaleString([], {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })
}

// "5m 24s ago", "just now", etc. Cheap relative-time without an i18n dep.
export function relativeTime(value) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const diff = Math.max(0, Date.now() - d.getTime())
  const s = Math.floor(diff / 1000)
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m ago`
  const days = Math.floor(h / 24)
  if (days < 30) return `${days}d ago`
  return formatDate(value)
}

// Compact token readout for run weight: 0 → "0", 950 → "950",
// 12_400 → "12.4k", 3_200_000 → "3.2M". #1822 replaced the misleading
// per-run $ figure (runs bill the operator's flat-rate Max subscription,
// not metered API spend) with raw token totals as the "how heavy was
// this run" signal.
export function formatTokens(n) {
  const v = Number(n) || 0
  if (v < 1000) return String(v)
  if (v < 1_000_000) {
    const k = v / 1000
    return `${k >= 100 ? Math.round(k) : k.toFixed(1).replace(/\.0$/, '')}k`
  }
  const m = v / 1_000_000
  return `${m >= 100 ? Math.round(m) : m.toFixed(1).replace(/\.0$/, '')}M`
}
