import {api, encodeFiles} from "../api.js?v=20260801-smooth2"
import {escapeHtml, money} from "../format.js?v=20260801-smooth2"
import {navigate} from "../router.js?v=20260801-smooth2"

export async function renderCaseActionPage(root, route) {
  const {case:item} = await api.getCase(route.caseId)
  if (!item.next_action) {
    navigate(`/cases/${encodeURIComponent(item.case_id)}`, {replace:true})
    return
  }
  const type = item.next_action.type
  const candidateAgent = type === "credit_approval" ? "credit" : ["manual_review","manager_review"].includes(type) ? "contract" : ""
  const candidateRequest = (item.agent_candidates || []).find((candidate) => candidate.status === "pending" && candidate.agent === candidateAgent)
  root.innerHTML = `
    <header class="page-header">
      <div><h1>${escapeHtml(item.next_action.label)}</h1><p>${escapeHtml(item.customer.customer_name)}　·　${escapeHtml(item.case_id)}</p></div>
      <a href="/cases/${encodeURIComponent(item.case_id)}" data-link class="secondary">返回案件详情</a>
    </header>
    <section class="panel action-card">
      ${context(item)}
      ${candidateRequest ? `<div class="candidate-approval-banner"><div><b>Agent候选正在等待本节点确认</b><small>候选 ${escapeHtml(candidateRequest.candidate_ref)} · 仅在点击批准时采纳</small></div><label class="checkbox-row"><input id="adoptCandidate" type="checkbox">本次批准采纳候选结果</label></div>` : ""}
      ${["upload_contract","submit_revision"].includes(type)
        ? contractForm(type, item)
        : type === "credit_supplement"
          ? creditSupplementForm()
          : decisionForm(type, item)}
    </section>
  `

  const form = root.querySelector("#actionForm")
  const files = root.querySelector("#actionFiles")
  if (files) files.addEventListener("change", () => {
    root.querySelector("#actionFileList").innerHTML = Array.from(files.files).map((file) => `<span class="file-chip">${escapeHtml(file.name)}</span>`).join("")
  })

  form.addEventListener("submit", async (event) => {
    event.preventDefault()
    const submitter = event.submitter
    const action = submitter?.dataset.action
    const candidateRequestId = root.querySelector("#adoptCandidate")?.checked ? candidateRequest?.review?.request_id || "" : ""
    if (candidateRequestId && action !== "approve") throw new Error("采纳Agent候选时请使用批准操作；调整后批准将保留人工调整值。")
    const encoded = await encodeFiles(files?.files || [])
    const contractText = root.querySelector("#contractText")?.value.trim() || ""
    let data
    if (type === "submit_revision" && action === "create_revision") {
      const decisions = Array.from(root.querySelectorAll("[data-revision-finding]")).map((card) => ({
        finding_key:card.dataset.revisionFinding,
        action:card.querySelector("select").value,
        replacement:card.querySelector("textarea[data-replacement]")?.value.trim() || "",
        reason:card.querySelector("textarea[data-reason]")?.value.trim() || "",
      }))
      if (!decisions.length) throw new Error("当前没有可自动定位的风险项，请上传人工修订版本。")
      const documentId = root.querySelector("#revisionDocumentId")?.value || ""
      await api.createContractRevision(item.case_id, {document_id:documentId, decisions})
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"修订版本已生成"}))
      navigate(`/cases/${encodeURIComponent(item.case_id)}/action`, {replace:true})
      return
    }
    if (type === "submit_revision" && action === "submit_revision_version") {
      const revisionId = submitter.dataset.revisionId
      const submitted = await api.submitContractRevision(item.case_id, revisionId)
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"清洁版已重新送审"}))
      navigate(`/cases/${encodeURIComponent(submitted.case.case_id)}`, {replace:true})
      return
    }
    if (["manager_review","special_release"].includes(type) && action === "approve" && !encoded.length) {
      throw new Error("批准例外或特别放行时必须上传审批证据附件。")
    }
    if (["upload_contract","submit_revision"].includes(type) && action !== "close_case") {
      if (!encoded.length && !contractText) throw new Error("请上传合同文件或粘贴合同正文。")
      data = await api.submitContract(item.case_id, {
        files:encoded,
        contract_texts:contractText ? [contractText] : [],
      })
    } else if (type === "credit_supplement" && action !== "close_case") {
      if (!encoded.length) throw new Error("请至少上传一份信用资料。")
      data = await api.submitCreditDocuments(item.case_id, {files:encoded})
    } else if (type === "credit_supplement") {
      data = await api.submitCreditAction(item.case_id, {action})
    } else if (type === "credit_approval") {
      data = await api.submitCreditAction(item.case_id, {
        action,
        approved_credit_limit:optionalNumber(root, "#approvedCreditLimit"),
        approved_term_days:optionalNumber(root, "#approvedTermDays"),
        purchase_exemption_approved:root.querySelector("#purchaseExemptionApproved")?.checked || false,
        approval_scope:root.querySelector("#approvalScope")?.value.trim() || "",
        validity_days:optionalNumber(root, "#validityDays"),
        oa_evidence_id:root.querySelector("#oaEvidenceId")?.value.trim() || "",
        comment:root.querySelector("#comment")?.value.trim() || "",
      })
    } else {
      data = await api.submitContractAction(item.case_id, {
        action,
        comment:root.querySelector("#comment")?.value.trim() || "",
        candidate_adoption_request_id:candidateRequestId,
        approval_scope:root.querySelector("#approvalScope")?.value.trim() || "",
        validity_days:optionalNumber(root, "#validityDays"),
        oa_evidence_id:root.querySelector("#oaEvidenceId")?.value.trim() || "",
        files:["manager_review","special_release"].includes(type) && action !== "approve" ? [] : encoded,
        candidate_adoption_request_id:candidateRequestId,
      })
    }
    window.dispatchEvent(new CustomEvent("app:toast", {detail:"操作已提交"}))
    navigate(`/cases/${encodeURIComponent(data.case.case_id)}`, {replace:true})
  })
}

