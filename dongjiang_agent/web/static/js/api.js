let csrfToken = ""
const responseCache = new Map()

export function setCsrfToken(value) {
  csrfToken = value || ""
}

async function request(path, options = {}) {
  const cacheTtl = Number(options.cacheTtl || 0)
  const method = options.method || "GET"
  const cachedValue = responseCache.get(path)
  if (method === "GET" && cacheTtl > 0 && cachedValue?.value && Date.now() - cachedValue.at < cacheTtl) return cachedValue.value
  if (method === "GET" && cacheTtl > 0 && cachedValue?.promise) return cachedValue.promise
  const headers = {...(options.headers || {})}
  if (method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken
  const execute = async () => {
    const {cacheTtl:_, ...fetchOptions} = options
    const response = await fetch(path, {...fetchOptions, headers, credentials:"same-origin"})
    const data = await response.json().catch(() => ({ok:false, error:"服务返回了无效响应"}))
    if (response.status === 401) window.dispatchEvent(new Event("app:unauthorized"))
    if (!response.ok || !data.ok) throw new Error(data.error || "请求失败")
    if (method !== "GET") responseCache.clear()
    return data
  }
  if (method !== "GET" || cacheTtl <= 0) return execute()
  const promise = execute().then((value) => {
    responseCache.set(path, {at:Date.now(), value})
    return value
  }).catch((error) => {
    responseCache.delete(path)
    throw error
  })
  responseCache.set(path, {at:Date.now(), promise})
  return promise
}

function cached(path, ttl = 15000) {
  return request(path, {cacheTtl:ttl})
}

export async function encodeFiles(fileList) {
  const rows = []
  for (const [index, file] of Array.from(fileList || []).entries()) {
    const bytes = new Uint8Array(await file.arrayBuffer())
    let binary = ""
    const chunkSize = 0x8000
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
    }
    rows.push({name:file.name, data_base64:btoa(binary), index})
  }
  return rows
}

function post(path, payload) {
  return request(path, {
    method:"POST",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload),
  })
}

function patch(path, payload) {
  return request(path, {
    method:"PATCH",
    headers:{"Content-Type":"application/json"},
    body:JSON.stringify(payload),
  })
}

export const api = {
  authStatus:() => request("/api/auth/status"),
  listImpersonationUsers:() => request("/api/auth/impersonation/users"),
  startImpersonation:(userId) => post("/api/auth/impersonate", {user_id:userId}),
  stopImpersonation:() => post("/api/auth/impersonation/stop", {}),
  setup:(payload) => post("/api/auth/setup", payload),
  login:(payload) => post("/api/auth/login", payload),
  requestRegistrationCode:(payload) => post("/api/auth/registration-code", payload),
  register:(payload) => post("/api/auth/register", payload),
  requestPasswordReset:(payload) => post("/api/auth/password-reset/request", payload),
  confirmPasswordReset:(payload) => post("/api/auth/password-reset/confirm", payload),
  logout:() => post("/api/auth/logout", {}),
  changePassword:(payload) => post("/api/auth/change-password", payload),
  listUsers:() => cached("/api/users"),
  createUser:(payload) => post("/api/users", payload),
  updateUser:(userId, payload) => patch(`/api/users/${encodeURIComponent(userId)}`, payload),
  listNotifications:() => cached("/api/notifications", 8000),
  listRequestTemplates:() => request("/api/request-templates"),
  createRequestTemplate:(payload) => post("/api/request-templates", payload),
  deleteRequestTemplate:(templateId) => post(`/api/request-templates/${encodeURIComponent(templateId)}/delete`, {}),
  navigationSummary:(refresh = false) => refresh ? request("/api/navigation-summary") : cached("/api/navigation-summary", 15000),
  markNotificationRead:(notificationId) => post(`/api/notifications/${encodeURIComponent(notificationId)}/read`, {}),
  markAllNotificationsRead:() => post("/api/notifications/read-all", {}),
  assignCaseOwner:(caseId, ownerUserId) => patch(`/api/cases/${encodeURIComponent(caseId)}`, {owner_user_id:ownerUserId}),
  listAudit:() => cached("/api/audit", 15000),
  listWritebackFailures:() => cached("/api/operations/writebacks", 10000),
  getSlaDashboard:() => cached("/api/operations/sla", 10000),
  getAgentOperations:() => cached("/api/operations/agents", 10000),
  probeModelHealth:() => post("/api/operations/model/probe", {}),
  runSlaSweep:() => post("/api/operations/sla/sweep", {}),
  getAnalytics:(days = 30) => cached(`/api/operations/analytics?days=${encodeURIComponent(days)}`, 15000),
  analyticsExportUrl:(days, format) => `/api/operations/analytics/export.${encodeURIComponent(format)}?days=${encodeURIComponent(days)}`,
  getBenchmark:() => cached("/api/operations/benchmarks", 15000),
  runBenchmark:(repeats = 2) => post("/api/operations/benchmarks/run", {repeats}),
  benchmarkExportUrl:(format) => `/api/operations/benchmarks/export.${encodeURIComponent(format)}`,
  listCases:({mine = false} = {}) => cached(`/api/cases${mine ? "?mine=1" : ""}`, 10000),
  getCase:(caseId) => cached(`/api/cases/${encodeURIComponent(caseId)}`, 10000),
  getDocumentFragment:(caseId, documentId, fragmentId = "", highlight = "") => request(`/api/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentId)}?fragment=${encodeURIComponent(fragmentId)}&highlight=${encodeURIComponent(highlight)}`),
  createContractRevision:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/revisions`, payload),
  submitContractRevision:(caseId, revisionId) => post(`/api/cases/${encodeURIComponent(caseId)}/revisions/${encodeURIComponent(revisionId)}/submit`, {}),
  revisionDownloadUrl:(caseId, revisionId, kind) => `/api/cases/${encodeURIComponent(caseId)}/revisions/${encodeURIComponent(revisionId)}/${encodeURIComponent(kind)}`,
  createContractTranslation:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/translations`, payload),
  getContractTranslation:(caseId, translationId) => request(`/api/cases/${encodeURIComponent(caseId)}/translations/${encodeURIComponent(translationId)}`),
  confirmContractTranslation:(caseId, translationId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/translations/${encodeURIComponent(translationId)}/confirm`, payload),
  translationDownloadUrl:(caseId, translationId) => `/api/cases/${encodeURIComponent(caseId)}/translations/${encodeURIComponent(translationId)}/download`,
  createCase:(payload) => post("/api/cases", payload),
  submitCreditDocuments:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/credit-documents`, payload),
  submitCreditAction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/credit-actions`, payload),
  submitContract:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/contracts`, payload),
  submitContractAction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/contract-actions`, payload),
  retryWriteback:(caseId, phase, system) => post(`/api/cases/${encodeURIComponent(caseId)}/writeback-retries`, {phase, system}),
  manageAgentIncident:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/agent-incidents`, payload),
  manageAgentCandidate:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/agent-candidate-actions`, payload),
  generateStructuredExtraction:(caseId, documentKind) => post(`/api/cases/${encodeURIComponent(caseId)}/structured-extractions`, {document_kind:documentKind}),
  manageStructuredExtraction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/structured-extraction-actions`, payload),
  runMockEnterpriseApproval:(caseId, payload = {}) => post(`/api/cases/${encodeURIComponent(caseId)}/mock-enterprise-approval`, payload),
  sweepAgentIncidents:() => post("/api/operations/agents/sweep", {}),
}
