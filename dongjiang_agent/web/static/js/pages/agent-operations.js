import {api} from "../api.js?v=20260807-login-fix"
import {dateTime, escapeHtml} from "../format.js?v=20260807-login-fix"

const severityLabels = {
  critical:"严重异常", warning:"需要关注", legacy:"历史计划",
  pending:"等待核验", healthy:"运行健康",
}
const issueLabels = {
  plan_integrity:"计划完整性失败", execution_deviation:"执行偏差",
  node_failed:"节点失败", evidence_degraded:"证据降级",
  fallback_degraded:"降级回退", retried:"发生重试", legacy_plan:"历史计划",
  repeated_degradation:"连续分析降级",
}
const incidentLabels = {
  open:"待确认", acknowledged:"已确认", assigned:"已分派",
  rerun_completed:"候选重跑完成", resolved:"已关闭",
}

export async function renderAgentOperationsPage(root) {
  const data = await api.getAgentOperations()
  const metrics = data.metrics || {}, plans = data.plans || [], modelHealth = data.model_health || {}
  root.innerHTML = `
    <header class="page-header"><div><h1>Agent 运维</h1><p>跨案件发现、处置并跟踪信用与合同子 Agent 的运行异常</p></div><div class="header-actions"><button id="probeModel" class="secondary">探测文本模型</button><button id="sweepAgents" class="secondary">立即扫描</button><button id="refreshAgents" class="secondary">刷新状态</button></div></header>
    ${modelHealthPanel(modelHealth)}
    <section class="metric-grid agent-ops-metrics">
      ${metric("动态计划", metrics.plan_total)}
      ${metric("治理覆盖率", percent(metrics.governance_coverage))}
      ${metric("严重异常", metrics.critical_plans, metrics.critical_plans ? "overdue" : "")}
      ${metric("需要关注", metrics.warning_plans, metrics.warning_plans ? "due_soon" : "")}
      ${metric("开放异常单", metrics.open_incidents)}
      ${metric("响应逾期", metrics.response_overdue, metrics.response_overdue ? "overdue" : "")}
      ${metric("解决逾期", metrics.resolution_overdue, metrics.resolution_overdue ? "overdue" : "")}
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
  root.querySelector("#probeModel").addEventListener("click", async (event) => {
    const button = event.currentTarget
    button.disabled = true
    try {
      const result = await api.probeModelHealth()
      const status = result.latest?.status === "healthy" ? "可用" : "不可用"
      window.dispatchEvent(new CustomEvent("app:toast", {detail:`文本模型探测完成：${status}`}))
      window.dispatchEvent(new Event("app:navigate"))
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
    }
  })
  root.querySelector("#sweepAgents").addEventListener("click", async (event) => {
    const button = event.currentTarget
    button.disabled = true
    try {
      const result = await api.sweepAgentIncidents()
      window.dispatchEvent(new CustomEvent("app:toast", {detail:`扫描完成，新建 ${result.opened_incidents || 0} 个异常单`}))
      window.dispatchEvent(new Event("app:navigate"))
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
    }
  })
  root.querySelector("#agentRows").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-incident-action]")
    if (!button) return
    const plan = plans.find((item) => item.plan_id === button.dataset.planId && item.case_id === button.dataset.caseId)
    if (plan) await showIncidentDialog(root, plan, button.dataset.incidentAction)
  })
  renderRows()
}

function modelHealthPanel(health) {
  const latest = health.latest || {}
  const status = latest.status || (health.configured ? "unknown" : "not_configured")
  const labels = {healthy:"可用",unhealthy:"不可用",model_unavailable:"模型不存在",not_configured:"未配置",unknown:"等待探测"}
  const tone = status === "healthy" ? "healthy" : status === "unknown" || status === "not_configured" ? "pending" : "critical"
  return `<section class="model-health-bar ${tone}"><div><small>文本模型</small><b>${escapeHtml(health.model || "未配置")}</b></div><div><small>当前状态</small><b>${escapeHtml(labels[status] || status)}</b></div><div><small>最近探测 / 调用</small><b>${latest.at ? dateTime(latest.at) : "尚无记录"}</b></div><div><small>最近成功率</small><b>${percent(health.recent_success_rate)}</b></div><div><small>最近错误</small><b>${latest.http_status ? `HTTP ${latest.http_status}` : escapeHtml(latest.error_type || "无")}</b></div></section>`
}

function metric(label, value, tone = "") {
  return `<article class="metric-card report-metric ${escapeHtml(tone)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "—")}</strong></article>`
}