function context(item) {
  const credit = item.credit || {}
  const model = credit.model_result || {}
  const approved = credit.approved_result
  const coverage = credit.data_coverage_ratio == null ? "—" : `${Math.round(Number(credit.data_coverage_ratio) * 100)}%`
  return `
    <h2>${escapeHtml(item.status_label)}</h2>
    <p>${message(item.next_action.type)}</p>
    <div class="decision-context">
      <div><span>风险等级</span><b>${escapeHtml(item.risk_label)}</b></div>
      <div><span>模型信用分</span><b>${model.score == null ? "—" : Number(model.score).toFixed(1)}</b></div>
      <div><span>${approved ? "正式授信额度" : "建议授信额度"}</span><b>${money(approved?.credit_limit ?? model.credit_limit)}</b></div>
      <div><span>${approved ? "正式账期" : "建议账期"}</span><b>${(approved?.term_days ?? model.term_days) == null ? "—" : `${approved?.term_days ?? model.term_days} 天`}</b></div>
      <div><span>资料覆盖率</span><b>${coverage}</b></div>
    </div>
    ${credit.requires_supplement ? `<div class="supplement-notice"><strong>本次补件原因</strong><ul>${(credit.supplement_reasons || []).map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></div>` : ""}
    ${item.credit_control?.credit_lock_reasons?.length ? `<div class="supplement-notice"><strong>信用控制锁定原因</strong><ul>${item.credit_control.credit_lock_reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul></div>` : ""}
    ${item.findings?.length ? `<div class="form-section"><div class="form-section-title"><h3>需要处理的问题</h3></div>${item.findings.map((finding) => `<article class="finding ${escapeHtml(finding.level || "")}"><h3>${escapeHtml(finding.title)}</h3><p>${escapeHtml(finding.message)}</p><small>${escapeHtml(finding.suggestion)}</small></article>`).join("")}</div>` : ""}
  `
}

function message(type) {
  return ({
    credit_approval:"请审核模型建议，并决定是否让授信正式生效。",
    credit_supplement:"请补充财报、评级报告或历史合作资料。",
    upload_contract:"请上传合同文件，或者粘贴合同正文。",
    submit_revision:"请修改风险事项后，提交最新版本合同。",
    manager_review:"请确认是否批准本次例外申请。",
    manual_review:"请确认审核结果，补充资料，或要求业务修改合同。",
    special_release:"客户信用控制已锁定，请核验原因并上传特别放行证据。",
  })[type] || "请处理当前事项。"
}

function contractForm(type, item) {
  if (type === "submit_revision") return revisionForm(item)
  return `
    <form id="actionForm">
      <div class="form-section">
        <label class="upload-zone" for="actionFiles">
          <input id="actionFiles" type="file" multiple accept=".txt,.md,.docx,.pdf">
          <b>${type === "submit_revision" ? "选择修改后的合同" : "选择合同文件"}</b>
          <span>支持 TXT、DOCX、PDF</span>
        </label>
        <div id="actionFileList" class="file-list"></div>
        <label class="full">合同正文
          <textarea id="contractText" rows="9" placeholder="也可以在这里粘贴合同正文"></textarea>
        </label>
      </div>
      <div class="form-actions">
        <button type="submit" class="danger" data-action="close_case">关闭案件</button>
        <button type="submit" class="primary" data-action="${type}">${type === "submit_revision" ? "重新提交" : "提交合同审核"}</button>
      </div>
    </form>`
}

function revisionForm(item) {
  const supported = (item.source_documents || []).filter((doc) =>
    doc.document_kind === "contract" && ["txt","md","docx"].includes((doc.media_type || "").toLowerCase())
  )
  const documentIds = new Set(supported.map((doc) => doc.document_id))
  const findings = (item.findings || []).filter((finding) => finding.finding_key && documentIds.has(finding.document_id))
  const grouped = supported.map((doc) => ({doc, findings:findings.filter((finding) => finding.document_id === doc.document_id)})).filter((row) => row.findings.length)
  const selected = grouped.at(-1) || grouped[0]
  const revisions = item.contract_revisions || []
  return `
    <form id="actionForm">
      ${selected ? `<div class="form-section revision-editor">
        <div class="section-heading"><h3>逐项处理风险</h3><span>${selected.findings.length} 项可定位</span></div>
        <label>修订合同<select id="revisionDocumentId"><option value="${escapeHtml(selected.doc.document_id)}">${escapeHtml(selected.doc.name)}</option></select></label>
        <div class="revision-findings">${selected.findings.map(revisionFinding).join("")}</div>
        <div class="form-actions"><button type="submit" class="danger" data-action="close_case">关闭案件</button><button type="submit" class="primary" data-action="create_revision">生成修订版本</button></div>
      </div>` : `<div class="supplement-notice"><strong>当前合同无法自动修订</strong><p>仅可对具有行号或段落号定位的 TXT、MD、DOCX 合同生成修订稿。请在下方上传人工修改后的版本。</p></div>`}
      ${revisionHistory(item, revisions)}
      <div class="form-section"><div class="section-heading"><h3>上传人工修订版本</h3><span>适用于 PDF 或复杂版式合同</span></div>
        <label class="upload-zone" for="actionFiles"><input id="actionFiles" type="file" multiple accept=".txt,.md,.docx,.pdf"><b>选择修改后的合同</b><span>支持 TXT、DOCX、PDF</span></label>
        <div id="actionFileList" class="file-list"></div>
        <label class="full">合同正文<textarea id="contractText" rows="7" placeholder="也可以在这里粘贴修改后的合同正文"></textarea></label>
        <div class="form-actions"><span></span><button type="submit" class="secondary" data-action="submit_revision">直接重新送审</button></div>
      </div>
    </form>`
}

function revisionFinding(finding) {
  const suggestion = finding.suggested_replacement || ""
  return `<article class="revision-finding" data-revision-finding="${escapeHtml(finding.finding_key)}">
    <header><div><small>${escapeHtml(finding.rule_id)} · ${escapeHtml(finding.location_label)}</small><h3>${escapeHtml(finding.title)}</h3></div>
      <select aria-label="${escapeHtml(finding.title)}处置方式"><option value="${suggestion ? "accept" : "custom"}">${suggestion ? "采用标准建议" : "人工修改"}</option>${suggestion ? `<option value="custom">人工修改</option>` : ""}<option value="retain">保留并说明</option></select></header>
    <p>${escapeHtml(finding.message)}</p>
    <label>替换后的完整条款<textarea data-replacement rows="4" placeholder="填写替换后的完整条款">${escapeHtml(suggestion)}</textarea></label>
    <label>修改或保留理由<textarea data-reason rows="2" placeholder="保留原条款时必填；修改时建议填写谈判依据"></textarea></label>
  </article>`
}

function revisionHistory(item, revisions) {
  if (!revisions.length) return ""
  return `<div class="form-section"><div class="section-heading"><h3>修订版本</h3><span>${revisions.length} 个版本</span></div>
    <div class="revision-list">${revisions.map((revision) => `<article class="revision-version"><div><b>${escapeHtml(revision.revision_id)}</b><small>${escapeHtml(revision.source_name)} · ${revision.decisions?.length || 0} 项处置 · ${escapeHtml(revision.status === "submitted" ? "已送审" : "草稿")}</small></div><div class="revision-actions"><a class="secondary" href="${api.revisionDownloadUrl(item.case_id, revision.revision_id, "redline")}">下载修订稿</a><a class="secondary" href="${api.revisionDownloadUrl(item.case_id, revision.revision_id, "clean")}">下载清洁稿</a>${revision.status === "draft" ? `<button type="submit" class="primary" data-action="submit_revision_version" data-revision-id="${escapeHtml(revision.revision_id)}">用清洁稿重新送审</button>` : ""}</div></article>`).join("")}</div>
  </div>`
}

function creditSupplementForm() {
  return `
    <form id="actionForm">
      <div class="form-section">
        <label class="upload-zone" for="actionFiles">
          <input id="actionFiles" type="file" multiple accept=".txt,.md,.docx,.pdf,.xlsx,.csv,.png,.jpg,.jpeg">
          <b>选择补充信用资料</b><span>财报、审计报告、评级报告或历史交易资料</span>
        </label>
        <div id="actionFileList" class="file-list"></div>
      </div>
      <div class="form-actions">
        <button type="submit" class="danger" data-action="close_case">关闭申请</button>
        <button type="submit" class="primary" data-action="submit_supplement">提交补充资料</button>
      </div>
    </form>`
}

function decisionForm(type, item) {
  const actions = type === "credit_approval"
    ? [["reject","拒绝","danger"],["request_supplement","要求补充资料","secondary"],["adjust_and_approve","调整后批准","secondary"],["approve","批准","primary"]]
    : ["manager_review","special_release"].includes(type)
    ? [["reject","驳回","danger"],["approve","批准","primary"]]
    : [["request_revision","要求修改合同","secondary"],["supplement","补充资料","secondary"],["approve","确认通过","primary"]]
  const model = item.credit?.model_result || {}
  return `
    <form id="actionForm">
      ${type === "credit_approval" ? `
        <div class="form-grid two">
          <label>正式授信额度（元）<input id="approvedCreditLimit" type="number" min="0" value="${escapeHtml(model.credit_limit ?? "")}"></label>
          <label>正式账期（天）<input id="approvedTermDays" type="number" min="1" max="${escapeHtml(model.hard_term_limit_days ?? "")}" value="${escapeHtml(model.term_days ?? "")}"></label>
          <label>批准有效期（天）<input id="validityDays" type="number" min="1" max="3650" value="180"></label>
          <label>OA证据ID<input id="oaEvidenceId" placeholder="OA流程或附件编号"></label>
          <label class="full">批准范围<input id="approvalScope" placeholder="客户、业务类型、项目或订单范围"></label>
          ${item.customer?.business_type === "TKM" ? `<label class="full checkbox-row"><input id="purchaseExemptionApproved" type="checkbox">批准未收回首期款即可采购项目物料</label>` : ""}
        </div>` : ""}
      ${type === "manual_review" ? `
        <div class="form-section">
          <label class="upload-zone" for="actionFiles">
            <input id="actionFiles" type="file" multiple accept=".txt,.md,.docx,.pdf,.xlsx,.csv,.png,.jpg,.jpeg">
            <b>选择需要补充的资料</b><span>仅“补充资料”操作需要上传文件</span>
          </label>
          <div id="actionFileList" class="file-list"></div>
        </div>` : ""}
      ${["manager_review","special_release"].includes(type) ? `
        <div class="form-section">
          <label class="upload-zone" for="actionFiles">
            <input id="actionFiles" type="file" multiple accept=".txt,.md,.docx,.pdf,.png,.jpg,.jpeg,.eml,.msg">
            <b>上传审批证据附件 *</b><span>批准邮件、OA截图、终端项目统一账期或特别放行材料</span>
          </label>
          <div id="actionFileList" class="file-list"></div>
          <div class="form-grid two">
            <label>批准范围 *<input id="approvalScope" placeholder="本案件、项目、订单或例外条件"></label>
            <label>有效期（天）<input id="validityDays" type="number" min="1" max="3650" value="30"></label>
            <label class="full">OA证据ID<input id="oaEvidenceId" placeholder="OA流程或附件编号"></label>
          </div>
        </div>` : ""}
      <label class="full">处理意见<textarea id="comment" rows="4" placeholder="填写审批或复核意见"></textarea></label>
      <div class="form-actions"><span></span><div class="right">
        ${actions.map(([action,label,style]) => `<button type="submit" class="${style}" data-action="${action}">${label}</button>`).join("")}
      </div></div>
    </form>`
}

function optionalNumber(root, selector) {
  const input = root.querySelector(selector)
  if (!input || input.value.trim() === "") return null
  const value = Number(input.value)
  return Number.isFinite(value) ? value : null
}
