import {api} from "../api.js?v=20260801-docai1"
import {customerType, dateTime, escapeHtml, money, statusClass} from "../format.js?v=20260801-docai1"

const tabs = [
  ["overview","概览"],["credit","信用评估"],["contract","合同审查"],
  ["agents","Agent运行"],["approval","审批记录"],["documents","原始资料"],["writeback","系统回写"],
]

export async function renderCaseDetailPage(root, route) {
  const {case:item} = await api.getCase(route.caseId)
  const customer = item.customer || {}
  const model = item.credit?.model_result || {}
  const approved = item.credit?.approved_result
  root.innerHTML = `
    <header class="page-header case-header">
      <div><div class="detail-title"><h1>${escapeHtml(customer.customer_name || "未命名客户")}</h1><span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span></div>
      <p>${escapeHtml(item.case_id)} · ${customerType(customer.customer_type)} · ${escapeHtml(customer.business_type || "—")} · 负责人 ${escapeHtml(item.owner?.display_name || "未分配")}</p></div>
      <div class="header-actions">
        ${item.permissions?.can_assign_owner ? `<button id="assignOwner" class="secondary">分配负责人</button>` : ""}
        <a href="/cases" data-link class="secondary">返回列表</a>
        ${item.next_action ? `<a href="/cases/${encodeURIComponent(item.case_id)}/action" data-link class="primary">${escapeHtml(item.next_action.label)}</a>` : ""}
      </div>
    </header>
    ${item.next_action ? `<section class="action-banner"><div><h2>${escapeHtml(item.status_label)}</h2><p>${actionMessage(item.next_action.type)}</p>${slaLine(item.sla)}</div><a href="/cases/${encodeURIComponent(item.case_id)}/action" data-link class="primary">${escapeHtml(item.next_action.label)}</a></section>` : ""}
    <section class="summary-grid">
      ${summary("模型信用分", model.score == null ? "—" : Number(model.score).toFixed(1))}
      ${summary("风险等级", item.risk_label)}
      ${summary(approved ? "正式授信额度" : "建议授信额度", money(approved?.credit_limit ?? model.credit_limit))}
      ${summary(approved ? "正式账期" : "建议账期", (approved?.term_days ?? model.term_days) == null ? "—" : `${approved?.term_days ?? model.term_days} 天`)}
    </section>
    <section class="panel case-workspace">
      <div class="tabs case-tabs">${tabs.map(([key,label], index) => `<button class="tab ${index === 0 ? "active" : ""}" data-tab="${key}">${label}</button>`).join("")}</div>
      <div id="tabBody" class="tab-body"></div>
    </section>`

  const renderTab = (tab) => {
    root.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab))
    root.querySelector("#tabBody").innerHTML = ({
      overview:overviewTab(item), credit:creditTab(item), contract:contractTab(item),
      agents:agentExecutionTab(item), approval:approvalTab(item), documents:documentsTab(item), writeback:writebackTab(item),
    })[tab]
    if (tab === "contract") {
      installEvidenceViewer(root, item)
      installTranslationActions(root, item, renderTab)
    }
    if (tab === "documents") installDocumentViewer(root, item)
    if (tab === "writeback") installWritebackRetry(root, item, renderTab)
    if (tab === "agents") {
      installCandidateActions(root, item, renderTab)
      installStructuredExtractionActions(root, item, renderTab)
    }
  }
  root.querySelector(".tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]")
    if (button) renderTab(button.dataset.tab)
  })
  root.querySelector("#assignOwner")?.addEventListener("click", async () => {
    const data = await api.listUsers()
    const sales = data.users.filter((user) => user.active && user.roles.includes("sales"))
    if (!sales.length) return window.dispatchEvent(new CustomEvent("app:toast", {detail:"请先创建销售用户"}))
    showOwnerDialog(root, item, sales)
  })
  const requestedTab = route.params.get("tab")
  renderTab(tabs.some(([key]) => key === requestedTab) ? requestedTab : "overview")
}

function overviewTab(item) {
  const customer = item.customer || {}
  const pending = item.pending_action
  return `<div class="overview-layout">
    <section><div class="section-heading"><h3>当前结论</h3><span>${escapeHtml(item.status_label)}</span></div>
      <div class="info-grid compact">
        ${info("当前阶段", item.phase === "contract" ? "合同审查" : "信用评估")}
        ${info("下一步", pending?.label || "流程已结束")}
        ${info("申请人", item.applicant?.display_name || "—")}
        ${info("负责人", item.owner?.display_name || "—")}
        ${info("项目或产品", customer.project_name || "—")}
        ${info("业务类型", customer.business_type || "—")}
      </div>
      ${item.findings?.length ? `<div class="form-section"><div class="section-heading"><h3>风险摘要</h3><span>${item.findings.length} 项</span></div>${item.findings.slice(0,3).map(findingCard).join("")}</div>` : `<div class="empty-note">当前没有合同风险事项。</div>`}
    </section>
    <aside class="case-rail"><div class="section-heading"><h3>案件信息</h3></div>
      ${rail("案件号", item.case_id)}${rail("创建时间", dateTime(item.created_at))}${rail("最近更新", dateTime(item.updated_at))}${item.sla?.due_at ? rail("当前待办截止", dateTime(item.sla.due_at)) : ""}${rail("原始资料", `${item.source_documents?.length || 0} 份`)}${rail("审批证据", `${item.approval_evidence?.length || 0} 份`)}
    </aside>
  </div>`
}

function slaLine(sla) {
  if (!sla || sla.state === "not_applicable") return ""
  const hours = Number(sla.remaining_hours || 0)
  const timing = sla.state === "overdue" ? `已逾期 ${Math.abs(hours).toFixed(1)} 小时` : `距离截止还有 ${hours.toFixed(1)} 小时`
  return `<small class="sla-line ${escapeHtml(sla.state)}">${escapeHtml(timing)} · 截止 ${dateTime(sla.due_at)}</small>`
}

