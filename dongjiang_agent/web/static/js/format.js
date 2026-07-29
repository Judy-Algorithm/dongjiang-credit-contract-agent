export const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => (
  {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]
))

export function money(value) {
  return value == null ? "—" : `¥${Number(value).toLocaleString("zh-CN")}`
}

export function dateTime(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : "—"
}

export function customerType(value) {
  return value === "existing" ? "存量客户" : value === "new" ? "新客户" : "—"
}

export function statusClass(status) {
  if (["approved","approved_by_exception","approved_after_manual_review","completed"].includes(status)) return "approved"
  if (["blocked","rejected"].includes(status)) return "blocked"
  if ([
    "credit_pending_approval","credit_supplement_required",
    "pending_special_approval","pending_manual_review","awaiting_contract",
  ].includes(status)) return "pending"
  return ""
}
