import {api} from "../api.js?v=20260802-five-role"
import {escapeHtml, dateTime} from "../format.js?v=20260802-five-role"

const roles = [
  ["case_submitter","业务经办人"],
  ["credit_approver","信用审批人"],
  ["legal_reviewer","合同法务"],
  ["exception_approver","授权审批人"],
  ["system_admin","系统管理员"],
]

export async function renderRegistrationsPage(root) {
  const data = await api.listRegistrations()
  const pending = data.applications.filter((item) => item.registration_status === "pending")
  const reviewed = data.applications.filter((item) => item.registration_status === "rejected")
  root.innerHTML = `
    <header class="page-header"><div><h1>注册审核</h1><p>核对申请人邮箱，为通过的账号分配实际工作角色</p></div><span class="badge ${pending.length ? "pending" : "approved"}">${pending.length} 个待审核</span></header>
    <section class="registration-list">
      ${pending.length ? pending.map(applicationCard).join("") : `<div class="panel empty-state"><h2>没有待审核申请</h2><p>新用户完成邮箱验证并提交注册后，会出现在这里。</p></div>`}
    </section>
    ${reviewed.length ? `<section class="panel reviewed-applications"><div class="section-heading"><h3>最近拒绝的申请</h3><span>${reviewed.length} 条</span></div><div class="table-scroll"><table><thead><tr><th>申请人</th><th>邮箱</th><th>提交时间</th><th>状态</th></tr></thead><tbody>${reviewed.map((item) => `<tr><td><b>${escapeHtml(item.display_name)}</b><small>${escapeHtml(item.username)}</small></td><td>${escapeHtml(item.email)}</td><td>${dateTime(item.created_at)}</td><td><span class="badge blocked">未通过</span></td></tr>`).join("")}</tbody></table></div></section>` : ""}`

  root.querySelectorAll("[data-registration-action]").forEach((button) => button.addEventListener("click", async () => {
    const card = button.closest("[data-application]")
    const error = card.querySelector(".error-box")
    const decision = button.dataset.registrationAction
    const selected = Array.from(card.querySelectorAll("input[type=radio]:checked")).map((item) => item.value)
    error.classList.add("hidden")
    if (decision === "approve" && !selected.length) {
      error.textContent = "请选择一个角色。"
      error.classList.remove("hidden")
      return
    }
    card.querySelectorAll("button").forEach((item) => item.disabled = true)
    try {
      await api.reviewRegistration(card.dataset.application, {decision, roles:selected.length ? selected : ["case_submitter"]})
      window.dispatchEvent(new CustomEvent("app:toast", {detail:decision === "approve" ? "账号已批准并发送通知" : "申请已拒绝并发送通知"}))
      window.dispatchEvent(new Event("app:navigate"))
    } catch (reason) {
      error.textContent = reason.message || String(reason)
      error.classList.remove("hidden")
      card.querySelectorAll("button").forEach((item) => item.disabled = false)
    }
  }))
}

function applicationCard(user) {
  return `<article class="panel registration-card" data-application="${escapeHtml(user.user_id)}">
    <div class="registration-person"><span>${escapeHtml((user.display_name || user.username).slice(0, 1))}</span><div><h2>${escapeHtml(user.display_name)}</h2><p>${escapeHtml(user.username)} · ${escapeHtml(user.email)}</p><small>提交于 ${dateTime(user.created_at)}，邮箱已验证</small></div></div>
    <fieldset class="role-picker"><legend>批准后角色（只能选择一个）</legend>${roles.map(([value, label]) => `<label><input type="radio" name="role-${escapeHtml(user.user_id)}" value="${value}" ${value === "case_submitter" ? "checked" : ""}><span>${label}</span></label>`).join("")}</fieldset>
    <div class="error-box hidden"></div>
    <div class="registration-actions"><button class="danger" type="button" data-registration-action="reject">拒绝申请</button><button class="primary" type="button" data-registration-action="approve">批准并启用</button></div>
  </article>`
}