function creditTab(item) {
  const credit = item.credit || {}, model = credit.model_result || {}, approved = credit.approved_result
  const selected = credit.rating_resolution?.selected
  const control = Object.keys(item.credit_control || {}).length ? item.credit_control : (approved || model)
  return `<div class="section-heading"><h3>模型建议</h3><span>${credit.data_coverage_ratio == null ? "资料覆盖率 —" : `资料覆盖率 ${Math.round(Number(credit.data_coverage_ratio) * 100)}%`}</span></div>
    <div class="info-grid">${info("信用分", model.score == null ? "—" : Number(model.score).toFixed(1))}${info("风险等级", model.risk_label)}${info("建议额度", money(model.credit_limit))}${info("建议账期", model.term_days == null ? "—" : `${model.term_days} 天`)}${info("采用评级", selected ? `${selected.agency || ""} ${selected.rating || ""}`.trim() : "未取得有效评级")}${info("最长账期", model.hard_term_limit_days == null ? "—" : `${model.hard_term_limit_days} 天`)}</div>
    <div class="form-section"><div class="section-heading"><h3>正式授信与占用</h3><span>${approved ? "已生效" : "等待审批"}</span></div>
      <div class="info-grid">${info("正式额度", money(approved?.credit_limit))}${info("正式账期", approved?.term_days == null ? "—" : `${approved.term_days} 天`)}${info("当前已占用", money(control.occupied_credit_amount))}${info("当前可用", money(control.available_credit_amount))}${info("有效期至", approved?.expires_at ? approved.expires_at.replace("T"," ").slice(0,10) : "—")}${info("信用控制", control.credit_locked ? "已锁定" : "正常")}</div>
      ${control.credit_lock_reasons?.length ? notice("锁定原因", control.credit_lock_reasons) : ""}
    </div>
    ${credit.requires_supplement ? notice("需要补充信用资料", credit.supplement_reasons || []) : ""}
    <div class="form-section"><div class="section-heading"><h3>评估依据</h3><span>缺失值不按0计算</span></div>
      ${credit.reasons?.length ? `<ul class="reason-list">${credit.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>` : `<div class="empty-note">暂无评估说明。</div>`}
      ${credit.missing_fields?.length ? `<div class="file-list">${credit.missing_fields.map((field) => `<span class="file-chip">缺少 ${escapeHtml(field.label)}</span>`).join("")}</div>` : ""}
    </div>`
}

function contractTab(item) {
  if (!item.credit?.effective) return emptyState("合同上传尚未开放", "信用审批生效后才能进入合同审查。")
  if (!item.contracts?.length) return emptyState("尚未提交合同", "销售上传合同后，风险与原文证据会显示在这里。")
  return `<div class="contract-review-layout">
    <section class="finding-pane"><div class="section-heading"><h3>风险事项</h3><span>${item.findings.length} 项</span></div>
      ${item.findings.length ? item.findings.map((finding, index) => findingCard(finding, index)).join("") : `<div class="empty-note">未发现需要处理的合同风险。</div>`}
    </section>
    <aside id="evidenceViewer" class="evidence-viewer"><div class="evidence-placeholder"><strong>原文证据</strong><p>点击风险事项中的“查看原文”，这里会显示对应页码、段落或单元格。</p></div></aside>
  </div>${aiAssistance(item)}${revisionArchive(item)}${translationWorkbench(item)}`
}

function translationWorkbench(item) {
  const contracts = (item.source_documents || []).filter((doc) => doc.document_kind === "contract" && doc.parse_status === "parsed" && doc.fragment_count)
  const translations = item.contract_translations || []
  if (!contracts.length) return ""
  const canManage = item.translation_permissions?.can_manage
  return `<section class="translation-workbench form-section">
    <div class="section-heading"><div><h3>多语言合同</h3><span>译文逐段绑定原文，人工确认后才可导出</span></div><span>${translations.length} 个版本</span></div>
    ${canManage ? `<form id="translationForm" class="translation-toolbar">
      <label>合同<select id="translationDocument">${contracts.map((doc) => `<option value="${escapeHtml(doc.document_id)}">${escapeHtml(doc.name)}</option>`).join("")}</select></label>
      <label>目标语言<select id="translationLanguage"><option value="en">英文</option><option value="zh">中文</option><option value="vi">越南语</option><option value="ja">日语</option><option value="es">西班牙语</option></select></label>
      <button type="submit" class="secondary">生成对齐译稿</button>
    </form>` : ""}
    ${translations.length ? `<div class="translation-list">${translations.map((translation) => translationRow(item, translation, canManage)).join("")}</div>` : `<div class="empty-note">尚未生成合同译稿。</div>`}
  </section>`
}

function translationRow(item, translation, canManage) {
  const confirmed = translation.status === "confirmed"
  return `<article class="translation-version"><div><b>${escapeHtml(translation.target_language_label)} · ${escapeHtml(translation.source_name)}</b><small>${escapeHtml(translation.translation_id)} · ${translation.segment_count || 0} 个对齐片段 · ${confirmed ? `已由 ${escapeHtml(translation.confirmed_by?.display_name || "人工")} 确认` : "等待人工复核"}</small></div><div class="revision-actions">${confirmed ? `<a class="secondary" href="${api.translationDownloadUrl(item.case_id, translation.translation_id)}">下载双语 Word</a>` : canManage ? `<button type="button" class="primary" data-translation-review="${escapeHtml(translation.translation_id)}">复核译稿</button>` : `<span class="badge pending">待确认</span>`}</div></article>`
}

function installTranslationActions(root, item, renderTab) {
  root.querySelector("#translationForm")?.addEventListener("submit", async (event) => {
    event.preventDefault()
    const button = event.submitter
    button.disabled = true
    button.textContent = "正在翻译..."
    try {
      const data = await api.createContractTranslation(item.case_id, {
        document_id:root.querySelector("#translationDocument").value,
        target_language:root.querySelector("#translationLanguage").value,
      })
      item.contract_translations = [data.translation, ...(item.contract_translations || [])]
      showTranslationDialog(root, item, data.translation, renderTab)
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
      button.textContent = "生成对齐译稿"
    }
  })
  root.querySelector(".translation-list")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-translation-review]")
    if (!button) return
    button.disabled = true
    try {
      const data = await api.getContractTranslation(item.case_id, button.dataset.translationReview)
      showTranslationDialog(root, item, data.translation, renderTab)
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
    }
  })
}

