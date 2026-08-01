import {api} from "../api.js?v=20260801-docai1"
import {escapeHtml} from "../format.js?v=20260801-docai1"

const metricDefinitions = [
  ["信用结论", "credit_conclusion_rate"],
  ["合同决策", "contract_decision_rate"],
  ["审批路由", "route_accuracy"],
  ["规则召回", "rule_recall"],
  ["误报保护", "false_positive_guard_rate"],
  ["证据定位", "evidence_location_rate"],
  ["Agent 一致", "agent_conformance_rate"],
  ["隐私检查", "privacy_pass_rate"],
  ["重复确定性", "determinism_rate"],
]

const tagLabels = {
  credit: "信用",
  contract: "合同",
  supplement: "补件",
  pass: "通过",
  "high-risk": "高风险",
  rating: "评级",
  "manual-review": "人工复核",
  "special-approval": "特批",
  "credit-control": "额度控制",
  completeness: "完整性",
  "hard-stop": "底线阻断",
  revision: "合同修订",
  privacy: "隐私",
}

export async function renderBenchmarkPage(root) {
  const data = await api.getBenchmark()
  renderPage(root, data)
}

function renderPage(root, data) {
  const suite = data.suite || {}
  const report = data.latest || null
  root.innerHTML = `
    <header class="page-header benchmark-header">
      <div><h1>质量评测</h1><p>用版本化合成案例验证信审、合同评审与动态工作流</p></div>
      <div class="header-actions benchmark-actions">
        <select id="benchmarkRepeats" aria-label="重复执行次数">
          <option value="1">执行 1 次</option>
          <option value="2" selected>执行 2 次</option>
          <option value="3">执行 3 次</option>
        </select>
        <button id="runBenchmark" class="primary" type="button">运行评测</button>
        ${report ? `<a class="secondary" href="${api.benchmarkExportUrl("csv")}">CSV</a><a class="secondary" href="${api.benchmarkExportUrl("xlsx")}">Excel</a>` : ""}
      </div>
    </header>
    ${suiteSummary(suite)}
    <div id="benchmarkRunState" class="benchmark-run-state hidden" role="status"></div>
    ${report ? reportView(report) : emptyView(suite)}
    <footer class="benchmark-disclaimer"><b>评测边界</b><span>${escapeHtml(report?.disclaimer || "本基准集只用于规则回归和流程验证，不代表真实业务泛化准确率或东江正式信用政策。")}</span></footer>`

  root.querySelector("#runBenchmark").addEventListener("click", () => runBenchmark(root, data))
}

async function runBenchmark(root, data) {
  const button = root.querySelector("#runBenchmark")
  const repeats = Number(root.querySelector("#benchmarkRepeats").value)
  const state = root.querySelector("#benchmarkRunState")
  const caseCount = Number((data.suite || {}).case_count || 0)
  button.disabled = true
  button.textContent = "评测运行中"
  root.querySelector("#benchmarkRepeats").disabled = true
  state.classList.remove("hidden")
  state.innerHTML = `<span class="benchmark-spinner" aria-hidden="true"></span><div><b>正在执行 ${caseCount * repeats} 次隔离案例运行</b><small>外部 AI 与 OA、CRM、SAP 集成保持关闭</small></div>`
  try {
    const result = await api.runBenchmark(repeats)
    window.dispatchEvent(new CustomEvent("app:toast", {detail:`质量评测完成：${result.report.metrics.case_passed}/${result.report.metrics.case_total} 个案例通过`}))
    renderPage(root, {...data, status:"completed", latest:result.report})
  } catch (reason) {
    state.innerHTML = `<div><b>评测未完成</b><small>${escapeHtml(reason.message || String(reason))}</small></div>`
    button.disabled = false
    button.textContent = "重新运行"
    root.querySelector("#benchmarkRepeats").disabled = false
  }
}

function suiteSummary(suite) {
  const tags = Object.entries(suite.tag_counts || {})
  return `<section class="panel benchmark-suite">
    <div class="benchmark-suite-main">
      <div><span class="benchmark-kicker">合成基准集</span><h2>${escapeHtml(suite.title || "质量评测基准")}</h2><p>${escapeHtml(suite.source || "—")}</p></div>
      <dl><div><dt>版本</dt><dd>v${escapeHtml(suite.version || "—")}</dd></div><div><dt>案例数</dt><dd>${escapeHtml(suite.case_count ?? 0)}</dd></div><div><dt>政策状态</dt><dd>${escapeHtml(policyLabel(suite.policy_status))}</dd></div></dl>
    </div>
    <div class="benchmark-coverage" aria-label="案例覆盖标签">${tags.map(([tag,count]) => `<span>${escapeHtml(tagLabels[tag] || tag)} <b>${count}</b></span>`).join("")}</div>
  </section>`
}

function emptyView(suite) {
  return `<section class="panel benchmark-empty"><div><span class="benchmark-empty-mark">01</span><h2>尚无评测报告</h2><p>当前版本包含 ${escapeHtml(suite.case_count ?? 0)} 个完全合成案例。首次运行后将在这里显示多维指标和逐案例检查结果。</p></div></section>`
}

