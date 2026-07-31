import {api} from "../api.js"
import {customerType, dateTime, escapeHtml, money, statusClass} from "../format.js"

export async function renderCaseDetailPage(root, route) {
  const {case:item} = await api.getCase(route.caseId)
  const customer = item.customer || {}
  const credit = item.credit || {}
  const model = credit.model_result || {}
  const approved = credit.approved_result
  root.innerHTML = `
    <header class="page-header">
      <div>
        <div class="detail-title">
          <h1>${escapeHtml(customer.customer_name || "未命名客户")}</h1>
          <span class="badge ${statusClass(item.status)}">${escapeHtml(item.status_label)}</span>
        </div>
        <p>${escapeHtml(item.case_id)}　·　${customerType(customer.customer_type)}　·　${escapeHtml(customer.business_type || "—")}</p>
      </div>
      <div class="header-actions">
        <a href="/cases" data-link class="secondary">返回列表</a>
        ${item.next_action ? `<a href="/cases/${encodeURIComponent(item.case_id)}/action" data-link class="primary">${escapeHtml(item.next_action.label)}</a>` : ""}
      </div>
    </header>
    ${item.next_action ? `
      <section class="action-banner">
        <div><h2>${escapeHtml(item.status_label)}</h2><p>${actionMessage(item.next_action.type)}</p></div>
        <a href="/cases/${encodeURIComponent(item.case_id)}/action" data-link class="primary">${escapeHtml(item.next_action.label)}</a>
      </section>` : ""}
    <section class="summary-grid">
      ${summary("模型信用分", model.score == null ? "—" : Number(model.score).toFixed(1))}
      ${summary("风险等级", item.risk_label)}
      ${summary(approved ? "正式授信额度" : "建议授信额度", money(approved?.credit_limit ?? model.credit_limit))}
      ${summary(approved ? "正式账期" : "建议账期", (approved?.term_days ?? model.term_days) == null ? "—" : `${approved?.term_days ?? model.term_days} 天`)}
    </section>
    <section class="panel">
      <div class="tabs">
        <button class="tab active" data-tab="credit">信用结果</button>
        <button class="tab" data-tab="contract">合同结果</button>
        <button class="tab" data-tab="records">处理记录</button>
        <button class="tab" data-tab="documents">案件资料</button>
      </div>
      <div id="tabBody" class="tab-body"></div>
    </section>
  `

  const renderTab = (tab) => {
    root.querySelectorAll(".tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab))
    root.querySelector("#tabBody").innerHTML = {
      credit:creditTab(item),
      contract:contractTab(item),
      records:recordsTab(item),
      documents:documentsTab(item),
    }[tab]
  }
  root.querySelector(".tabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-tab]")
    if (button) renderTab(button.dataset.tab)
  })
  renderTab("credit")
}

function summary(label, value) {
  return `<article class="summary-card"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></article>`
}

function actionMessage(type) {
  return ({
    credit_approval:"模型评估已完成，等待信用审批后生效。",
    credit_supplement:"请补充信审人员要求的信用资料。",
    upload_contract:"信用审核已经完成，请提交合同。",
    submit_revision:"合同需要修改后重新提交。",
    manager_review:"该案件需要管理层确认。",
    manual_review:"该案件需要财务或法务复核。",
  })[type] || "请完成当前待办事项。"
}