function showTranslationDialog(root, item, translation, renderTab) {
  root.querySelector("#translationDialog")?.remove()
  root.insertAdjacentHTML("beforeend", `<div id="translationDialog" class="modal-backdrop"><form class="modal-panel translation-dialog"><div class="section-heading"><div><h3>${escapeHtml(translation.target_language_label)}译稿复核</h3><span>${translation.entries.length} 个原文对齐片段</span></div><button type="button" class="text-button" data-close>关闭</button></div><p class="field-hint">逐段核对并修正译文。确认后生成双语 Word，原合同不会被覆盖。</p><div class="translation-segments">${translation.entries.map((entry) => `<article data-translation-fragment="${escapeHtml(entry.fragment_id)}"><small>${escapeHtml(entry.location_label)}</small><pre>${escapeHtml(entry.source_text)}</pre><label>译文<textarea rows="4" required>${escapeHtml(entry.translated_text)}</textarea></label></article>`).join("")}</div><label>人工复核说明<textarea id="translationReviewNote" rows="3" required placeholder="填写复核范围、依据或需要继续确认的事项"></textarea></label><div id="translationError" class="error-box hidden"></div><div class="form-actions"><span>机器翻译仅作辅助，法律效力以签署原文为准。</span><button type="submit" class="primary">确认并生成双语稿</button></div></form></div>`)
  const dialog = root.querySelector("#translationDialog")
  dialog.querySelector("[data-close]").addEventListener("click", () => { dialog.remove(); renderTab("contract") })
  dialog.addEventListener("click", (event) => { if (event.target === dialog) { dialog.remove(); renderTab("contract") } })
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    const button = event.submitter
    const error = dialog.querySelector("#translationError")
    button.disabled = true
    error.classList.add("hidden")
    try {
      const data = await api.confirmContractTranslation(item.case_id, translation.translation_id, {
        review_note:dialog.querySelector("#translationReviewNote").value,
        entries:Array.from(dialog.querySelectorAll("[data-translation-fragment]")).map((row) => ({fragment_id:row.dataset.translationFragment, translated_text:row.querySelector("textarea").value})),
      })
      item.contract_translations = (item.contract_translations || []).map((entry) => entry.translation_id === data.translation.translation_id ? data.translation : entry)
      dialog.remove()
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"译稿已确认，双语 Word 已归档"}))
      renderTab("contract")
    } catch (reason) {
      error.textContent = reason.message || String(reason)
      error.classList.remove("hidden")
      button.disabled = false
    }
  })
}

function aiAssistance(item) {
  const groups = item.ai_assistance || []
  if (!groups.length) return ""
  const findings = groups.flatMap((group) => group.findings || [])
  const status = groups.some((group) => group.status === "succeeded") ? "succeeded" : groups[0].status
  const statusLabel = ({succeeded:"已完成", failed:"调用失败，已回退规则", not_configured:"未配置，当前使用规则审查", not_applicable:"不适用"})[status] || status
  return `<section class="ai-assistance form-section"><div class="section-heading"><div><h3>AI 辅助发现</h3><span class="ai-disclaimer">仅供辅助，不改变制度规则和审批结论</span></div><span class="badge ${status === "succeeded" ? "approved" : status === "failed" ? "high" : "pending"}">${escapeHtml(statusLabel)}</span></div>
    ${groups.map((group) => group.summary ? `<p class="ai-summary">${escapeHtml(group.summary)}</p>` : "").join("")}
    ${findings.length ? `<div class="ai-finding-list">${findings.map((finding, index) => aiFindingCard(finding, index)).join("")}</div>` : `<div class="empty-note">当前没有额外的 AI 辅助发现。</div>`}
  </section>`
}

function aiFindingCard(finding, index) {
  const locatable = finding.document_id && finding.fragment_id
  return `<article class="ai-finding"><div class="finding-head"><div><small>${escapeHtml(finding.finding_id || "AI")}</small><h3>${escapeHtml(finding.title)}</h3></div><div class="ai-finding-meta"><span class="badge ${escapeHtml(finding.level || "medium")}">${Math.round(Number(finding.confidence || 0) * 100)}% 置信</span>${locatable ? `<button class="text-button evidence-link" data-ai-finding-index="${index}">${escapeHtml(finding.location_label || "查看原文")}</button>` : ""}</div></div><p>${escapeHtml(finding.message)}</p><div class="finding-suggestion"><b>建议</b><span>${escapeHtml(finding.suggestion || "人工复核")}</span></div></article>`
}

function revisionArchive(item) {
  const revisions = item.contract_revisions || []
  if (!revisions.length) return ""
  return `<div class="form-section"><div class="section-heading"><h3>合同版本链</h3><span>${revisions.length} 个修订版本</span></div>
    <div class="revision-list">${revisions.map((revision) => `<article class="revision-version"><div><b>${escapeHtml(revision.revision_id)}</b><small>${escapeHtml(revision.source_name)} · ${revision.decisions?.length || 0} 项处置 · ${escapeHtml(revision.status === "submitted" ? "已重新送审" : "草稿")}</small></div><div class="revision-actions"><a class="secondary" href="${api.revisionDownloadUrl(item.case_id, revision.revision_id, "redline")}">修订稿</a><a class="secondary" href="${api.revisionDownloadUrl(item.case_id, revision.revision_id, "clean")}">清洁稿</a></div></article>`).join("")}</div>
  </div>`
}

function findingCard(finding, index = null) {
  const locatable = finding.document_id && finding.fragment_id
  return `<article class="finding ${escapeHtml(finding.level || "")}">
    <div class="finding-head"><div><small>${escapeHtml(finding.rule_id || "风险规则")}</small><h3>${escapeHtml(finding.title)}</h3></div>${locatable ? `<button class="text-button evidence-link" data-finding-index="${index}">${escapeHtml(finding.location_label || "查看原文")}</button>` : ""}</div>
    <p>${escapeHtml(finding.message)}</p><div class="finding-suggestion"><b>建议</b><span>${escapeHtml(finding.suggestion)}</span></div>
  </article>`
}

