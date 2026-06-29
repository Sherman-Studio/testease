import axios from 'axios'

// Same-origin: in production FastAPI serves both the SPA and /api; in dev the
// Vite proxy (vite.config.js) forwards /api to the local uvicorn.
const http = axios.create({ baseURL: '/api' })

// ── BYOK: LLM backend config ──────────────────────────────────────────────
// Status only — the API never returns the token. setLLMConfig posts an
// optional new token (vaulted server-side); omit it to change only the backend.
export function getLLMConfig() {
  return http.get('/config/llm').then((r) => r.data)
}
export function setLLMConfig(payload) {
  return http.put('/config/llm', payload).then((r) => r.data)
}
export function clearLLMToken() {
  return http.delete('/config/llm/token').then((r) => r.data)
}

export function listRuns() {
  return http.get('/runs').then((r) => r.data)
}

export function getRun(runId) {
  return http.get(`/runs/${encodeURIComponent(runId)}`).then((r) => r.data)
}

export function setFindingStatus(findingId, status) {
  return http
    .patch(`/findings/${encodeURIComponent(findingId)}`, { status })
    .then((r) => r.data)
}

export function fileIssue(runId) {
  return http
    .post(`/runs/${encodeURIComponent(runId)}/file-issue`)
    .then((r) => r.data)
}

// #1115 — server-side filing for a single finding. Returns
// {gh_issue_url, gh_issue_number}. The API
// 409s if the finding has already been filed (frontend should treat
// that as "already linked — show the existing URL" rather than an
// error).
export function fileFindingIssue(findingId) {
  return http
    .post(`/findings/${encodeURIComponent(findingId)}/file-issue`)
    .then((r) => r.data)
}

export function syncFindingGhState(findingId) {
  return http
    .post(`/findings/${encodeURIComponent(findingId)}/sync-gh-state`)
    .then((r) => r.data)
}

// -- Run control ------------------------------------------------------------
export function getPersonas() {
  return http.get('/runs/personas').then((r) => r.data.personas)
}

export function getActiveRun() {
  return http.get('/runs/active').then((r) => r.data.active)
}

// Per-run overrides — all optional. Omit a field (or pass null/undefined) and
// the harness uses the pod-spec default for that knob.
//
//   - `concurrency`         1..6 → QA_HARNESS_CONCURRENCY            (#824)
//   - `podCount`            2..4 → N-Jobs fan-out (omit = single pod) (#1821)
//   - `exploreModel`        allowlisted id → QA_EXPLORE_MODEL        (#836)
//   - `reportModel`         allowlisted id → QA_REPORT_MODEL         (#836)
//   - `maxTurns`            10..5000 → QA_MAX_TURNS                  (#858, #1115)
//   - `runDurationS`        300..7200 → QA_RUN_TIMEOUT_S              (#1115)
//   - `runNotes`            free text ≤500 chars → QA_RUN_NOTES      (#858)
//   - `mandatoryActionIds`  list[str] ≤50, ids in CATALOG → QA_MANDATORY_ACTIONS (#861)
//   - `targetUrl`           http(s) URL ≤500 chars → QA_WEB_BASE_URL (#1018)
//   - `enabledMCPServers`   list[str] ≤20, ids in catalog → QA_ENABLED_MCPS (#1031)
//
// The allowlists / bounds are enforced server-side (a Pydantic pattern); see
// TriggerRunRequest in api/qa_review_api/app.py for the canonical rules.
export function triggerRun(
  personas,
  {
    concurrency,
    podCount,
    exploreModel,
    reportModel,
    maxTurns,
    runDurationS,
    runNotes,
    mandatoryActionIds,
    targetUrl,
    targetId,
    enabledMCPServers,
  } = {},
) {
  const body = { personas }
  if (concurrency != null) body.concurrency = concurrency
  // #1821 — only forward an explicit multi-pod fan-out. null/omitted means
  // single-pod: the server forwards pod_count=1 and builds today's single
  // Job byte-for-byte. The product (pods × concurrency) ceiling is enforced
  // server-side (422); the UI guards it client-side too.
  if (podCount != null) body.pod_count = podCount
  if (exploreModel != null) body.explore_model = exploreModel
  if (reportModel != null) body.report_model = reportModel
  if (maxTurns != null) body.max_turns = maxTurns
  if (runDurationS != null) body.run_duration_s = runDurationS
  if (runNotes != null && runNotes !== '') body.run_notes = runNotes
  if (mandatoryActionIds && mandatoryActionIds.length > 0) {
    body.mandatory_action_ids = mandatoryActionIds
  }
  // #1018 — only forward target_url when the operator filled it in; an
  // empty string means "fall through to the CronJob template's baked-in
  // QA_WEB_BASE_URL", preserving the pre-#1018 single-tenant behaviour
  // for the SlyReply sandbox. Trim before send so trailing whitespace
  // from a paste doesn't 422 on the server-side http(s) pattern.
  if (targetUrl != null && targetUrl.trim() !== '') {
    body.target_url = targetUrl.trim()
  }
  // P4 — the registered target this run is for. When set, the server
  // auto-enables the MCP servers this target has granted capabilities for and
  // injects their vaulted credentials. Omitted ⇒ a plain URL-only run.
  if (targetId != null && targetId !== '') {
    body.target_id = targetId
  }
  // #1031 — only forward when the operator made an explicit selection
  // that's NOT the catalog defaults. An empty list / null defers to
  // the server, which defers to qa_agents.mcp_catalog defaults — the
  // pre-Slice-C behaviour preservation contract.
  if (enabledMCPServers && enabledMCPServers.length > 0) {
    body.enabled_mcp_servers = enabledMCPServers
  }
  return http.post('/runs/trigger', body).then((r) => r.data)
}

