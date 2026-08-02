import {api} from "../api.js?v=20260802-auth-simplified"
import {escapeHtml} from "../format.js?v=20260802-auth-simplified"
import {navigate} from "../router.js?v=20260802-auth-simplified"

const rangeOptions = [[7,"近7天"],[30,"近30天"],[90,"近90天"],[0,"全部"]]

export async function renderAnalyticsPage(root, route) {
  const requested = Number(route.params.get("days") ?? 30)
  const days = [0,7,30,90].includes(requested) ? requested : 30
  const data = await api.getAnalytics(days)
  const m = data.metrics
  root.innerHTML = `
    <header class="page-header"><div><h1>管理分析</h1><p>量化案件办结效率、节点SLA达标率和责任队列积压</p></div>
      <div class="header-actions report-actions">
        <select id="analyticsRange" aria-label="统计范围">${rangeOptions.map(([value,label]) => `<option value="${value}" ${value === days ? "selected" : ""}>${label}</option>`).join("")}</select>
        <a class="secondary" href="${api.analyticsExportUrl(days,"csv")}">下载 CSV</a>
        <a class="primary" href="${api.analyticsExportUrl(days,"xlsx")}">下载 Excel</a>
      </div>
    </header>
    <section class="metric-grid analytics-metrics">
      ${metric("发起案件", m.case_total, "")}
      ${metric("案件办结率", percent(m.completion_rate), "")}
      ${metric("平均办结时长", hours(m.avg_cycle_hours), "")}
      ${metric("节点SLA达标率", percent(m.sla_on_time_rate), rateClass(m.sla_on_time_rate))}
      ${metric("当前逾期", m.current_overdue, m.current_overdue ? "overdue" : "on_track")}
    </section>
    <section class="panel analytics-trend-panel">
      <div class="section-heading"><div><h3>案件趋势</h3><span>${escapeHtml(data.range_label)} · 新增与办结</span></div></div>
      ${trendChart(data.trend)}
    </section>
    <section class="analytics-layout">
      <section class="panel"><div class="section-heading"><div><h3>节点表现</h3><span>历史处理时长与当前积压</span></div></div>
        <div class="table-scroll"><table><thead><tr><th>节点</th><th>目标</th><th>已完成</th><th>达标率</th><th>平均/P95</th><th>当前积压</th><th>逾期</th></tr></thead><tbody>
          ${data.nodes.map(nodeRow).join("")}
        </tbody></table></div>
      </section>
      <aside class="panel backlog-panel"><div class="section-heading"><div><h3>责任队列</h3><span>当前待办分布</span></div></div>
        ${data.backlog.length ? data.backlog.map(backlogRow).join("") : `<div class="empty-note">当前没有进行中的待办。</div>`}
      </aside>
    </section>
    <footer class="report-footnote">统计生成于 ${escapeHtml(data.generated_at.replace("T"," ").slice(0,19))} · SLA政策 ${escapeHtml(data.policy_version || "—")} · 仅使用案件Trace与状态数据，不包含合同正文。</footer>`

  root.querySelector("#analyticsRange").addEventListener("change", (event) => navigate(`/analytics?days=${encodeURIComponent(event.target.value)}`))
}

function metric(label, value, tone) {
  return `<article class="metric-card report-metric ${escapeHtml(tone)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "—")}</strong></article>`
}

function percent(value) {
  return value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`
}

function hours(value) {
  return value == null ? "—" : `${Number(value).toFixed(1)} 小时`
}

function rateClass(value) {
  return value == null ? "" : value >= .9 ? "on_track" : value >= .75 ? "due_soon" : "overdue"
}

function nodeRow(item) {
  return `<tr><td><b>${escapeHtml(item.label)}</b></td><td>${Number(item.target_hours).toFixed(0)} 小时</td><td>${item.completed}</td><td>${percent(item.attainment_rate)}</td><td>${hours(item.avg_hours)}<small>P95 ${hours(item.p95_hours)}</small></td><td>${item.open}</td><td><span class="badge ${item.overdue ? "sla-overdue" : "sla-on_track"}">${item.overdue}</span></td></tr>`
}

function backlogRow(item) {
  return `<div class="bottleneck-row"><div><b>${escapeHtml(item.label)}</b><small>${item.total} 个待办</small></div><span class="${item.overdue ? "urgent" : ""}">${item.overdue ? `${item.overdue} 个逾期` : "无逾期"}</span></div>`
}

function trendChart(rows) {
  const relevant = rows.filter((row) => row.created || row.completed)
  const source = relevant.length ? relevant : rows.slice(-7)
  if (!source.length) return `<div class="empty-note">当前范围没有趋势数据。</div>`
  const max = Math.max(1, ...source.flatMap((row) => [row.created,row.completed]))
  return `<div class="trend-chart" role="img" aria-label="案件新增和办结趋势">
    ${source.map((row) => `<div class="trend-column"><div class="trend-bars"><i class="created" style="height:${Math.max(row.created / max * 100, row.created ? 8 : 0)}%" title="新增 ${row.created}"></i><i class="completed" style="height:${Math.max(row.completed / max * 100, row.completed ? 8 : 0)}%" title="办结 ${row.completed}"></i></div><small>${escapeHtml(row.date.slice(5))}</small></div>`).join("")}
  </div><div class="trend-legend"><span><i class="created"></i>新增</span><span><i class="completed"></i>办结</span></div>`
}
