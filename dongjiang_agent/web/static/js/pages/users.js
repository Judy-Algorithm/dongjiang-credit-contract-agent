import {api} from "../api.js?v=20260807-csrf-delete"
import {escapeHtml, dateTime} from "../format.js?v=20260807-request-templates"

const roles = [
  ["case_submitter","业务经办人"],
  ["credit_approver","信用审批人"],
  ["legal_reviewer","合同法务"],
  ["exception_approver","授权审批人"],
  ["system_admin","系统管理员"],
]

export async function renderUsersPage(root) {
  const data = await api.listUsers()
  root.innerHTML = `
    <header class="page-header"><div><h1>用户与权限</h1><p>创建内部账号并为每个账号分配一项工作职责</p></div><button id="newUser" class="primary">创建用户</button></header>
    <section id="userEditor" class="panel user-editor hidden"></section>
    <section class="panel"><div class="table-scroll"><table>
      <thead><tr><th>用户</th><th>角色</th><th>状态</th><th>最近登录</th><th>操作</th></tr></thead>
      <tbody>${data.users.map(userRow).join("")}</tbody>
    </table></div></section>`
  root.querySelector("#newUser").addEventListener("click", () => showCreateForm(root))
  root.querySelector("tbody").addEventListener("click", async (event) => {
    const button = event.target.closest("[data-user-action]")
    if (!button) return
    const user = data.users.find((item) => item.user_id === button.dataset.userId)
    if (!user) return
    if (button.dataset.userAction === "edit") showEditForm(root, user)
    if (button.dataset.userAction === "toggle") {
      await api.updateUser(user.user_id, {active:!user.active})
      await renderUsersPage(root)
    }
  })
}

function userRow(user) {
  return `<tr>
    <td><b>${escapeHtml(user.display_name)}</b><small>${escapeHtml(user.username)}</small><small>${escapeHtml(user.email || "未绑定邮箱")}</small></td>
    <td><div class="role-list">${user.role_labels.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div></td>
    <td><span class="badge ${user.active ? "approved" : ""}">${user.active ? "启用" : "已停用"}</span></td>
    <td>${dateTime(user.last_login_at)}</td>
    <td><button class="text-button" data-user-action="edit" data-user-id="${escapeHtml(user.user_id)}">编辑</button><button class="text-button ${user.active ? "danger-text" : ""}" data-user-action="toggle" data-user-id="${escapeHtml(user.user_id)}">${user.active ? "停用" : "启用"}</button></td>
  </tr>`
}

function showCreateForm(root) {
  const editor = root.querySelector("#userEditor")
  editor.classList.remove("hidden")
  editor.innerHTML = `<form class="inline-editor">
    <div class="form-section-title"><h3>创建内部用户</h3><button type="button" class="text-button" data-close>关闭</button></div>
    <div class="form-grid two"><label>昵称<input id="displayName" required></label><label>用户名<input id="username" minlength="3" required></label><label>邮箱<input id="email" type="email" required></label><label>登录密码<input id="password" type="password" minlength="10" required></label></div>
    ${roleOptions([])}<div id="editorError" class="error-box hidden"></div>
    <div class="form-actions"><span class="field-hint">创建后账号立即启用，可直接使用该密码登录。</span><button class="primary" type="submit">创建用户</button></div>
  </form>`
  bindClose(editor)
  editor.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    await editorSubmit(editor, async () => {
      await api.createUser({
        display_name:value(editor, "#displayName"),
        username:value(editor, "#username"),
        email:value(editor, "#email"),
        password:value(editor, "#password"),
        roles:selectedRoles(editor),
      })
      await renderUsersPage(root)
    })
  })
}

function showEditForm(root, user) {
  const editor = root.querySelector("#userEditor")
  editor.classList.remove("hidden")
  editor.innerHTML = `<form class="inline-editor">
    <div class="form-section-title"><h3>编辑用户</h3><button type="button" class="text-button" data-close>关闭</button></div>
    <div class="form-grid two"><label>昵称<input id="displayName" value="${escapeHtml(user.display_name)}" required></label><label>用户名<input id="username" value="${escapeHtml(user.username)}" minlength="3" required></label><label>邮箱<input id="email" type="email" value="${escapeHtml(user.email || "")}" required></label><label>重置密码（选填）<input id="temporaryPassword" type="password" minlength="10" autocomplete="new-password"><span class="field-hint">留空则不修改密码</span></label></div>
    ${roleOptions(user.roles)}
    <p class="field-hint">填写新密码后，该用户的现有会话会立即失效，可直接使用新密码登录。</p>
    <div id="editorError" class="error-box hidden"></div>
    <div class="form-actions"><span></span><button class="primary" type="submit">保存修改</button></div>
  </form>`
  bindClose(editor)
  editor.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    await editorSubmit(editor, async () => {
      const payload = {
        display_name:value(editor, "#displayName"),
        username:value(editor, "#username"),
        email:value(editor, "#email"),
        roles:selectedRoles(editor),
      }
      const temporaryPassword = value(editor, "#temporaryPassword")
      if (temporaryPassword) payload.temporary_password = temporaryPassword
      await api.updateUser(user.user_id, payload)
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"用户资料已更新"}))
      await renderUsersPage(root)
    })
  })
}

function roleOptions(selected) {
  const current = new Set(selected)
  return `<fieldset class="role-picker"><legend>角色（每个账号只能选择一个）</legend>${roles.map(([value,label]) => `<label><input type="radio" name="role" value="${value}" ${current.has(value) ? "checked" : ""} required><span>${label}</span></label>`).join("")}</fieldset>`
}

function selectedRoles(root) {
  return Array.from(root.querySelectorAll(".role-picker input:checked")).map((item) => item.value)
}

function value(root, selector) {
  return root.querySelector(selector).value.trim()
}

function bindClose(editor) {
  editor.querySelector("[data-close]").addEventListener("click", () => editor.classList.add("hidden"))
}

async function editorSubmit(editor, action) {
  const error = editor.querySelector("#editorError")
  error.classList.add("hidden")
  try { await action() } catch (reason) { error.textContent = reason.message || reason; error.classList.remove("hidden") }
}