function percent(value) {
  return value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`
}

function planRow(plan) {
  const issues = (plan.issue_types || []).map((issue) => issueLabels[issue] || issue)
  const incident = plan.incident || {}, incidentStatus = incident.status || ""
  const canOpenIncident = Boolean(plan.incident_eligible) && (!incidentStatus || incidentStatus === "resolved")
  return `<tr class="agent-ops-row severity-${escapeHtml(plan.severity)}">
    <td><a class="case-name" href="/cases/${encodeURIComponent(plan.case_id)}?tab=agents" data-link>${escapeHtml(plan.customer_name || "未命名客户")}</a><small>${escapeHtml(plan.case_id)} · ${escapeHtml(plan.business_type || "—")} · ${escapeHtml(plan.owner || "未分配")}</small><b>${escapeHtml(plan.label || plan.agent)}</b></td>
    <td><code>${escapeHtml(plan.plan_id)}</code><small>Plan ${escapeHtml(plan.version || "—")} · ${plan.spec_hash ? `规范 ${escapeHtml(plan.spec_hash)}` : "无冻结哈希"}</small></td>
    <td><span class="badge agent-severity-${escapeHtml(plan.severity)}">${escapeHtml(severityLabels[plan.severity] || plan.severity)}</span><small>${integrityLabel(plan.integrity_status)} · ${auditLabel(plan.audit_status)}</small></td>
    <td><span>${plan.executed_count}/${plan.task_count} 已执行</span><small>失败 ${plan.failed_count} · 降级 ${plan.degraded_count} · 重试 ${plan.retry_count} · 复用 ${plan.reused_count}</small></td>
    <td>${issues.length ? `<div class="agent-issue-list">${issues.map((issue) => `<span>${escapeHtml(issue)}</span>`).join("")}</div>` : `<span class="muted">未发现异常</span>`}${incidentStatus ? `<div class="incident-state"><b>${escapeHtml(incidentLabels[incidentStatus] || incidentStatus)}${incident.source === "automatic" ? " · 自动发现" : ""}</b><small>${escapeHtml(incident.assignee?.display_name || "未分派")} · 重跑 ${incident.rerun_count || 0} 次</small>${slaSummary(incident.sla)}</div>` : ""}</td>
    <td>${dateTime(plan.updated_at)}</td>
    <td><div class="agent-row-actions"><a class="primary small" href="/cases/${encodeURIComponent(plan.case_id)}?tab=agents" data-link>查看运行</a>${canOpenIncident || incidentStatus === "open" ? actionButton(plan,"acknowledge","确认异常") : ""}${incidentStatus && !["open","resolved"].includes(incidentStatus) ? `${actionButton(plan,"assign","分派")}${plan.retryable_tasks?.length ? actionButton(plan,"rerun","候选重跑") : ""}${actionButton(plan,"resolve","关闭")}` : ""}</div></td>
  </tr>`
}

function slaSummary(sla = {}) {
  const response = sla.response || {}, resolution = sla.resolution || {}
  const tone = [response.state,resolution.state].includes("overdue") ? "overdue" : [response.state,resolution.state].includes("due_soon") ? "due_soon" : ""
  const phase = response.state === "completed" ? `解决 ${slaState(resolution)}` : `响应 ${slaState(response)}`
  return `<small class="incident-sla ${tone}">${escapeHtml(phase)}</small>`
}

function slaState(item = {}) {
  if (item.state === "completed") return "已完成"
  if (item.remaining_hours == null) return item.state_label || "未配置"
  return item.remaining_hours < 0 ? `逾期 ${Math.abs(item.remaining_hours).toFixed(1)} 小时` : `剩余 ${Number(item.remaining_hours).toFixed(1)} 小时`
}

function actionButton(plan, action, label) {
  return `<button class="text-button" data-incident-action="${escapeHtml(action)}" data-case-id="${escapeHtml(plan.case_id)}" data-plan-id="${escapeHtml(plan.plan_id)}">${escapeHtml(label)}</button>`
}

async function showIncidentDialog(root, plan, action) {
  let users = []
  if (action === "assign") users = (await api.listUsers()).users.filter((user) => user.active)
  const title = ({acknowledge:"确认Agent异常",assign:"分派异常责任人",rerun:"受控候选重跑",resolve:"关闭Agent异常"})[action] || "处置Agent异常"
  const incident = plan.incident || {}
  root.insertAdjacentHTML("beforeend", `<div id="agentIncidentDialog" class="modal-backdrop"><form class="modal-panel agent-incident-dialog"><div class="section-heading"><h3>${escapeHtml(title)}</h3><button type="button" class="text-button" data-close>关闭</button></div><p class="field-hint">案件 ${escapeHtml(plan.case_id)} · ${escapeHtml(plan.label)} · ${escapeHtml(plan.plan_id)}</p>
    ${action === "assign" ? `<label>责任人<select id="incidentAssignee" required>${users.map((user) => `<option value="${escapeHtml(user.user_id)}" ${user.user_id === incident.assignee?.user_id ? "selected" : ""}>${escapeHtml(user.display_name)} · ${escapeHtml(user.role_labels.join("/"))}</option>`).join("")}</select></label>` : ""}
    ${action === "rerun" ? `<label>允许重跑的分析节点<select id="incidentTask" required>${(plan.retryable_tasks || []).map((task) => `<option value="${escapeHtml(task.task_id)}">${escapeHtml(task.label)} · ${escapeHtml(task.task_type)}</option>`).join("")}</select></label><div class="agent-rerun-notice">候选重跑会在隔离副本中重新执行分析、汇总与核验，不会修改正式授信、合同结论或审批状态。</div>` : ""}
    <label>处理说明<textarea id="incidentNote" rows="4" maxlength="500" ${["rerun","resolve"].includes(action) ? "required minlength=2" : ""} placeholder="记录判断依据、排查结果或后续动作"></textarea></label><div id="incidentError" class="error-box hidden"></div><div class="form-actions"><span></span><button class="primary" type="submit">确认${escapeHtml(title.replace(/^确认/,""))}</button></div></form></div>`)
  const dialog = root.querySelector("#agentIncidentDialog")
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.remove())
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.remove() })
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault(); const error = dialog.querySelector("#incidentError"), submit = event.submitter
    error.classList.add("hidden"); submit.disabled = true
    try {
      await api.manageAgentIncident(plan.case_id, {action,plan_id:plan.plan_id,note:dialog.querySelector("#incidentNote").value,assignee_user_id:dialog.querySelector("#incidentAssignee")?.value || "",task_id:dialog.querySelector("#incidentTask")?.value || ""})
      window.dispatchEvent(new CustomEvent("app:toast", {detail:action === "rerun" ? "候选重跑已完成，正式结论未改变" : "Agent异常状态已更新"}))
      window.dispatchEvent(new Event("app:navigate"))
    } catch (reason) { error.textContent = reason.message || String(reason); error.classList.remove("hidden"); submit.disabled = false }
  })
}

function integrityLabel(value) {
  return ({valid:"完整性通过",invalid:"完整性失败",legacy:"历史计划"})[value] || value
}

function auditLabel(value) {
  return ({conformant:"执行一致",non_conformant:"执行偏差",pending:"等待审计"})[value] || value
}