// #861 — Coverage catalog fetch for the trigger-page checklist + the
// run-detail mandatory-attempted indicator. Returns
// `{categories: string[], actions: CoverageAction[]}`. The catalog is
// static, so callers cache it for the page session — fetched once on
// mount, no invalidation.
export function getCoverageCatalog() {
  return http.get('/runs/coverage-catalog').then((r) => r.data)
}

// The live-log endpoint is a Server-Sent-Events stream — consumed with a
// native EventSource (not axios), so the component needs the absolute path.
export const ACTIVE_LOGS_URL = '/api/runs/active/logs'

// #860 — Transcript tab data + screenshot URL helper.
//
// `getTranscript` returns `{ steps: [...] }` — an empty list means the
// harness ran without the recorder wired (pre-#860) OR the persona made
// no tool calls. The component renders an "(no steps recorded)" state
// in both cases; the API treats them identically.
export function getTranscript(runId, personaId) {
  return http
    .get(
      `/runs/${encodeURIComponent(runId)}/personas/${encodeURIComponent(personaId)}/transcript`,
    )
    .then((r) => r.data.steps || [])
}

// Inline-able image URL for a screenshot oid (use as `<img :src="...">`).
// Bytes are streamed by the API and cached aggressively (the oid pins an
// immutable blob); browsers reuse the cache across step thumbnails and
// the full-size modal automatically.
export function screenshotUrl(runId, oid) {
  return `/api/runs/${encodeURIComponent(runId)}/screenshots/${encodeURIComponent(oid)}`
}

// #862 — saved scenarios. CRUD on the qa_scenarios collection. The
// server validates persona_id against KNOWN_PERSONAS and every
// mandatory_action_id against the catalog (422 on a typo before the
// row is created). All four helpers translate axios errors into the
// usual response.status / response.data shape the views surface to
// the operator.
export function listScenarios() {
  return http.get('/scenarios').then((r) => r.data.scenarios || [])
}
export function getScenario(scenarioId) {
  return http
    .get(`/scenarios/${encodeURIComponent(scenarioId)}`)
    .then((r) => r.data)
}
export function createScenario(payload) {
  return http.post('/scenarios', payload).then((r) => r.data)
}
export function updateScenario(scenarioId, patch) {
  return http
    .patch(`/scenarios/${encodeURIComponent(scenarioId)}`, patch)
    .then((r) => r.data)
}
export function deleteScenario(scenarioId) {
  return http
    .delete(`/scenarios/${encodeURIComponent(scenarioId)}`)
    .then(() => true)
}