function approvalTab(item) {
  return `<div class="approval-layout"><section><div class="section-heading"><h3>处理时间线</h3><span>${item.records?.length || 0} 条</span></div>
    ${item.records?.length ? `<div class="record-list">${item.records.map((record) => `<div class="record"><b>${escapeHtml(record.label)}</b><time>${dateTime(record.time)}</time></div>`).join("")}</div>` : `<div class="empty-note">暂无处理记录。</div>`}</section>
    <section><div class="section-heading"><h3>OA 审批链</h3><span>${item.approval_chain?.length || 0} 个节点</span></div>
      ${item.approval_chain?.length ? `<div class="review-list">${item.approval_chain.map((step) => `<div class="review-row"><b>${escapeHtml(approvalStage(step.stage))}</b><span>${escapeHtml(step.status || "pending")}</span></div>`).join("")}</div>` : `<div class="empty-note">尚未形成 OA 审批链。</div>`}
      ${item.approval_evidence?.length ? `<div class="form-section"><div class="section-heading"><h3>审批证据</h3></div><div class="review-list">${item.approval_evidence.map((evidence) => `<div class="review-row"><b>${escapeHtml(evidence.name)}</b><span>${escapeHtml(evidence.actor_id || "")} · ${dateTime(evidence.archived_at)}</span></div>`).join("")}</div></div>` : ""}
    </section></div>`
}

function agentExecutionTab(item) {
  const execution = item.agent_execution || {}, plans = execution.plans || []
  const candidates = item.agent_candidates || []
  const extractions = item.structured_extractions || []
  const extractionPermissions = item.structured_extraction_permissions || {}
  const canGenerateExtraction = extractionPermissions.can_generate_credit || extractionPermissions.can_generate_contract
  if (!plans.length && !candidates.length && !extractions.length && !canGenerateExtraction) return emptyState("暂无 Agent 运行记录", "新发起或重新执行的案件会在这里显示动态任务计划。")
  return `<div class="agent-execution">
    ${structuredExtractionPanel(item)}
    ${plans.length ? `<div class="agent-parent"><div><small>父工作流</small><h3>${escapeHtml(execution.parent_label || "业务主流程")}</h3></div><span class="badge approved">受控动态编排</span></div><div class="agent-flow-connector" aria-hidden="true"></div>${plans.map(agentPlan).join("")}` : ""}
    ${candidateReviews(candidates)}
    <div class="agent-security-note">${escapeHtml(execution.security_notice || "")}</div>
  </div>`
}

function structuredExtractionPanel(item) {
  const rows = item.structured_extractions || [], permissions = item.structured_extraction_permissions || {}
  const actions = `${permissions.can_generate_credit ? `<button class="secondary" data-generate-extraction="credit">提取信用字段</button>` : ""}${permissions.can_generate_contract ? `<button class="secondary" data-generate-extraction="contract">提取合同字段</button>` : ""}`
  if (!rows.length && !actions) return ""
  return `<section class="structured-extractions"><div class="section-heading"><div><h3>AI 结构化提取</h3><span>字段候选必须绑定原文证据，人工采纳后重新执行受控 Agent</span></div><div class="header-actions">${actions}</div></div>
    ${rows.length ? `<div class="structured-extraction-list">${rows.map(structuredExtractionCard).join("")}</div>` : `<div class="empty-note">尚未生成结构化字段候选。</div>`}
  </section>`
}

function structuredExtractionCard(item) {
  const labels = {pending:"等待人工确认",adopted:"已采纳并重跑",rejected:"已拒绝",verification_failed:"独立核验失败"}
  const tone = item.status === "adopted" ? "approved" : item.status === "rejected" || item.status === "verification_failed" ? "high" : "pending"
  return `<article class="structured-extraction-card"><header><div><small>${escapeHtml(item.extraction_id)} · ${item.document_kind === "credit" ? "信用资料" : "合同"}</small><h4>${escapeHtml(item.summary || "结构化字段候选")}</h4><p>${escapeHtml(item.model || "文本模型")} · ${dateTime(item.created_at)} · 独立核验 ${escapeHtml(item.verification?.status || "—")}</p></div><span class="badge ${tone}">${escapeHtml(labels[item.status] || item.status)}</span></header>
    <div class="structured-field-list">${(item.candidates || []).map((candidate) => `<label class="structured-field-row"><input type="checkbox" value="${escapeHtml(candidate.candidate_id)}" ${item.status === "pending" ? "checked" : "disabled"}><span><b>${escapeHtml(candidate.label)}</b><small>${escapeHtml(extractionValue(candidate.value))} · ${Math.round(Number(candidate.confidence || 0) * 100)}% 置信</small></span><button type="button" class="text-button" data-extraction-evidence data-document-id="${escapeHtml(candidate.document_id)}" data-fragment-id="${escapeHtml(candidate.fragment_id)}">${escapeHtml(candidate.location_label || "查看证据")}</button></label>`).join("")}</div>
    ${item.permissions?.can_review ? `<footer><small>采纳只更新所选字段；随后重新分析、评分/审查和独立核验，不自动批准。</small><div><button class="secondary" data-extraction-action="reject" data-extraction-id="${escapeHtml(item.extraction_id)}">拒绝</button><button class="primary" data-extraction-action="adopt" data-extraction-id="${escapeHtml(item.extraction_id)}">采纳所选字段</button></div></footer>` : item.decision?.result_plan_id ? `<footer><small>新计划 ${escapeHtml(item.decision.result_plan_id)} · 采纳 ${item.decision.candidate_count || 0} 个字段</small></footer>` : ""}
  </article>`
}

function extractionValue(value) {
  if (typeof value === "boolean") return value ? "是" : "否"
  return value == null ? "—" : String(value)
}

