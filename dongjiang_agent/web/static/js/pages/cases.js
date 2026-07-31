import {api} from "../api.js"
import {customerType, dateTime, escapeHtml, statusClass} from "../format.js"

const pendingStatuses = new Set([
  "credit_pending_approval","credit_supplement_required",
  "awaiting_contract","blocked","pending_special_approval","pending_manual_review",
])
const approvalStatuses = new Set([
  "credit_pending_approval","pending_special_approval","pending_manual_review",
])

export async function renderCasesPage(root, route) {
  const mineOnly = route.params.get("mine") === "1"
  const data = await api.listCases({mine:mineOnly})
  const cases = data.cases || []
  root.innerHTML = `
    <header class="page-header">
      <div>
        <h1>${mineOnly ? "我的待办" : "案件列表"}</h1>
        <p>${mineOnly ? "需要继续处理的案件" : "查看客户信审、合同审核和审批状态"}</p>
      </div>
    </header>
    <section class="metric-grid">
      ${metric("全部", cases.length, "", !mineOnly)}
      ${metric("待我处理", cases.filter((item) => item.is_my_task).length, "pending", mineOnly)}
      ${metric("审批中", cases.filter((item) => approvalStatuses.has(item.status)).length, "approval")}
      ${metric("已完成", cases.filter((item) => ["approved","approved_by_exception","approved_after_manual_review","completed"].includes(item.status)).length, "done")}
      ${metric("已退回", cases.filter((item) => ["blocked","rejected"].includes(item.status)).length, "returned")}
    </section>
    <section class="panel toolbar">
      <input id="caseSearch" placeholder="搜索客户名称或案件号">
      <select id="businessFilter"><option value="">全部业务</option><option value="TKP">TKP</option><option value="TKM">TKM</option></select>
      <select id="statusFilter">
        <option value="">全部状态</option>
        <option value="pending">待我处理</option>
        <option value="approval">审批中</option>
        <option value="credit_pending_approval">等待信审审批</option>
        <option value="credit_supplement_required">等待补充信审资料</option>
        <option value="awaiting_contract">等待上传合同</option>
        <option value="blocked">等待修改合同</option>
        <option value="pending_special_approval">等待管理层审批</option>
        <option value="pending_manual_review">等待财务法务复核</option>
        <option value="done">已完成</option>
      </select>
      <button id="clearFilters" class="secondary">清除筛选</button>
    </section>
    <section class="panel">
      <div class="table-scroll">
        <table>
          <thead><tr><th>案件号</th><th>客户</th><th>业务</th><th>当前状态</th><th>风险等级</th><th>更新时间</th><th>操作</th></tr></thead>
          <tbody id="caseRows"></tbody>
        </table>
      </div>
      <div id="emptyCases" class="empty-state hidden">
        <h2>暂无案件</h2>
        <p>创建案件后，信用审核和合同审核结果会显示在这里。</p>
        <a href="/cases/new" data-link class="primary">发起信审</a>
      </div>
    </section>
  `

  const search = root.querySelector("#caseSearch")
  const business = root.querySelector("#businessFilter")
  const status = root.querySelector("#statusFilter")
  if (mineOnly) status.value = "pending"

  const renderRows = () => {
    const query = search.value.trim().toLowerCase()
    const filtered = cases.filter((item) => {
      if (mineOnly && !item.is_my_task) return false
      if (query && !`${item.case_id} ${item.customer_name}`.toLowerCase().includes(query)) return false
      if (business.value && item.business_type !== business.value) return false
      if (status.value === "pending" && !item.is_my_task) return false
      if (status.value === "approval" && !approvalStatuses.has(item.status)) return false
      if (status.value === "done" && !["approved","approved_by_exception","approved_after_manual_review","completed"].includes(item.status)) return false
      if (status.value && !["pending","approval","done"].includes(status.value) && item.status !== status.value) return false
      return true
    })
    const rows = root.querySelector("#caseRows")
    const empty = root.querySelector("#emptyCases")
    rows.innerHTML = filtered.map(caseRow).join("")
    empty.classList.toggle("hidden", filtered.length > 0)
    root.querySelector(".table-scroll").classList.toggle("hidden", filtered.length === 0)
  }

  ;[search,business,status].forEach((element) => element.addEventListener("input", renderRows))
  root.querySelector("#clearFilters").addEventListener("click", () => {
    search.value = ""; business.value = ""; status.value = mineOnly ? "pending" : ""; renderRows()
  })
  root.querySelector(".metric-grid").addEventListener("click", (event) => {
    const card = event.target.closest("[data-metric]")
    if (!card) return
    const filter = card.dataset.metric
    if (filter === "returned") status.value = "blocked"
    else status.value = filter
    search.value = ""
    root.querySelectorAll(".metric-card").forEach((item) => item.classList.toggle("active", item === card))
    renderRows()
  })
  renderRows()
}

function metric(label, value, filter, active = false) {
  return `<button class="metric-card ${active ? "active" : ""}" data-metric="${escapeHtml(filter)}"><span>${label}</span><strong>${value}</strong></button>`
}

function caseRow(item) {
  const action = item.next_action
  return `
    <tr>
      <td><a class="case-id" href="/cases/${encodeURIComponent(item.case_id)}" data-link>${escapeHtml(item.case_id)}</a></td>
      <td><a class="case-name" href="/cases/${encodeURIComponent(item.case_id)}" data-link>${escapeHtml(item.customer_name || "未命名客户")}</a><small>${customerType(item.customer_type)} · ${escapeHtml(item.owner?.display_name || "未分配")}</small></td>
      <td>${escapeHtml(item.business_type || "—")}</td>
      <td><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span></td>
      <td><span class="badge ${escapeHtml(item.risk_level || "")}">${escapeHtml(item.risk_label)}</span></td>
      <td>${dateTime(item.updated_at)}</td>
      <td>${action
        ? `<a class="primary small" href="/cases/${encodeURIComponent(item.case_id)}/action" data-link>${escapeHtml(action.label)}</a>`
        : `<a class="text-button" href="/cases/${encodeURIComponent(item.case_id)}" data-link>查看</a>`}</td>
    </tr>`
}
