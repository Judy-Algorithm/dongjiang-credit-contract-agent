async function request(path, options = {}) {
  const response = await fetch(path, options)
  const data = await response.json().catch(() => ({ok:false, error:"服务返回了无效响应"}))
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

export const api = {
  listCases:() => request("/api/cases"),
  getCase:(caseId) => request(`/api/cases/${encodeURIComponent(caseId)}`),
  createCase:(payload) => post("/api/cases", payload),
  submitCreditDocuments:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/credit-documents`, payload),
  submitCreditAction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/credit-actions`, payload),
  submitContract:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/contracts`, payload),
  submitContractAction:(caseId, payload) => post(`/api/cases/${encodeURIComponent(caseId)}/contract-actions`, payload),
}