function installStructuredExtractionActions(root, item, renderTab) {
  root.querySelector(".structured-extractions")?.addEventListener("click", async (event) => {
    const generate = event.target.closest("[data-generate-extraction]")
    if (generate) {
      generate.disabled = true
      try {
        const data = await api.generateStructuredExtraction(item.case_id, generate.dataset.generateExtraction)
        Object.assign(item, data.case)
        window.dispatchEvent(new CustomEvent("app:toast", {detail:"结构化字段候选已生成"}))
        renderTab("agents")
      } catch (reason) {
        window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
        generate.disabled = false
      }
      return
    }
    const evidence = event.target.closest("[data-extraction-evidence]")
    if (evidence) {
      root.insertAdjacentHTML("beforeend", `<div id="extractionEvidenceDialog" class="modal-backdrop"><section class="modal-panel extraction-evidence-dialog"><div class="section-heading"><h3>字段原文证据</h3><button type="button" class="text-button" data-close>关闭</button></div><div id="extractionEvidenceViewer" class="evidence-viewer"><div class="evidence-loading">正在读取原件...</div></div></section></div>`)
      const dialog = root.querySelector("#extractionEvidenceDialog")
      dialog.querySelector("[data-close]").addEventListener("click", () => dialog.remove())
      loadEvidence(dialog.querySelector("#extractionEvidenceViewer"), item, evidence.dataset.documentId, evidence.dataset.fragmentId)
      return
    }
    const actionButton = event.target.closest("[data-extraction-action]")
    if (!actionButton) return
    const card = actionButton.closest(".structured-extraction-card")
    const selected = [...card.querySelectorAll('input[type="checkbox"]:checked')].map((control) => control.value)
    if (actionButton.dataset.extractionAction === "adopt" && !selected.length) {
      return window.dispatchEvent(new CustomEvent("app:toast", {detail:"请至少选择一个字段"}))
    }
    root.insertAdjacentHTML("beforeend", `<div id="extractionDecisionDialog" class="modal-backdrop"><form class="modal-panel"><div class="section-heading"><h3>${actionButton.dataset.extractionAction === "adopt" ? "采纳结构化字段" : "拒绝结构化字段"}</h3><button type="button" class="text-button" data-close>关闭</button></div><p class="field-hint">采纳后会重新执行受控 Agent，仍需当前审批节点正式确认。</p><label>核验理由<textarea id="extractionReason" required rows="4"></textarea></label><div id="extractionDecisionError" class="error-box hidden"></div><div class="form-actions"><span></span><button type="submit" class="primary">确认</button></div></form></div>`)
    const dialog = root.querySelector("#extractionDecisionDialog")
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.remove())
    dialog.querySelector("form").addEventListener("submit", async (submitEvent) => {
      submitEvent.preventDefault(); const submit = submitEvent.submitter; submit.disabled = true
      try {
        const data = await api.manageStructuredExtraction(item.case_id, {extraction_id:actionButton.dataset.extractionId,action:actionButton.dataset.extractionAction,candidate_ids:selected,reason:dialog.querySelector("#extractionReason").value})
        Object.assign(item, data.case); dialog.remove(); window.dispatchEvent(new CustomEvent("app:toast", {detail:actionButton.dataset.extractionAction === "adopt" ? "字段已采纳并重新执行 Agent" : "候选已拒绝"})); renderTab("agents")
      } catch (reason) { const box = dialog.querySelector("#extractionDecisionError"); box.textContent = reason.message || String(reason); box.classList.remove("hidden"); submit.disabled = false }
    })
  })
}

function candidateReviews(candidates) {
  if (!candidates.length) return ""
  return `<section class="candidate-reviews"><div class="section-heading"><div><h3>异常与候选</h3><span>候选结果不会直接覆盖正式结论</span></div><span>${candidates.length} 项</span></div>
    <div class="candidate-list">${candidates.map(candidateCard).join("")}</div>
  </section>`
}

function candidateCard(candidate) {
  const statusLabel = ({candidate:"候选运行",pending:"等待正式审批",approved:"已由正式审批采纳",rejected:"已拒绝",superseded:"已失效"})[candidate.status] || candidate.status
  const comparison = candidate.comparison || {}
  return `<article class="candidate-card"><header><div><small>${escapeHtml(candidate.agent_label)} · ${escapeHtml(candidate.candidate_ref)}</small><h4>${escapeHtml(statusLabel)}</h4><p>${escapeHtml(candidate.eligibility_message || "")}</p></div><span class="badge ${candidate.status === "approved" ? "approved" : candidate.status === "rejected" ? "high" : "pending"}">${escapeHtml(statusLabel)}</span></header>
    ${comparison.mode === "field" ? creditCandidateDiff(comparison.differences || []) : contractCandidateDiff(comparison.documents || [])}
    <footer><small>提交申请不会修改正式结论；正式生效仍需当前审批节点确认。</small><div>${candidate.permissions?.can_reject_candidate ? `<button class="secondary" data-candidate-action="reject_candidate" data-incident-id="${escapeHtml(candidate.incident_id)}" data-request-id="${escapeHtml(candidate.review?.request_id || "")}">拒绝候选</button>` : ""}${candidate.permissions?.can_request_adoption ? `<button class="primary" data-candidate-action="request_adoption" data-incident-id="${escapeHtml(candidate.incident_id)}">提交采纳申请</button>` : ""}</div></footer>
  </article>`
}

function creditCandidateDiff(rows) {
  return `<div class="candidate-diff"><div class="candidate-diff-head"><span>对比项</span><span>正式结果</span><span>候选结果</span></div>${rows.map((row) => `<div class="candidate-diff-row ${row.changed ? "changed" : ""}"><b>${escapeHtml(row.label)}</b><span>${candidateValue(row.field, row.official)}</span><span>${candidateValue(row.field, row.candidate)}</span></div>`).join("")}</div>`
}

function contractCandidateDiff(documents) {
  return `<div class="candidate-contracts">${documents.map((doc) => `<div class="candidate-contract-row ${doc.changed ? "changed" : ""}"><div><b>${escapeHtml(doc.document_id)}</b><small>${doc.changed ? `${doc.changed_fields.length} 项变化` : "无变化"}</small></div><span>${escapeHtml(doc.official?.decision || "—")} · ${escapeHtml(doc.official?.risk_level || "—")}</span><span>${escapeHtml(doc.candidate?.decision || "—")} · ${escapeHtml(doc.candidate?.risk_level || "—")}</span><small>规则 ${doc.official?.rule_finding_count ?? "—"} → ${doc.candidate?.rule_finding_count ?? "—"} · AI ${doc.official?.ai_finding_count ?? "—"} → ${doc.candidate?.ai_finding_count ?? "—"} · 证据覆盖 ${percent(doc.official?.evidence_coverage)} → ${percent(doc.candidate?.evidence_coverage)}</small></div>`).join("")}</div>`
}

