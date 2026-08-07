import {api} from "../api.js?v=20260807-csrf-delete"
import {dateTime, escapeHtml} from "../format.js?v=20260807-request-templates"

export async function renderWritebacksPage(root) {
  const data = await api.listWritebackFailures()
  root.innerHTML = `<header class="page-header"><div><h1>回写运维</h1><p>集中处理 OA、CRM 和 SAP 的失败回写，成功记录不会重复调用</p></div><button id="refreshWritebacks" class="secondary">刷新状态</button></header>
    <section class="metric-grid writeback-metrics">
      ${metric("失败总数", data.total)}${metric("OA", count(data.failures,"oa"))}${metric("CRM", count(data.failures,"crm"))}${metric("SAP", count(data.failures,"sap"))}
    </section>
    <section class="panel"><div class="table-scroll"><table><thead><tr><th>案件</th><th>阶段</th><th>系统</th><th>最近尝试</th><th>失败原因</th><th>操作</th></tr></thead>
      <tbody>${data.failures.map(row).join("")}</tbody></table></div>
      ${data.failures.length ? "" : `<div class="empty-state"><h2>没有失败回写</h2><p>当前 OA、CRM 和 SAP 没有需要人工重试的记录。</p></div>`}
    </section>`
  root.querySelector("#refreshWritebacks").addEventListener("click", () => window.dispatchEvent(new Event("app:navigate")))
  root.querySelector("tbody")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-retry-writeback]")
    if (!button) return
    button.disabled = true
    try {
      const data = await api.retryWriteback(button.dataset.caseId, button.dataset.phase, button.dataset.system)
      window.dispatchEvent(new CustomEvent("app:toast", {detail:data.result.status === "succeeded" ? "回写重试成功" : "重试仍失败，请查看错误原因"}))
      window.dispatchEvent(new Event("app:navigate"))
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
    }
  })
}

function metric(label, value) { return `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${value}</strong></article>` }
function count(rows, system) { return rows.filter((item) => item.system === system).length }
function row(item) {
  return `<tr><td><a class="case-id" href="/cases/${encodeURIComponent(item.case_id)}" data-link>${escapeHtml(item.case_id)}</a><small>${escapeHtml(item.customer_name || "未命名客户")}</small></td><td>${escapeHtml(phaseLabel(item.phase))}</td><td><span class="badge high">${escapeHtml(item.system.toUpperCase())}</span></td><td>${dateTime(item.attempted_at)}</td><td class="integration-error">${escapeHtml(item.error || "未知错误")}</td><td><button class="primary small" data-retry-writeback data-case-id="${escapeHtml(item.case_id)}" data-phase="${escapeHtml(item.phase)}" data-system="${escapeHtml(item.system)}">重试</button></td></tr>`
}
function phaseLabel(value) { return ({credit_activation:"授信生效",final:"案件完成",inactivation:"授信清零"})[value] || value }