// ---------------------------------------------------------------------------
// #902 / #904 — Transcript Search.
//
// Two helpers map to the two backend endpoints (added in this slice):
//
//   * getPersonaLogs   — per-persona chronological replay from qa_run_logs.
//                        Returns ``{ logs: [...] }``. Empty list = the run
//                        is pre-#903 or the persona never emitted — both are
//                        valid display states, not errors.
//   * searchTranscripts — cross-run case-insensitive regex match on
//                        ``content``. Returns ``{ results, count, query }``.
//                        ``query`` echoes the inputs so the UI can render a
//                        "you searched for: ..." caption.
//
// All params are optional; the backend treats empty strings the same as
// omitted (no text gate, etc.). Omits empty values so the URL stays clean.
// ---------------------------------------------------------------------------
export function getPersonaLogs(runId, personaId) {
  return http
    .get(
      `/runs/${encodeURIComponent(runId)}/personas/${encodeURIComponent(personaId)}/logs`,
    )
    .then((r) => r.data)
}

export function searchTranscripts({
  q, persona, kind, since, until, limit = 200,
} = {}) {
  const params = { limit }
  if (q) params.q = q
  if (persona) params.persona = persona
  if (kind) params.kind = kind
  if (since) params.since = since   // ISO-8601
  if (until) params.until = until   // ISO-8601
  return http
    .get('/transcripts/search', { params })
    .then((r) => r.data)
}

// ---------------------------------------------------------------------------
// QA Studio redesign — persona library CRUD against ``qa_personas``.
//
// Endpoints are documented in api/qa_review_api/app.py. Notable bits:
//
//   * ``listPersonas({ includeHidden })`` — default rows first, then
//     alphabetical. ``hidden`` is the soft-delete flag for seeded
//     personas (which can't be hard-deleted); the dashboard hides them
//     by default.
//   * ``createPersona`` — 409 on duplicate persona_id.
//   * ``updatePersona`` patch uses ``exclude_unset`` server-side, so an
//     omitted key is preserved and an explicit ``null`` clears (for the
//     three nullable fields). Build patches with the fields you actually
//     changed — don't blanket-send everything.
//   * ``deletePersona`` 422s on default personas (use ``hidden=true``
//     via updatePersona instead); 404 on unknown.
// ---------------------------------------------------------------------------
export function listPersonas({ includeHidden = false } = {}) {
  const params = {}
  if (includeHidden) params.include_hidden = true
  return http.get('/personas', { params }).then((r) => r.data.personas || [])
}

export function getPersonaDetail(personaId) {
  return http
    .get(`/personas/${encodeURIComponent(personaId)}`)
    .then((r) => r.data)
}

export function createPersona(payload) {
  return http.post('/personas', payload).then((r) => r.data)
}

export function updatePersona(personaId, patch) {
  return http
    .patch(`/personas/${encodeURIComponent(personaId)}`, patch)
    .then((r) => r.data)
}

export function deletePersona(personaId) {
  return http
    .delete(`/personas/${encodeURIComponent(personaId)}`)
    .then(() => true)
}

// ---------------------------------------------------------------------------
// #1146 — admin / nuclear-button API.
//
// adminWipe — POST a literal confirm token to drop every per-run +
// per-persona collection. The /admin page wraps it in a typed-WIPE
// modal so a single clicked button can't trigger this.
// listAdminWipes — recent wipe audit rows, newest first.
// ---------------------------------------------------------------------------
// #1108 — `wipeMailpit` is an opt-in toggle on the admin nuclear
// button. False by default; the /admin modal surfaces a checkbox
// the operator ticks for a true full-reset (cross-run inbox
// continuity is the norm, so the default is "leave Mailpit alone").
export function adminWipe({ confirm, requesterNote = '', wipeMailpit = false } = {}) {
  return http
    .post('/admin/wipe', {
      confirm,
      requester_note: requesterNote,
      wipe_mailpit: wipeMailpit,
    })
    .then((r) => r.data)
}

export function listAdminWipes({ limit = 20 } = {}) {
  return http
    .get('/admin/wipes', { params: { limit } })
    .then((r) => r.data.wipes || [])
}