function creditTab(item) {
  const credit = item.credit || {}
  const model = credit.model_result || {}
  const approved = credit.approved_result
  const selected = credit.rating_resolution?.selected
  const coverage = credit.data_coverage_ratio == null ? "—" : `${Math.round(Number(credit.data_coverage_ratio) * 100)}%`
  const control = Object.keys(item.credit_control || {}).length ? item.credit_control : (approved || model)
  return `
    <div class="form-section-title"><h3>模型建议</h3><span>${approved ? "审批时的模型基准" : "尚需人工审批后生效"}</span></div>
    <div class="info-grid">
      ${info("信用分", model.score == null ? "—" : Number(model.score).toFixed(1))}
      ${info("风险等级", model.risk_label)}
      ${info("建议授信额度", money(model.credit_limit))}
      ${info("建议账期", model.term_days == null ? "—" : `${model.term_days} 天`)}
      ${info("最长账期", model.hard_term_limit_days == null ? "—" : `${model.hard_term_limit_days} 天`)}
      ${info("资料覆盖率", coverage)}
      ${info("采用评级", selected ? `${selected.agency || ""} ${selected.rating || ""}`.trim() : "未取得有效评级")}
      ${item.customer?.business_type === "TKM" ? info("TKM业务子类型", item.customer.tkm_business_subtype === "automotive_standard" ? "汽车及标准业务" : "精密模具业务") : ""}
      ${item.customer?.business_type === "TKM" ? info("首期采购款豁免", model.purchase_exemption_approved ? "已批准" : "未批准") : ""}
    </div>
    <div class="form-section">
      <div class="form-section-title"><h3>信用控制</h3><span>${control.credit_locked ? "已锁定，需特别放行" : "未锁定"}</span></div>
      <div class="info-grid">
        ${info("总批准额度", money(approved?.credit_limit ?? model.credit_limit))}
        ${info("当前已占用", money(control.occupied_credit_amount))}
        ${info("当前可用额度", money(control.available_credit_amount))}
      </div>
      ${control.credit_lock_reasons?.length ? `<div class="supplement-notice"><strong>锁定原因</strong><ul>${control.credit_lock_reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></div>` : ""}
    </div>
    ${credit.requires_supplement ? `
      <div class="supplement-notice">
        <strong>需要补充信用资料</strong>
        <ul>${(credit.supplement_reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
      </div>` : ""}
    <div class="form-section">
      <div class="form-section-title"><h3>正式授信</h3><span>${approved ? "已生效" : "尚未生效"}</span></div>
      ${approved ? `<div class="info-grid">
        ${info("正式授信额度", money(approved.credit_limit))}
        ${info("正式账期", approved.term_days == null ? "—" : `${approved.term_days} 天`)}
        ${info("有效期至", approved.expires_at ? approved.expires_at.replace("T"," ").slice(0,10) : "—")}
      </div>` : `<div class="empty-state"><p>信用审批通过后，这里会显示正式额度、账期和有效期。</p></div>`}
    </div>
    <div class="form-section">
      <div class="form-section-title"><h3>缺失资料</h3><span>缺失值未按0计算</span></div>
      ${credit.missing_fields?.length
        ? `<div class="file-list">${credit.missing_fields.map((field) => `<span class="file-chip">${escapeHtml(field.label)}</span>`).join("")}</div>`
        : `<p>关键信用资料完整。</p>`}
    </div>
    ${credit.reasons?.length ? `<div class="form-section"><div class="form-section-title"><h3>评估说明</h3></div><ul>${credit.reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></div>` : ""}
    ${item.approval_evidence?.length ? `<div class="form-section"><div class="form-section-title"><h3>审批证据</h3><span>${item.approval_evidence.length} 个已归档附件</span></div><div class="review-list">${item.approval_evidence.map((evidence) => `<div class="review-row"><b>${escapeHtml(evidence.name)}</b><span>SHA-256 ${escapeHtml(String(evidence.sha256 || "").slice(0,12))}</span></div>`).join("")}</div></div>` : ""}
    ${item.approval_chain?.length ? `<div class="form-section"><div class="form-section-title"><h3>OA审批链</h3></div><div class="review-list">${item.approval_chain.map((step) => `<div class="review-row"><b>${escapeHtml(approvalStage(step.stage))}</b><span>${escapeHtml(step.status || "pending")}</span></div>`).join("")}</div></div>` : ""}
  `
}

function contractTab(item) {
  if (!item.credit?.effective) return `<div class="empty-state"><h2>合同上传尚未开放</h2><p>信用审批通过并形成正式授信后，销售才可以上传合同。</p></div>`
  if (!item.contracts?.length) return `<div class="empty-state"><h2>尚未提交合同</h2><p>信用审核完成后，可以上传合同进行审核。</p></div>`
  return `
    <div class="info-grid">
      ${item.contracts.map((contract) => `
        <div class="info-item">
          <span>${escapeHtml(contract.name || "合同")}</span>
          <strong>${money(contract.amount)}</strong>
          <small>账期：${contract.payment_term_days == null ? "未识别" : `${contract.payment_term_days} 天`}</small>
        </div>`).join("")}
    </div>
    <div class="form-section">
      <div class="form-section-title"><h3>风险事项</h3><span>${item.findings.length} 项</span></div>
      ${item.findings.length ? item.findings.map((finding) => `
        <article class="finding ${escapeHtml(finding.level || "")}">
          <h3>${escapeHtml(finding.title)}</h3>
          <p>${escapeHtml(finding.message)}</p>
          <small>处理建议：${escapeHtml(finding.suggestion)}</small>
        </article>`).join("") : `<p>未发现需要处理的合同风险。</p>`}
    </div>
  `
}

function recordsTab(item) {
  return item.records?.length
    ? `<div class="record-list">${item.records.map((record) => `<div class="record"><b>${escapeHtml(record.label)}</b><time>${dateTime(record.time)}</time></div>`).join("")}</div>`
    : `<div class="empty-state"><p>暂无处理记录。</p></div>`
}

function documentsTab(item) {
  const docs = item.source_documents || []
  return docs.length
    ? `<div class="review-list">${docs.map((doc) => `<div class="review-row"><b>${escapeHtml(doc.name)}</b><span>${doc.document_kind === "contract" ? "合同" : "信用资料"} · SHA-256 ${escapeHtml(String(doc.sha256 || "").slice(0,12))}</span></div>`).join("")}</div>`
    : `<div class="empty-state"><p>当前案件没有上传文件。</p></div>`
}

function approvalStage(stage) {
  return ({applicant:"申请人",marketing_director:"所属市场总监",credit_control:"信用管理",senior_finance_manager:"高级财务经理",group_finance_director:"集团财务总监"})[stage] || stage || "审批节点"
}

function info(label, value) {
  return `<div class="info-item"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></div>`
}
