import {api, encodeFiles} from "../api.js?v=20260731-nav"
import {escapeHtml} from "../format.js?v=20260731-nav"
import {navigate} from "../router.js?v=20260731-nav"

const draftKey = "dongjiang:new-case-draft"

export async function renderNewCasePage(root) {
  const draft = JSON.parse(sessionStorage.getItem(draftKey) || "{}")
  root.innerHTML = `
    <header class="page-header">
      <div><h1>发起信审</h1><p>提交客户资料和本次合作申请</p></div>
      <a href="/cases" data-link class="secondary">返回案件列表</a>
    </header>
    <div class="stepper">
      <div class="active" data-step-label="1">1　客户与本次合作</div>
      <div data-step-label="2">2　信用资料</div>
      <div data-step-label="3">3　确认提交</div>
    </div>
    <form id="newCaseForm" class="panel form-panel">
      <section data-step-panel="1">
        <h2>客户与本次合作</h2>
        <div class="form-grid">
          <label>客户名称 *<input id="customerName" autocomplete="organization" required></label>
          <label>统一社会信用代码<input id="unifiedCreditCode" placeholder="用于准确匹配企业"></label>
          <label>CRM客户编号<input id="crmCustomerId" placeholder="没有可留空"></label>
          <label>客户类型 *
            <select id="customerType" required><option value="">请选择</option><option value="new">新客户</option><option value="existing">存量客户</option></select>
          </label>
          <label>业务类型 *
            <select id="businessType" required><option value="">请选择</option><option value="TKP">TKP</option><option value="TKM">TKM</option></select>
          </label>
          <label>TKM业务子类型
            <select id="tkmBusinessSubtype"><option value="">非TKM无需填写</option><option value="automotive_standard">汽车及标准业务</option><option value="precision">精密模具业务</option></select>
          </label>
          <label>项目或产品名称 *<input id="projectName" required></label>
          <label>预计月度订单额（元）<input id="monthlyOrder" type="number" min="0" placeholder="未知可留空"></label>
          <label>本次订单或预计合同金额（元）<input id="contractAmount" type="number" min="0" placeholder="未知可留空"></label>
          <label>客户申请信用额度（元）<input id="requestedCreditLimit" type="number" min="0" placeholder="未知可留空"></label>
          <label>客户申请账期（天）<input id="requestedTermDays" type="number" min="1" placeholder="未知可留空"></label>
          <label>结算币种<select id="currency"><option value="CNY">人民币 CNY</option><option value="USD">美元 USD</option><option value="EUR">欧元 EUR</option><option value="HKD">港币 HKD</option></select></label>
          <label class="full">申请说明<textarea id="applicationReason" rows="4" placeholder="填写合作背景、特殊条件或额度账期申请原因"></textarea></label>
          <label class="full checkbox-row"><input id="purchaseExemptionRequested" type="checkbox">申请TKM首期采购款豁免</label>
        </div>
      </section>

      <section data-step-panel="2" class="hidden">
        <h2>信用资料</h2>
        <div class="form-grid">
          <label>注册资本（元）<input id="registeredCapital" type="number" min="0" placeholder="可由上传资料自动提取"></label>
          <label>成立年限<input id="yearsInBusiness" type="number" min="0" placeholder="可由上传资料自动提取"></label>
          <label>资产负债率（%）<input id="assetLiabilityRatio" type="number" step="0.01" placeholder="未知可留空"></label>
          <label>净利率（%）<input id="netMargin" type="number" step="0.01" placeholder="未知可留空"></label>
          <label>流动比率<input id="currentRatio" type="number" step="0.01" placeholder="未知可留空"></label>
          <label>营收增长率（%）<input id="revenueGrowth" type="number" step="0.01" placeholder="未知可留空"></label>
          <label>合作年限<input id="cooperationYears" type="number" min="0" step="0.1" placeholder="新客户可留空"></label>
          <label>近12个月逾期次数<input id="overdueCount" type="number" min="0" placeholder="未知可留空"></label>
          <label>近12个月最长逾期天数<input id="maxOverdueDays" type="number" min="0" placeholder="未知可留空"></label>
          <label>按时付款率（%）<input id="onTimeRate" type="number" min="0" max="100" step="0.01" placeholder="未知可留空"></label>
          <label>当前未收款金额（元）<input id="outstandingReceivables" type="number" min="0" placeholder="TKM尾款或TKP已出货未收款"></label>
          <label>在手已入单金额（元）<input id="openOrderAmount" type="number" min="0" placeholder="尚未结算的在手订单"></label>
          <label>当前未收款最长逾期（天）<input id="currentOverdueDays" type="number" min="0" placeholder="超过30天将触发锁定"></label>
          <label>最近一次订单日期<input id="lastOrderDate" type="date"></label>
        </div>
        <div class="form-section">
          <div class="form-section-title"><h3>第三方主体评级</h3><span>没有评级时可以不填写</span></div>
          <div id="ratingRows">
            ${ratingRow()}${ratingRow()}${ratingRow()}
          </div>
        </div>
        <div class="form-section">
          <div class="form-section-title"><h3>客户资料</h3><span>单文件不超过15MB，总计不超过30MB</span></div>
          <label class="upload-zone" for="creditFiles">
            <input id="creditFiles" type="file" multiple accept=".txt,.md,.docx,.pdf,.xlsx,.csv,.png,.jpg,.jpeg">
            <b>选择财报、评级报告或历史交易资料</b>
            <span>支持 TXT、DOCX、PDF、XLSX、CSV、PNG、JPG</span>
          </label>
          <div id="creditFileList" class="file-list"></div>
        </div>
      </section>

      <section data-step-panel="3" class="hidden">
        <h2>确认提交</h2>
        <div id="caseReview" class="review-list"></div>
      </section>

      <div class="form-actions">
        <button type="button" id="previousStep" class="secondary hidden">上一步</button>
        <div class="right">
          <button type="button" id="saveDraft" class="secondary">保存草稿</button>
          <button type="button" id="nextStep" class="primary">下一步</button>
          <button type="submit" id="submitCase" class="primary hidden">提交信用审核</button>
        </div>
      </div>
    </form>
  `

  restoreDraft(root, draft)
  let step = 1
  const showStep = (next) => {
    step = next
    root.querySelectorAll("[data-step-panel]").forEach((item) => item.classList.toggle("hidden", Number(item.dataset.stepPanel) !== step))
    root.querySelectorAll("[data-step-label]").forEach((item) => {
      const number = Number(item.dataset.stepLabel)
      item.classList.toggle("active", number === step)
      item.classList.toggle("done", number < step)
    })
    root.querySelector("#previousStep").classList.toggle("hidden", step === 1)
    root.querySelector("#nextStep").classList.toggle("hidden", step === 3)
    root.querySelector("#submitCase").classList.toggle("hidden", step !== 3)
    if (step === 3) renderReview(root, collectForm(root))
  }

  root.querySelector("#creditFiles").addEventListener("change", (event) => {
    root.querySelector("#creditFileList").innerHTML = Array.from(event.target.files).map((file) => `<span class="file-chip">${escapeHtml(file.name)}</span>`).join("")
  })
  root.querySelector("#nextStep").addEventListener("click", () => {
    if (step === 1 && !validateBasics(root)) return
    saveDraft(root)
    showStep(Math.min(3, step + 1))
  })
  root.querySelector("#previousStep").addEventListener("click", () => showStep(Math.max(1, step - 1)))
  root.querySelector("#saveDraft").addEventListener("click", () => {
    saveDraft(root)
    window.dispatchEvent(new CustomEvent("app:toast", {detail:"草稿已保存在当前浏览器"}))
  })
  root.querySelector("#newCaseForm").addEventListener("submit", async (event) => {
    event.preventDefault()
    if (!validateBasics(root)) { showStep(1); return }
    const customer = collectForm(root)
    const files = await encodeFiles(root.querySelector("#creditFiles").files)
    const data = await api.createCase({customer, files, use_cached_credit:true})
    sessionStorage.removeItem(draftKey)
    navigate(`/cases/${encodeURIComponent(data.case.case_id)}`, {replace:true})
  })
}