// #1105 — persona credentials status. Returns {has_credentials,
// email?, registered_at?, verified?, last_rotation_n?,
// has_session_jwt?, jwt_expires_at?}. The password is intentionally
// never returned by the API — both the personas card badge and the
// detail-page reset row read from this shape only.
export function getPersonaCredentialsStatus(personaId) {
  return http
    .get(`/personas/${encodeURIComponent(personaId)}/credentials/status`)
    .then((r) => r.data)
}

// #1105 — operator "reset login" action. Clears the persona's saved
// credentials sub-doc so the next run signs up fresh. Idempotent on
// the server side (clearing already-empty creds returns 204).
export function clearPersonaCredentials(personaId) {
  return http
    .delete(`/personas/${encodeURIComponent(personaId)}/credentials`)
    .then(() => true)
}

// QA Studio — merged step + log timeline for a single run, sorted by ts.
// Used by the new RunDetail live-workflow panel; one round trip rather
// than two-per-persona for the transcript + logs.
export function getRunTimeline(runId) {
  return http
    .get(`/runs/${encodeURIComponent(runId)}/timeline`)
    .then((r) => r.data)
}

// ---------------------------------------------------------------------------
// Slice 1 of #1002 — discovered_* read endpoints.
//
// Three GETs over the new collections written by the harness's post-run
// distillation hook. All read-only; the operator browses what personas
// learned about the site without (yet) being able to edit the corpus.
// Slice 2 will add approval/merge writes.
//
// Filter params are all optional and combine — empty = newest-first feed:
//   listDiscoveredActions({ runId })            → this run's coverage
//   listDiscoveredActions({ category: 'auth' }) → all auth actions across runs
//   listDiscoveredActions({ personaId: 'maya' }) → maya's cumulative discoveries
// ---------------------------------------------------------------------------
function _discoveredParams({ runId, personaId, category, limit }) {
  const params = {}
  if (runId) params.run_id = runId
  if (personaId) params.persona_id = personaId
  if (category) params.category = category
  if (limit != null) params.limit = limit
  return params
}

export function listDiscoveredActions(opts = {}) {
  return http
    .get('/discovered-actions', { params: _discoveredParams(opts) })
    .then((r) => r.data)
}

export function listDiscoveredTools(opts = {}) {
  // Tools have no category dimension — drop that param if a caller
  // accidentally passes it.
  return http
    .get('/discovered-tools', {
      params: _discoveredParams({ ...opts, category: undefined }),
    })
    .then((r) => r.data)
}

export function listDiscoveredBranches(opts = {}) {
  return http
    .get('/discovered-branches', {
      params: _discoveredParams({ ...opts, category: undefined }),
    })
    .then((r) => r.data)
}

// Slice B of #1028 — MCP server catalog. Static data from
// qa_agents.mcp_catalog; fetched once on /mcp-tools mount, no
// pagination needed (catalog is curated, ~10s of entries at most).
export function listMCPServers() {
  return http.get('/mcp-servers').then((r) => r.data.servers)
}

// ---------------------------------------------------------------------------
// Site Model — per-(tenant, target) site knowledge as data (#2097/#2100).
//
// Read-only surfaces/flows + curation for site_knowledge: the human pass over
// the heuristic by-design migration (91/94 entries). All scoped server-side to
// DEFAULT_TENANT; the API strips the embedding vectors from every response.
// ---------------------------------------------------------------------------
export function listSiteTargets() {
  return http.get('/site/targets').then((r) => r.data.targets || [])
}

export function getSiteTarget(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}`)
    .then((r) => r.data)
}

// Register a new site to test (the onboarding front door). payload:
// { base_url, display_name?, target_id? } → the created target (lifecycle
// "registered"); the server slugifies + de-duplicates the target_id.
export function createSiteTarget(payload) {
  return http.post('/site/targets', payload).then((r) => r.data)
}

export function listSiteSurfaces(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}/surfaces`)
    .then((r) => r.data.surfaces || [])
}