function candidateValue(field, value) {
  if (value == null || value === "") return "—"
  if (["requires_supplement","credit_locked"].includes(field)) return value ? "是" : "否"
  if (field === "approved_credit_limit") return money(value)
  if (field === "recommended_term_days") return `${value} 天`
  return escapeHtml(String(value))
}

function percent(value) { return value == null ? "—" : `${Math.round(Number(value) * 100)}%` }

function installCandidateActions(root, item, renderTab) {
  root.querySelector(".candidate-list")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-candidate-action]")
    if (!button) return
    const action = button.dataset.candidateAction
    const label = action === "request_adoption" ? "提交采纳申请" : "拒绝候选"
    root.insertAdjacentHTML("beforeend", `<div id="candidateDialog" class="modal-backdrop"><form class="modal-panel candidate-dialog"><div class="section-heading"><h3>${label}</h3><button type="button" class="text-button" data-close>关闭</button></div><p class="field-hint">该操作不会直接改变正式业务结论。</p><label>处置理由<textarea id="candidateReason" rows="4" required placeholder="填写核验依据和处置理由"></textarea></label><p id="candidateError" class="error-box hidden"></p><div class="form-actions"><span></span><button type="submit" class="${action === "request_adoption" ? "primary" : "secondary"}">${label}</button></div></form></div>`)
    const dialog = root.querySelector("#candidateDialog")
    dialog.querySelector("[data-close]").addEventListener("click", () => dialog.remove())
    dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.remove() })
    dialog.querySelector("form").addEventListener("submit", async (e) => {
      e.preventDefault(); const submit = e.submitter, error = dialog.querySelector("#candidateError"); submit.disabled = true; error.classList.add("hidden")
      try {
        const data = await api.manageAgentCandidate(item.case_id, {action, incident_id:button.dataset.incidentId, request_id:button.dataset.requestId || "", reason:dialog.querySelector("#candidateReason").value})
        Object.assign(item, data.case); dialog.remove(); window.dispatchEvent(new CustomEvent("app:toast", {detail:action === "request_adoption" ? "候选已提交正式审批" : "候选已拒绝"})); renderTab("agents")
      } catch (reason) { error.textContent = reason.message || String(reason); error.classList.remove("hidden"); submit.disabled = false }
    })
  })
}

function agentPlan(plan) {
  const groups = ["analysis","synthesis","decision","verification"].map((phase) => ({phase, nodes:(plan.nodes || []).filter((node) => node.phase === phase)})).filter((group) => group.nodes.length)
  const statusLabel = ({completed:"已完成",running:"运行中",failed:"需检查"})[plan.status] || plan.status
  const snapshot = plan.runtime_snapshot || {}, audit = plan.execution_audit || {}
  const auditLabel = ({conformant:"执行一致",non_conformant:"发现偏差",pending:"等待审计"})[audit.status] || audit.status
  return `<section class="agent-plan">
    <header class="agent-plan-head"><div><small>${escapeHtml(plan.plan_id)} · 计划版本 ${escapeHtml(plan.version || "—")}</small><h3>${escapeHtml(plan.label || plan.agent)}</h3><p>运行时选择 ${plan.task_count || 0} 个白名单任务，已完成 ${plan.completed_count || 0} 个</p><div class="agent-plan-tags"><span>${plan.frozen ? "计划已冻结" : "计划未冻结"}</span><span>规范 ${escapeHtml(plan.spec_hash || "—")}</span><span>${escapeHtml(plan.task_catalog_version || "任务目录未记录")}</span></div></div><div class="agent-plan-status"><span class="badge ${plan.status === "completed" ? "approved" : plan.status === "failed" ? "high" : "pending"}">${escapeHtml(statusLabel)}</span><small>累计 ${duration(plan.total_duration_ms)}</small></div></header>
    ${agentRuntimeSnapshot(snapshot)}
    <div class="agent-lanes">${groups.map(agentLane).join("")}</div>
    <div class="agent-audit ${audit.status === "non_conformant" ? "has-deviation" : ""}"><div><small>执行偏差审计</small><strong>${escapeHtml(auditLabel || "等待审计")}</strong></div><dl><div><dt>完整性</dt><dd>${audit.integrity_valid === true ? "通过" : audit.integrity_valid === false ? "失败" : "待核验"}</dd></div><div><dt>重试</dt><dd>${audit.retry_count || 0} 次</dd></div><div><dt>降级</dt><dd>${audit.fallback_count || 0} 次</dd></div></dl>${agentAuditIssues(audit)}</div>
  </section>`
}

function agentRuntimeSnapshot(snapshot) {
  const values = [
    ["信用规则", snapshot.credit_policy_version, snapshot.credit_policy_hash],
    ["合同规则", snapshot.contract_policy_version, snapshot.contract_policy_hash],
    ["模型", snapshot.ai_enabled ? snapshot.model : "未启用", ""],
    ["提示词", snapshot.prompt_version, snapshot.prompt_hash],
  ]
  if (!values.some(([, value]) => value)) return ""
  return `<div class="agent-runtime">${values.map(([label, value, hash]) => `<div><small>${escapeHtml(label)}</small><b>${escapeHtml(value || "—")}</b>${hash ? `<code>${escapeHtml(hash)}</code>` : ""}</div>`).join("")}</div>`
}

function agentAuditIssues(audit) {
  const issues = [
    ["缺失任务", audit.missing_tasks], ["越权任务", audit.unexpected_tasks],
    ["重复结果", audit.duplicate_results], ["证据违规", audit.evidence_violations],
  ].filter(([, values]) => values?.length)
  return issues.length ? `<div class="agent-audit-issues">${issues.map(([label, values]) => `<span><b>${escapeHtml(label)}</b>${values.map(escapeHtml).join("、")}</span>`).join("")}</div>` : ""
}

function agentLane(group) {
  const label = ({analysis:"并行分析",synthesis:"结果汇总",decision:"确定性决策",verification:"独立核验"})[group.phase] || group.phase
  return `<section class="agent-lane"><div class="agent-lane-label"><span>${escapeHtml(label)}</span><small>${group.nodes.length} 个节点</small></div><div class="agent-node-grid">${group.nodes.map(agentNode).join("")}</div></section>`
}