function ratingRow() {
  return `
    <div class="rating-row" data-rating-row>
      <select class="rating-agency"><option value="">请选择评级机构</option><option>中债资信</option><option>中诚信国际</option><option>联合资信</option></select>
      <input class="rating-value" placeholder="主体评级">
      <select class="rating-outlook"><option value="">评级展望</option><option>稳定</option><option>正面</option><option>负面</option></select>
      <input class="rating-date" type="date">
    </div>`
}

function value(root, id) {
  return root.querySelector(`#${id}`).value.trim()
}

function optionalNumber(root, id, percent = false) {
  const raw = value(root, id)
  if (raw === "") return null
  const number = Number(raw)
  return Number.isFinite(number) ? (percent ? number / 100 : number) : null
}

function collectForm(root) {
  const ratings = Array.from(root.querySelectorAll("[data-rating-row]")).map((row) => ({
    agency:row.querySelector(".rating-agency").value,
    rating:row.querySelector(".rating-value").value.trim(),
    outlook:row.querySelector(".rating-outlook").value,
    rating_date:row.querySelector(".rating-date").value,
    source:"用户录入",
  })).filter((item) => item.agency && item.rating)
  return {
    customer_name:value(root, "customerName"),
    unified_social_credit_code:value(root, "unifiedCreditCode"),
    crm_customer_id:value(root, "crmCustomerId"),
    customer_type:value(root, "customerType"),
    business_type:value(root, "businessType"),
    tkm_business_subtype:value(root, "tkmBusinessSubtype"),
    project_name:value(root, "projectName"),
    contract_amount:optionalNumber(root, "contractAmount"),
    requested_credit_limit:optionalNumber(root, "requestedCreditLimit"),
    requested_term_days:optionalNumber(root, "requestedTermDays"),
    currency:value(root, "currency"),
    application_reason:value(root, "applicationReason"),
    purchase_exemption_requested:root.querySelector("#purchaseExemptionRequested").checked,
    monthly_order_amount:optionalNumber(root, "monthlyOrder"),
    registered_capital:optionalNumber(root, "registeredCapital"),
    years_in_business:optionalNumber(root, "yearsInBusiness"),
    asset_liability_ratio:optionalNumber(root, "assetLiabilityRatio", true),
    net_margin:optionalNumber(root, "netMargin", true),
    current_ratio:optionalNumber(root, "currentRatio"),
    revenue_growth:optionalNumber(root, "revenueGrowth", true),
    cooperation_years:optionalNumber(root, "cooperationYears"),
    overdue_count_12m:optionalNumber(root, "overdueCount"),
    max_overdue_days_12m:optionalNumber(root, "maxOverdueDays"),
    on_time_payment_rate:optionalNumber(root, "onTimeRate", true),
    outstanding_receivables_amount:optionalNumber(root, "outstandingReceivables"),
    open_order_amount:optionalNumber(root, "openOrderAmount"),
    current_overdue_days:optionalNumber(root, "currentOverdueDays"),
    last_order_date:value(root, "lastOrderDate"),
    external_ratings:ratings,
  }
}

