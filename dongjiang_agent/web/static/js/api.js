let csrfToken = ""

export function setCsrfToken(value) {
  csrfToken = value || ""
}

async function request(path, options = {}) {
  const headers = {...(options.headers || {})}
  if (options.method && options.method !== "GET" && csrfToken) headers["X-CSRF-Token"] = csrfToken
  const response = await fetch(path, {...options, headers, credentials:"same-origin"})
  const data = await response.json().catch(() => ({ok:false, error:"服务返回了无效响应"}))
  if (response.status === 401) window.dispatchEvent(new Event("app:unauthorized"))
  if (!response.ok || !data.ok) throw new Error(data.error || "请求失败")
  return data
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
  setup:(payload) => post("/api/auth/setup", payload),
  login:(payload) => post("/api/auth/login", payload),
  logout:() => post("/api/auth/logout", {}),
  changePassword:(payload) => post("/api/auth/change-password", payload),
  listUsers:() => request("/api/users"),
  createUser:(payload) => post("/api/users", payload),
  updateUser:(userId, payload) => patch(`/api/users/${encodeURIComponent(userId)}`, payload),
  assignCaseOwner:(caseId, ownerUserId) => patch(`/api/cases/${encodeURIComponent(caseId)}`, {owner_user_id:ownerUserId}),
  listAudit:() => request("/api/audit"),
  listCases:({mine = false} = {}) => request(`/api/cases${mine ? "?mine=1" : ""}`),
  getCase:(caseId) => request(`/api/cases/${encodeURIComponent(caseId)}`),
  getDocumentFragment:(caseId, documentId, fragmentId = "") => request(`/api/cases/${encodeURIComponent(caseId)}/documents/${encodeURIComponent(documentId)}?fragment=${encodeURIComponent(fragmentId)}`),
  createCase:(payload) => post("/api/cases", payload),
  submitCreditDocuments:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/credit-documents`, payload),
  submitCreditAction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/credit-actions`, payload),
  submitContract:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/contracts`, payload),
  submitContractAction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/contract-actions`, payload),
}