function agentNode(node) {
  const statusLabel = ({completed:"完成",degraded:"降级",failed:"失败",running:"运行",pending:"等待",reused:"已复用"})[node.status] || node.status
  const evidenceLabel = ({passed:"证据通过",degraded:"证据降级",failed:"证据失败",not_applicable:"无需证据"})[node.evidence_gate] || node.evidence_gate
  return `<article class="agent-node ${escapeHtml(node.status || "pending")}">
    <div class="agent-node-head"><span class="agent-status-dot" aria-hidden="true"></span><div><small>${escapeHtml(node.task_type || "task")}</small><h4>${escapeHtml(node.label || node.task_id)}</h4></div><b>${escapeHtml(statusLabel)}</b></div>
    <dl><div><dt>耗时</dt><dd>${duration(node.duration_ms)}</dd></div><div><dt>执行器</dt><dd>${escapeHtml(node.model || "待分配")}</dd></div></dl>
    <div class="agent-node-tags"><span>尝试 ${node.attempt_count || 0} 次</span>${node.idempotency_key ? `<span>幂等 ${escapeHtml(node.idempotency_key)}</span>` : ""}${node.evidence_gate && node.evidence_gate !== "not_applicable" ? `<span class="${node.evidence_gate === "degraded" || node.evidence_gate === "failed" ? "warn" : ""}">${escapeHtml(evidenceLabel)}</span>` : ""}</div>
    ${node.attempt_count > 1 ? `<div class="agent-attempts">${node.attempt_history.map((attempt) => `<span>第 ${attempt.attempt} 次 · ${escapeHtml(attempt.status || "未知")} · ${duration(attempt.duration_ms)}${attempt.error_type ? ` · ${escapeHtml(attempt.error_type)}` : ""}</span>`).join("")}</div>` : ""}
    <div class="agent-node-summary"><small>输入摘要</small><p>${escapeHtml(node.input_summary || "等待执行")}</p><small>输出摘要</small><p>${escapeHtml(node.output_summary || "尚无输出")}</p></div>
    ${node.depends_on?.length ? `<footer>依赖 ${node.depends_on.map((value) => escapeHtml(value)).join("、")}</footer>` : ""}
  </article>`
}

