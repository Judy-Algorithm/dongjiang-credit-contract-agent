import {api} from "../api.js?v=20260731-nav"
import {dateTime, escapeHtml} from "../format.js?v=20260731-nav"

export async function renderOperationsPage(root) {
  const data = await api.getSlaDashboard()
  root.innerHTML = `
    <header class="page-header"><div><h1>时效运营</h1><p>查看待办期限、临期提醒和流程积压节点</p></div><button id="runSlaSweep" class="secondary">立即扫描提醒</button></header>
    <section class="metric-grid sla-metrics">
      ${metric("待办总数", data.metrics.total, "")}
      ${metric("已逾期", data.metrics.overdue, "overdue")}
      ${metric("即将到期", data.metrics.due_soon, "due_soon")}
      ${metric("时效正常", data.metrics.on_track, "on_track")}
    </section>
    <section class="operations-layout">
      <section class="panel"><div class="section-heading operations-heading"><h3>待办时效明细</h3><select id="slaFilter"><option value="">全部状态</option><option value="overdue">已逾期</option><option value="due_soon">即将到期</option><option value="on_track">时效正常</option></select></div>
        <div class="table-scroll"><table><thead><tr><th>案件</th><th>当前节点</th><th>负责人</th><th>进入时间</th><th>截止时间</th><th>时效</th><th>操作</th></tr></thead><tbody id="slaRows"></tbody></table></div>
        <div id="emptySla" class="empty-state hidden"><h2>没有匹配的待办</h2><p>当前筛选条件下没有进行中的流程任务。</p></div>
      </section>
      <aside class="panel bottleneck-panel"><div class="section-heading"><h3>节点积压</h3><span>按待办节点汇总</span></div>${data.by_task.length ? data.by_task.map(bottleneck).join("") : `<div class="empty-note">当前没有进行中的待办。</div>`}</aside>
    </section>`

  const filter = root.querySelector("#slaFilter")
  const renderRows = () => {
    const items = data.items.filter((item) => !filter.value || item.sla.state === filter.value)
    root.querySelector("#slaRows").innerHTML = items.map(row).join("")
    root.querySelector(".table-scroll").classList.toggle("hidden", !items.length)
    root.querySelector("#emptySla").classList.toggle("hidden", Boolean(items.length))
  }
  filter.addEventListener("input", renderRows)
  root.querySelector(".sla-metrics").addEventListener("click", (event) => {
    const card = event.target.closest("[data-sla-filter]")
    if (!card) return
    filter.value = card.dataset.slaFilter
    root.querySelectorAll(".sla-metrics .metric-card").forEach((item) => item.classList.toggle("active", item === card))
    renderRows()
  })
  root.querySelector("#runSlaSweep").addEventListener("click", async (event) => {
    event.currentTarget.disabled = true
    try {
      const result = await api.runSlaSweep()
      window.dispatchEvent(new CustomEvent("app:toast", {detail:`扫描完成，新增 ${result.notifications_created} 条提醒`}))
      window.dispatchEvent(new Event("app:navigate"))
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      event.currentTarget.disabled = false
    }
  })
  renderRows()
}

function metric(label, value, filter) {
  return `<button class="metric-card" data-sla-filter="${escapeHtml(filter)}"><span>${escapeHtml(label)}</span><strong>${Number(value || 0)}</strong></button>`
}

function row(item) {
  return `<tr><td><a class="case-name" href="/cases/${encodeURIComponent(item.case_id)}" data-link>${escapeHtml(item.customer_name || "未命名客户")}</a><small>${escapeHtml(item.case_id)} · ${escapeHtml(item.business_type)}</small></td><td>${escapeHtml(item.sla.task_label)}</td><td>${escapeHtml(item.owner?.display_name || "按角色分配")}</td><td>${dateTime(item.sla.entered_at)}</td><td>${dateTime(item.sla.due_at)}</td><td>${slaBadge(item.sla)}</td><td><a class="primary small" href="/cases/${encodeURIComponent(item.case_id)}/action" data-link>处理</a></td></tr>`
}

function slaBadge(sla) {
  const timing = sla.state === "overdue" ? `逾期 ${Math.abs(Number(sla.remaining_hours)).toFixed(1)} 小时` : sla.state === "due_soon" ? `剩余 ${Number(sla.remaining_hours).toFixed(1)} 小时` : `剩余 ${Number(sla.remaining_hours).toFixed(1)} 小时`
  return `<span class="badge sla-${escapeHtml(sla.state)}">${escapeHtml(timing)}</span>`
}

function bottleneck(item) {
  const urgent = Number(item.overdue) + Number(item.due_soon)
  return `<div class="bottleneck-row"><div><b>${escapeHtml(item.label)}</b><small>${item.total} 个待办</small></div><span class="${urgent ? "urgent" : ""}">${urgent ? `${urgent} 个需关注` : "全部正常"}</span></div>`
}
