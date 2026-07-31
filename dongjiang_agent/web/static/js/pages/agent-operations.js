import {api} from "../api.js?v=20260731-agentops2"
import {dateTime, escapeHtml} from "../format.js?v=20260731-nav"

const severityLabels = {
  critical:"严重异常", warning:"需要关注", legacy:"历史计划",
  pending:"等待核验", healthy:"运行健康",
}
const issueLabels = {
  plan_integrity:"计划完整性失败", execution_deviation:"执行偏差",
  node_failed:"节点失败", evidence_degraded:"证据降级",
  fallback_degraded:"降级回退", retried:"发生重试", legacy_plan:"历史计划",
}

export async function renderAgentOperationsPage(root) {
  const data = await api.getAgentOperations()
  const metrics = data.metrics || {}, plans = data.plans || []
  root.innerHTML = `
    <header class="page-header"><div><h1>Agent 运维</h1><p>跨案件检查动态计划完整性、执行偏差、节点降级和重试</p></div><button id="refreshAgents" class="secondary">刷新状态</button></header>
    <section class="metric-grid agent-ops-metrics">
      ${metric("动态计划", metrics.plan_total)}
      ${metric("治理覆盖率", percent(metrics.governance_coverage))}
      ${metric("严重异常", metrics.critical_plans, metrics.critical_plans ? "overdue" : "")}
      ${metric("需要关注", metrics.warning_plans, metrics.warning_plans ? "due_soon" : "")}
      ${metric("缓存复用", metrics.reused_nodes)}
    </section>
    <section class="panel agent-ops-panel">
      <div class="agent-ops-toolbar">
        <div><input id="agentSearch" type="search" placeholder="搜索案件号或客户名称" aria-label="搜索 Agent 计划"><select id="agentType" aria-label="Agent 类型"><option value="">全部 Agent</option><option value="credit">信用信审</option><option value="contract">合同审查</option></select><select id="agentSeverity" aria-label="健康状态"><option value="">全部状态</option><option value="critical">严重异常</option><option value="warning">需要关注</option><option value="pending">等待核验</option><option value="legacy">历史计划</option><option value="healthy">运行健康</option></select></div>
        <span id="agentResultCount">${plans.length} 个计划</span>
      </div>
      <div class="table-scroll"><table><thead><tr><th>案件 / Agent</th><th>计划身份</th><th>治理状态</th><th>节点运行</th><th>问题摘要</th><th>更新时间</th><th>操作</th></tr></thead><tbody id="agentRows"></tbody></table></div>
      <div id="emptyAgents" class="empty-state hidden"><h2>没有匹配的运行计划</h2><p>调整筛选条件，或等待新案件产生 Agent 运行记录。</p></div>
    </section>
    <footer class="report-footnote">${escapeHtml(data.security_notice || "")} · 生成于 ${dateTime(data.generated_at)}</footer>`

  const search = root.querySelector("#agentSearch"), agentType = root.querySelector("#agentType"), severity = root.querySelector("#agentSeverity")
  const renderRows = () => {
    const keyword = search.value.trim().toLowerCase()
    const filtered = plans.filter((plan) =>
      (!agentType.value || plan.agent === agentType.value)
      && (!severity.value || plan.severity === severity.value)
      && (!keyword || `${plan.case_id} ${plan.customer_name}`.toLowerCase().includes(keyword))
    )
    root.querySelector("#agentRows").innerHTML = filtered.map(planRow).join("")
    root.querySelector(".table-scroll").classList.toggle("hidden", !filtered.length)
    root.querySelector("#emptyAgents").classList.toggle("hidden", Boolean(filtered.length))
    root.querySelector("#agentResultCount").textContent = `${filtered.length} 个计划`
  }
  ;[search,agentType,severity].forEach((control) => control.addEventListener("input", renderRows))
  root.querySelector("#refreshAgents").addEventListener("click", () => window.dispatchEvent(new Event("app:navigate")))
  renderRows()
}

function metric(label, value, tone = "") {
  return `<article class="metric-card report-metric ${escapeHtml(tone)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "—")}</strong></article>`
}

function percent(value) {
  return value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`
}

function planRow(plan) {
  const issues = (plan.issue_types || []).map((issue) => issueLabels[issue] || issue)
  return `<tr class="agent-ops-row severity-${escapeHtml(plan.severity)}">
    <td><a class="case-name" href="/cases/${encodeURIComponent(plan.case_id)}?tab=agents" data-link>${escapeHtml(plan.customer_name || "未命名客户")}</a><small>${escapeHtml(plan.case_id)} · ${escapeHtml(plan.business_type || "—")} · ${escapeHtml(plan.owner || "未分配")}</small><b>${escapeHtml(plan.label || plan.agent)}</b></td>
    <td><code>${escapeHtml(plan.plan_id)}</code><small>Plan ${escapeHtml(plan.version || "—")} · ${plan.spec_hash ? `规范 ${escapeHtml(plan.spec_hash)}` : "无冻结哈希"}</small></td>
    <td><span class="badge agent-severity-${escapeHtml(plan.severity)}">${escapeHtml(severityLabels[plan.severity] || plan.severity)}</span><small>${integrityLabel(plan.integrity_status)} · ${auditLabel(plan.audit_status)}</small></td>
    <td><span>${plan.executed_count}/${plan.task_count} 已执行</span><small>失败 ${plan.failed_count} · 降级 ${plan.degraded_count} · 重试 ${plan.retry_count} · 复用 ${plan.reused_count}</small></td>
    <td>${issues.length ? `<div class="agent-issue-list">${issues.map((issue) => `<span>${escapeHtml(issue)}</span>`).join("")}</div>` : `<span class="muted">未发现异常</span>`}</td>
    <td>${dateTime(plan.updated_at)}</td>
    <td><a class="primary small" href="/cases/${encodeURIComponent(plan.case_id)}?tab=agents" data-link>查看运行</a></td>
  </tr>`
}

function integrityLabel(value) {
  return ({valid:"完整性通过",invalid:"完整性失败",legacy:"历史计划"})[value] || value
}

function auditLabel(value) {
  return ({conformant:"执行一致",non_conformant:"执行偏差",pending:"等待审计"})[value] || value
}