function duration(value) {
  if (value == null) return "—"
  const ms = Number(value || 0)
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`
}

function documentsTab(item) {
  const docs = item.source_documents || []
  return docs.length ? `<div class="document-layout"><section class="document-list"><div class="section-heading"><h3>案件原件</h3><span>${docs.length} 份</span></div>${docs.map((doc, index) => `<button class="document-row" ${doc.document_id && doc.fragment_count ? `data-document-index="${index}"` : "disabled"}><span class="document-icon">${escapeHtml((doc.media_type || "FILE").slice(0,4).toUpperCase())}</span><span><b>${escapeHtml(doc.name)}</b><small>${doc.document_kind === "contract" ? "合同" : "信用资料"} · ${parseStatus(doc.parse_status)} · ${doc.document_id ? `${doc.fragment_count || 0} 个可定位片段` : "历史文件需重新解析"}</small>${documentFeatures(doc)}</span></button>`).join("")}</section><aside id="documentViewer" class="evidence-viewer"><div class="evidence-placeholder"><strong>资料预览</strong><p>选择左侧文件查看首个可解析片段。</p></div></aside></div>` : emptyState("暂无原始资料", "当前案件没有上传文件。")
}

function documentFeatures(doc) {
  const features = doc.features || {}, labels = []
  if (features.ocr_page_count) labels.push(`OCR ${features.ocr_page_count} 页`)
  if (features.ocr_image_count) labels.push(`OCR 图片 ${features.ocr_image_count} 张`)
  if (features.word_table_cell_count) labels.push(`Word 表格 ${features.word_table_cell_count} 个单元格`)
  return labels.length ? `<small>${labels.map(escapeHtml).join(" · ")}</small>` : ""
}

function writebackTab(item) {
  const writeback = item.writeback || {}, entries = Object.entries(writeback)
  return `<div class="section-heading"><div><h3>企业系统回写</h3><span>${entries.length ? "已记录调用结果" : "尚未执行"}</span></div>${item.permissions?.can_run_mock_approval ? `<button id="runMockApproval" class="secondary">运行 Mock OA 审批</button>` : ""}</div>
    ${entries.length ? `<div class="integration-grid">${entries.map(([phase, result]) => integrationCard(phase, result, item.permissions?.can_retry_writeback)).join("")}</div>` : emptyState("暂无回写记录", "OA、CRM、SAP 调用结果会集中显示在这里。")}`
}

function installWritebackRetry(root, item, renderTab) {
  root.querySelector("#runMockApproval")?.addEventListener("click", async (event) => {
    const button = event.currentTarget
    button.disabled = true
    try {
      const data = await api.runMockEnterpriseApproval(item.case_id)
      Object.assign(item, data.case)
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"Mock OA审批完成，CRM/SAP回写已执行"}))
      renderTab("writeback")
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
    }
  })
  root.querySelector(".integration-grid")?.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-writeback-retry]")
    if (!button) return
    button.disabled = true
    try {
      const data = await api.retryWriteback(item.case_id, button.dataset.phase, button.dataset.system)
      Object.assign(item, data.case)
      window.dispatchEvent(new CustomEvent("app:toast", {detail:data.result.status === "succeeded" ? "回写重试成功" : "重试仍失败，请查看错误原因"}))
      renderTab("writeback")
    } catch (reason) {
      window.dispatchEvent(new CustomEvent("app:toast", {detail:reason.message || String(reason)}))
      button.disabled = false
    }
  })
}

async function loadEvidence(viewer, item, documentId, fragmentId) {
  viewer.innerHTML = `<div class="evidence-loading">正在读取原件...</div>`
  try {
    const data = await api.getDocumentFragment(item.case_id, documentId, fragmentId)
    viewer.innerHTML = `<header><div><strong>${escapeHtml(data.document.name)}</strong><small>${escapeHtml(data.selected_location_label)}</small></div><span>${escapeHtml(data.document.media_type.toUpperCase())}</span></header><div class="fragment-list">${data.fragments.map((fragment) => `<article class="document-fragment ${fragment.selected ? "selected" : ""}"><small>${escapeHtml(fragment.location_label)}</small><pre>${escapeHtml(fragment.text)}</pre></article>`).join("")}</div>${data.document.warnings?.length ? notice("解析提示", data.document.warnings) : ""}`
  } catch (reason) {
    viewer.innerHTML = `<div class="error-box">${escapeHtml(reason.message || reason)}</div>`
  }
}

function installEvidenceViewer(root, item) {
  root.querySelector(".finding-pane")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-finding-index]")
    if (!button) return
    const finding = item.findings[Number(button.dataset.findingIndex)]
    if (finding) loadEvidence(root.querySelector("#evidenceViewer"), item, finding.document_id, finding.fragment_id)
  })
  const aiFindings = (item.ai_assistance || []).flatMap((group) => group.findings || [])
  root.querySelector(".ai-assistance")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-ai-finding-index]")
    if (!button) return
    const finding = aiFindings[Number(button.dataset.aiFindingIndex)]
    if (!finding) return
    const viewer = root.querySelector("#evidenceViewer")
    loadEvidence(viewer, item, finding.document_id, finding.fragment_id)
    if (window.innerWidth <= 900) viewer.scrollIntoView({behavior:"smooth", block:"start"})
  })
}

function installDocumentViewer(root, item) {
  root.querySelector(".document-list")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-document-index]")
    if (!button) return
    const doc = item.source_documents[Number(button.dataset.documentIndex)]
    root.querySelectorAll(".document-row").forEach((row) => row.classList.toggle("active", row === button))
    loadEvidence(root.querySelector("#documentViewer"), item, doc.document_id, "")
  })
}

function showOwnerDialog(root, item, users) {
  const current = item.owner?.user_id || ""
  root.insertAdjacentHTML("beforeend", `<div id="ownerDialog" class="modal-backdrop"><form class="modal-panel"><div class="section-heading"><h3>分配案件负责人</h3><button type="button" class="text-button" data-close>关闭</button></div><label>销售负责人<select id="ownerUser">${users.map((user) => `<option value="${escapeHtml(user.user_id)}" ${user.user_id === current ? "selected" : ""}>${escapeHtml(user.display_name)} · ${escapeHtml(user.username)}</option>`).join("")}</select></label><div id="ownerError" class="error-box hidden"></div><div class="form-actions"><span></span><button class="primary" type="submit">确认分配</button></div></form></div>`)
  const dialog = root.querySelector("#ownerDialog")
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.remove())
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.remove() })
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault(); const error = dialog.querySelector("#ownerError"); error.classList.add("hidden")
    try { await api.assignCaseOwner(item.case_id, dialog.querySelector("#ownerUser").value); window.dispatchEvent(new CustomEvent("app:toast", {detail:"负责人已更新"})); window.dispatchEvent(new Event("app:navigate")) }
    catch (reason) { error.textContent = reason.message || reason; error.classList.remove("hidden") }
  })
}

function summary(label, value) { return `<article class="summary-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>` }
function info(label, value) { return `<div class="info-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>` }
function rail(label, value) { return `<div class="rail-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(value)}</b></div>` }
function notice(title, rows) { return `<div class="supplement-notice"><strong>${escapeHtml(title)}</strong><ul>${rows.map((row) => `<li>${escapeHtml(row)}</li>`).join("")}</ul></div>` }
function emptyState(title, text) { return `<div class="empty-state"><h2>${escapeHtml(title)}</h2><p>${escapeHtml(text)}</p></div>` }
function parseStatus(value) { return ({parsed:"已解析",pending:"等待解析",failed:"解析失败"})[value] || "状态未知" }
function approvalStage(stage) { return ({applicant:"申请人",marketing_director:"所属市场总监",credit_control:"信用管理",senior_finance_manager:"高级财务经理",group_finance_director:"集团财务总监"})[stage] || stage || "审批节点" }
function actionMessage(type) { return ({credit_approval:"模型评估已完成，等待信用审批后生效。",credit_supplement:"请补充信审人员要求的信用资料。",upload_contract:"信用审核已经完成，请提交合同。",submit_revision:"合同需要修改后重新提交。",manager_review:"该案件需要管理层确认。",manual_review:"该案件需要财务或法务复核。"})[type] || "请完成当前待办事项。" }
function integrationCard(phase, value, canRetry = false) {
  const rows = value && typeof value === "object"
    ? Object.entries(value).filter(([system]) => ["oa","crm","sap"].includes(system))
    : []
  const phaseLabel = ({credit_activation:"授信生效",contract_approval:"合同审批",case_completion:"案件完成"})[phase] || phase
  return `<article class="integration-card"><header><h3>${escapeHtml(phaseLabel)}</h3></header>${rows.length ? rows.map(([system,result]) => {
    const status = result?.status || "unknown"
    const systemLabel = ({oa:"OA 审批",crm:"CRM 客户",sap:"SAP 信用"})[system] || system.toUpperCase()
    const statusLabel = ({succeeded:"成功",success:"成功",failed:"失败",skipped:"已跳过",not_configured:"未配置",pending:"处理中",unknown:"未知"})[status] || status
    const retryCount = result?.retry_history?.length || 0
    return `<div class="integration-row"><div><span>${escapeHtml(systemLabel)}</span>${result?.error ? `<small class="integration-inline-error">${escapeHtml(result.error)}</small>` : ""}${retryCount ? `<small>已人工重试 ${retryCount} 次</small>` : ""}</div><div class="integration-row-actions"><b class="badge ${["success","succeeded"].includes(status) ? "approved" : status === "failed" ? "high" : "pending"}">${escapeHtml(statusLabel)}</b>${status === "failed" && canRetry ? `<button class="text-button" data-writeback-retry data-phase="${escapeHtml(phase)}" data-system="${escapeHtml(system)}">重试</button>` : ""}</div></div>`
  }).join("") : `<p>${escapeHtml(String(value || "无记录"))}</p>`}</article>`
}