function validateBasics(root) {
  const required = ["customerName","customerType","businessType","projectName"]
  for (const id of required) {
    const input = root.querySelector(`#${id}`)
    if (!input.value.trim()) { input.reportValidity(); input.focus(); return false }
  }
  return true
}

function saveDraft(root) {
  sessionStorage.setItem(draftKey, JSON.stringify(collectForm(root)))
}

function restoreDraft(root, draft) {
  const mapping = {
    customerName:"customer_name",unifiedCreditCode:"unified_social_credit_code",
    crmCustomerId:"crm_customer_id",customerType:"customer_type",businessType:"business_type",
    tkmBusinessSubtype:"tkm_business_subtype",
    projectName:"project_name",contractAmount:"contract_amount",
    requestedCreditLimit:"requested_credit_limit",requestedTermDays:"requested_term_days",
    currency:"currency",applicationReason:"application_reason",
    monthlyOrder:"monthly_order_amount",registeredCapital:"registered_capital",
    yearsInBusiness:"years_in_business",assetLiabilityRatio:"asset_liability_ratio",
    netMargin:"net_margin",currentRatio:"current_ratio",revenueGrowth:"revenue_growth",
    cooperationYears:"cooperation_years",overdueCount:"overdue_count_12m",
    maxOverdueDays:"max_overdue_days_12m",onTimeRate:"on_time_payment_rate",
    outstandingReceivables:"outstanding_receivables_amount",openOrderAmount:"open_order_amount",
    currentOverdueDays:"current_overdue_days",
    lastOrderDate:"last_order_date",
  }
  root.querySelector("#purchaseExemptionRequested").checked = Boolean(draft.purchase_exemption_requested)
  for (const [id, key] of Object.entries(mapping)) {
    let item = draft[key]
    if (["assetLiabilityRatio","netMargin","revenueGrowth","onTimeRate"].includes(id) && item != null) item *= 100
    if (item != null) root.querySelector(`#${id}`).value = item
  }
  ;(draft.external_ratings || []).forEach((rating, index) => {
    const row = root.querySelectorAll("[data-rating-row]")[index]
    if (!row) return
    row.querySelector(".rating-agency").value = rating.agency || ""
    row.querySelector(".rating-value").value = rating.rating || ""
    row.querySelector(".rating-outlook").value = rating.outlook || ""
    row.querySelector(".rating-date").value = rating.rating_date || ""
  })
}

function renderReview(root, customer) {
  const labels = [
    ["客户名称", customer.customer_name || "未填写"],
    ["客户类型", customer.customer_type === "existing" ? "存量客户" : "新客户"],
    ["业务类型", customer.business_type],
    ["项目或产品", customer.project_name || "未填写"],
    ["月度订单额", customer.monthly_order_amount == null ? "未填写" : `¥${Number(customer.monthly_order_amount).toLocaleString("zh-CN")}`],
    ["第三方评级", customer.external_ratings.length ? `${customer.external_ratings.length} 条` : "未填写"],
    ["当前授信占用", `¥${Number((customer.outstanding_receivables_amount || 0) + (customer.open_order_amount || 0)).toLocaleString("zh-CN")}`],
    ["当前逾期", customer.current_overdue_days == null ? "未填写" : `${customer.current_overdue_days} 天`],
    ["上传资料", `${root.querySelector("#creditFiles").files.length} 个文件`],
  ]
  root.querySelector("#caseReview").innerHTML = labels.map(([label, item]) => `<div class="review-row"><span>${escapeHtml(label)}</span><b>${escapeHtml(item)}</b></div>`).join("")
}
