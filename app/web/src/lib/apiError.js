// Human-readable formatting for Axios/Fetch errors raised by the
// review-ui API. The trigger endpoint (and many others) can fail with
// two distinct response shapes:
//
//   1. Manual HTTPException — ``{detail: "unknown persona(s): X — ..."}``
//      ``detail`` is a string and just renders as-is.
//   2. Pydantic 422 — ``{detail: [{loc, msg, type, ...}, ...]}``
//      ``detail`` is an array. Template-stringifying it gives the
//      useless ``[object Object],[object Object]`` so the user can't
//      see what actually failed.
//
// This formatter handles both, plus the JS Error fallback when there
// is no response (network failure). Pulls the last ``loc`` segment
// (the field name) and the ``msg`` so a 422 reads like
// ``target_url: String should match pattern ...``.

export function formatApiError(e) {
  const detail = e?.response?.data?.detail
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((entry) => {
        if (!entry || typeof entry !== 'object') return String(entry)
        const loc = Array.isArray(entry.loc) ? entry.loc.slice(-1)[0] : entry.loc
        const msg = entry.msg || entry.message || JSON.stringify(entry)
        return loc ? `${loc}: ${msg}` : msg
      })
      .filter(Boolean)
    if (parts.length) return parts.join('; ')
  }
  if (detail && typeof detail === 'object') {
    return JSON.stringify(detail)
  }
  return e?.message || 'unknown error'
}