export function listSiteFlows(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}/flows`)
    .then((r) => r.data.flows || [])
}

export function listSiteKnowledge(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}/knowledge`)
    .then((r) => r.data.knowledge || [])
}

export function createSiteKnowledge(targetId, payload) {
  return http
    .post(`/site/targets/${encodeURIComponent(targetId)}/knowledge`, payload)
    .then((r) => r.data)
}

// PATCH carries target_id in the body — the route path is only the entry_id but
// the (tenant, target, entry_id) key needs the target.
export function updateSiteKnowledge(entryId, targetId, patch) {
  return http
    .patch(`/site/knowledge/${encodeURIComponent(entryId)}`, {
      target_id: targetId,
      ...patch,
    })
    .then((r) => r.data)
}

export function deleteSiteKnowledge(entryId, targetId) {
  return http
    .delete(`/site/knowledge/${encodeURIComponent(entryId)}`, {
      params: { target_id: targetId },
    })
    .then(() => true)
}

// ---------------------------------------------------------------------------
// Explorer questionnaire (site_questions) + target onboarding lifecycle.
//
// The questionnaire is the product's hinge — consent + config + knowledge
// elicitation. Secret answers are vaulted server-side; the API never returns a
// raw secret value (answered secrets carry only a credential_ref pointer).
// ---------------------------------------------------------------------------
// Returns { questions, status, lifecycle, lifecycle_states }.
export function listSiteQuestions(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}/questions`)
    .then((r) => r.data)
}

export function createSiteQuestion(targetId, payload) {
  return http
    .post(`/site/targets/${encodeURIComponent(targetId)}/questions`, payload)
    .then((r) => r.data)
}

export function answerSiteQuestion(targetId, questionId, answer, label = '') {
  return http
    .post(
      `/site/targets/${encodeURIComponent(targetId)}/questions/${encodeURIComponent(questionId)}/answer`,
      { answer, label },
    )
    .then((r) => r.data)
}

export function skipSiteQuestion(targetId, questionId) {
  return http
    .post(
      `/site/targets/${encodeURIComponent(targetId)}/questions/${encodeURIComponent(questionId)}/skip`,
    )
    .then((r) => r.data)
}

export function deleteSiteQuestion(targetId, questionId) {
  return http
    .delete(
      `/site/targets/${encodeURIComponent(targetId)}/questions/${encodeURIComponent(questionId)}`,
    )
    .then(() => true)
}

export function setTargetLifecycle(targetId, lifecycle) {
  return http
    .post(`/site/targets/${encodeURIComponent(targetId)}/lifecycle`, { lifecycle })
    .then((r) => r.data)
}

// Run the heuristic explorer — bootstrap the model + questionnaire from the
// site's homepage and advance the lifecycle to awaiting-answers.
export function exploreSiteTarget(targetId) {
  return http
    .post(`/site/targets/${encodeURIComponent(targetId)}/explore`)
    .then((r) => r.data)
}

// ── Capabilities — grant deeper access ────────────────────────────────────
// Returns { depth, capabilities } (catalog merged with grant status). Secrets
// are vaulted server-side; tokens are never returned.
export function getSiteCapabilities(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}/capabilities`)
    .then((r) => r.data)
}
// P4 — the MCP servers a target's granted capabilities light up for its runs
// (names only; credentials never returned). Drives the New Run auto-enable hint.
export function getTargetMcp(targetId) {
  return http
    .get(`/site/targets/${encodeURIComponent(targetId)}/mcp`)
    .then((r) => r.data)
}
export function setCapability(targetId, capabilityId, payload) {
  return http
    .put(
      `/site/targets/${encodeURIComponent(targetId)}/capabilities/${encodeURIComponent(capabilityId)}`,
      payload,
    )
    .then((r) => r.data)
}
export function addCustomCapability(targetId, payload) {
  return http
    .post(`/site/targets/${encodeURIComponent(targetId)}/capabilities`, payload)
    .then((r) => r.data)
}
export function revokeCapability(targetId, capabilityId) {
  return http
    .delete(
      `/site/targets/${encodeURIComponent(targetId)}/capabilities/${encodeURIComponent(capabilityId)}`,
    )
    .then((r) => r.data)
}