function reportView(report) {
  const metrics = report.metrics || {}
  const passed = report.status === "passed"
  return `<section class="benchmark-result-head ${passed ? "passed" : "failed"}">
      <div><span class="benchmark-kicker">最近一次运行</span><div class="benchmark-result-title"><h2>${passed ? "全部通过" : "发现回归失败"}</h2><span class="badge ${passed ? "approved" : "blocked"}">${passed ? "PASSED" : "FAILED"}</span></div><p>${escapeHtml(report.run_id || "—")} · ${escapeHtml(formatDateTime(report.completed_at))} · 重复 ${escapeHtml(report.repeat_count || 1)} 次</p></div>
      <dl class="benchmark-runtime"><div><dt>运行空间</dt><dd>临时隔离</dd></div><div><dt>外部 AI</dt><dd>关闭</dd></div><div><dt>企业集成</dt><dd>关闭</dd></div><div><dt>计划版本</dt><dd>${escapeHtml((report.runtime || {}).workflow_plan_version || "—")}</dd></div></dl>
    </section>
    <section class="metric-grid benchmark-primary-metrics">
      ${summaryMetric("案例通过", `${metrics.case_passed ?? 0}/${metrics.case_total ?? 0}`, passed ? "on_track" : "overdue")}
      ${summaryMetric("检查通过", `${metrics.check_passed ?? 0}/${metrics.check_total ?? 0}`, metrics.regression_pass_rate === 1 ? "on_track" : "overdue")}
      ${summaryMetric("平均耗时", latency(metrics.average_latency_ms), "")}
      ${summaryMetric("P95 耗时", latency(metrics.p95_latency_ms), "")}
    </section>
    <section class="panel benchmark-dimensions">
      <div class="section-heading"><div><h3>维度指标</h3><span>每项均由明确期望与实际输出比对</span></div><b>${percent(metrics.regression_pass_rate)} 总检查通过率</b></div>
      <div class="benchmark-dimension-grid">${metricDefinitions.map(([label,key]) => dimensionMetric(label, metrics[key])).join("")}</div>
    </section>
    <section class="panel benchmark-cases">
      <div class="section-heading"><div><h3>案例结果</h3><span>${escapeHtml((report.suite || {}).title || "合成基准集")} v${escapeHtml((report.suite || {}).version || "—")}</span></div><span>${metrics.case_passed ?? 0} / ${metrics.case_total ?? 0} 通过</span></div>
      <div class="table-scroll"><table><thead><tr><th>案例</th><th>覆盖</th><th>结果</th><th>检查</th><th>平均耗时</th><th>检查详情</th></tr></thead><tbody>${(report.cases || []).map(caseRow).join("")}</tbody></table></div>
    </section>`
}

function summaryMetric(label, value, tone) {
  return `<article class="metric-card report-metric ${escapeHtml(tone)}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`
}

function dimensionMetric(label, value) {
  const tone = value === 1 ? "passed" : value == null ? "unknown" : "failed"
  return `<div class="benchmark-dimension ${tone}"><span>${escapeHtml(label)}</span><strong>${percent(value)}</strong><i aria-hidden="true"><b style="width:${value == null ? 0 : Math.max(0, Math.min(100, Number(value) * 100))}%"></b></i></div>`
}

function caseRow(item) {
  const checks = item.checks || []
  const passedChecks = checks.filter((check) => check.passed).length
  return `<tr class="benchmark-case-row ${item.passed ? "passed" : "failed"}">
    <td><b>${escapeHtml(item.name || item.case_key)}</b><small>${escapeHtml(item.case_key || "—")}</small></td>
    <td><div class="benchmark-tags">${(item.tags || []).map((tag) => `<span>${escapeHtml(tagLabels[tag] || tag)}</span>`).join("")}</div></td>
    <td><span class="badge ${item.passed ? "approved" : "blocked"}">${item.passed ? "通过" : "失败"}</span></td>
    <td>${passedChecks}/${checks.length}<small>${item.repeat_count || 1} 次结果${checks.some((check) => check.metric === "determinism" && !check.passed) ? "不一致" : "一致"}</small></td>
    <td>${latency(item.duration_ms)}</td>
    <td>${checkDetails(checks, item.passed)}</td>
  </tr>`
}

function checkDetails(checks, casePassed) {
  const ordered = [...checks].sort((left, right) => Number(left.passed) - Number(right.passed))
  return `<details class="benchmark-checks"><summary>${casePassed ? "查看检查" : `查看 ${checks.filter((item) => !item.passed).length} 项失败`}</summary><div>${ordered.map((check) => `<article class="${check.passed ? "passed" : "failed"}"><header><b>${escapeHtml(check.label || "检查项")}</b><span>${check.passed ? "通过" : "失败"}</span></header><dl><div><dt>期望</dt><dd>${escapeHtml(displayValue(check.expected))}</dd></div><div><dt>实际</dt><dd>${escapeHtml(displayValue(check.actual))}</dd></div></dl></article>`).join("")}</div></details>`
}

function percent(value) {
  return value == null ? "—" : `${(Number(value) * 100).toFixed(1)}%`
}

function latency(value) {
  return value == null ? "—" : `${Number(value).toFixed(0)} ms`
}

function displayValue(value) {
  if (value == null) return "空"
  if (typeof value === "boolean") return value ? "是" : "否"
  if (typeof value === "object") return JSON.stringify(value)
  return String(value)
}

function formatDateTime(value) {
  if (!value) return "—"
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? String(value).replace("T", " ").slice(0,19) : date.toLocaleString("zh-CN", {hour12:false})
}

function policyLabel(value) {
  return value === "PROPOSED_NOT_OFFICIAL" ? "提议版（非正式政策）" : value || "—"
}
