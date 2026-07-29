import {api, encodeFiles} from "../api.js"
import {escapeHtml, money} from "../format.js"
import {navigate} from "../router.js"

export async function renderCaseActionPage(root, route) {
  const {case:item} = await api.getCase(route.caseId)
  if (!item.next_action) {
    navigate(`/cases/${encodeURIComponent(item.case_id)}`, {replace:true})
    return
  }
  const type = item.next_action.type
  root.innerHTML = `
    <header class="page-header">
      <div><h1>${escapeHtml(item.next_action.label)}</h1><p>${escapeHtml(item.customer.customer_name)}　·　${escapeHtml(item.case_id)}</p></div>
      <a href="/cases/${encodeURIComponent(item.case_id)}" data-link class="secondary">返回案件详情</a>
    </header>
    <section class="panel action-card">
      ${context(item)}
      ${["upload_contract","submit_revision"].includes(type)
        ? contractForm(type)
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
    const encoded = await encodeFiles(files?.files || [])
    const contractText = root.querySelector("#contractText")?.value.trim() || ""
    let data
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
        comment:root.querySelector("#comment")?.value.trim() || "",
      })
    } else {
      data = await api.submitContractAction(item.case_id, {
        action,
        comment:root.querySelector("#comment")?.value.trim() || "",
        files:encoded,
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
  return `
    <h2>${escapeHtml(item.status_label)}</h2>
    <p>${message(item.next_action.type)}</p>
    <div class="decision-context">
      <div><span>风险等级</span><b>${escapeHtml(item.risk_label)}</b></div>
      <div><span>模型信用分</span><b>${model.score == null ? "—" : Number(model.score).toFixed(1)}</b></div>
      <div><span>${approved ? "正式授信额度" : "建议授信额度"}</span><b>${money(approved?.credit_limit ?? model.credit_limit)}</b></div>
      <div><span>${approved ? "正式账期" : "建议账期"}</span><b>${(approved?.term_days ?? model.term_days) == null ? "—" : `${approved?.term_days ?? model.term_days} 天`}</b></div>
    </div>
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
  })[type] || "请处理当前事项。"
}

function contractForm(type) {
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
    : type === "manager_review"
    ? [["reject","驳回","danger"],["approve","批准","primary"]]
    : [["request_revision","要求修改合同","secondary"],["supplement","补充资料","secondary"],["approve","确认通过","primary"]]
  const model = item.credit?.model_result || {}
  return `
    <form id="actionForm">
      ${type === "credit_approval" ? `
        <div class="form-grid two">
          <label>正式授信额度（元）<input id="approvedCreditLimit" type="number" min="0" value="${escapeHtml(model.credit_limit ?? "")}"></label>
          <label>正式账期（天）<input id="approvedTermDays" type="number" min="1" max="${escapeHtml(model.hard_term_limit_days ?? "")}" value="${escapeHtml(model.term_days ?? "")}"></label>
        </div>` : ""}
      ${type === "manual_review" ? `
        <div class="form-section">
          <label class="upload-zone" for="actionFiles">
            <input id="actionFiles" type="file" multiple accept=".txt,.md,.docx,.pdf,.xlsx,.csv,.png,.jpg,.jpeg">
            <b>选择需要补充的资料</b><span>仅“补充资料”操作需要上传文件</span>
          </label>
          <div id="actionFileList" class="file-list"></div>
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
