import {api} from "../api.js?v=20260801-agentsla"
import {escapeHtml, dateTime} from "../format.js?v=20260731-nav"

const roles = [
  ["sales","销售"],["credit","信用管理"],["finance","财务"],["legal","法务"],
  ["director","市场总监"],["ceo","集团管理层"],["admin","系统管理员"],
]

export async function renderUsersPage(root) {
  const data = await api.listUsers()
  root.innerHTML = `
    <header class="page-header"><div><h1>用户与权限</h1><p>管理员创建内部账号并分配审批职责</p></div><button id="newUser" class="primary">创建用户</button></header>
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
    if (button.dataset.userAction === "roles") showRoleForm(root, user)
    if (button.dataset.userAction === "password") showPasswordForm(root, user)
    if (button.dataset.userAction === "email") showEmailForm(root, user)
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
    <td><span class="badge ${user.active ? "approved" : ""}">${user.active ? "启用" : "已停用"}</span>${user.must_change_password ? `<small>等待修改初始密码</small>` : ""}</td>
    <td>${dateTime(user.last_login_at)}</td>
    <td><button class="text-button" data-user-action="roles" data-user-id="${escapeHtml(user.user_id)}">角色</button><button class="text-button" data-user-action="email" data-user-id="${escapeHtml(user.user_id)}">邮箱</button><button class="text-button" data-user-action="password" data-user-id="${escapeHtml(user.user_id)}">重置密码</button><button class="text-button ${user.active ? "danger-text" : ""}" data-user-action="toggle" data-user-id="${escapeHtml(user.user_id)}">${user.active ? "停用" : "启用"}</button></td>
  </tr>`
}

function showCreateForm(root) {
  const editor = root.querySelector("#userEditor")
  editor.classList.remove("hidden")
  editor.innerHTML = `<form id="createUserForm" class="inline-editor">
    <div class="form-section-title"><h3>创建内部用户</h3><button type="button" class="text-button" data-close>关闭</button></div>
    <div class="form-grid two"><label>姓名<input id="displayName" required></label><label>用户名<input id="username" minlength="3" required></label><label>邮箱<input id="email" type="email" required></label><label class="full">初始密码<input id="password" type="password" minlength="10" required></label></div>
    ${roleOptions([])}<div id="editorError" class="error-box hidden"></div>
    <div class="form-actions"><span class="field-hint">用户首次登录后必须修改密码。</span><button class="primary" type="submit">创建用户</button></div>
  </form>`
  editor.querySelector("[data-close]").addEventListener("click", () => editor.classList.add("hidden"))
  editor.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    await editorSubmit(editor, async () => {
      await api.createUser({
        display_name:editor.querySelector("#displayName").value,
        username:editor.querySelector("#username").value,
        email:editor.querySelector("#email").value,
        password:editor.querySelector("#password").value,
        roles:selectedRoles(editor),
      })
      await renderUsersPage(root)
    })
  })
}

function showEmailForm(root, user) {
  const editor = root.querySelector("#userEditor")
  editor.classList.remove("hidden")
  editor.innerHTML = `<form class="inline-editor">
    <div class="form-section-title"><h3>绑定 ${escapeHtml(user.display_name)} 的邮箱</h3><button type="button" class="text-button" data-close>关闭</button></div>
    <div class="form-grid two"><label class="full">邮箱<input id="email" type="email" value="${escapeHtml(user.email || "")}" required></label></div>
    <p class="field-hint">绑定后，该账号可以通过邮箱验证码找回密码。</p>
    <div id="editorError" class="error-box hidden"></div>
    <div class="form-actions"><span></span><button class="primary" type="submit">保存邮箱</button></div>
  </form>`
  editor.querySelector("[data-close]").addEventListener("click", () => editor.classList.add("hidden"))
  editor.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    await editorSubmit(editor, async () => {
      await api.updateUser(user.user_id, {email:editor.querySelector("#email").value})
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"邮箱已保存"}))
      await renderUsersPage(root)
    })
  })
}

function showRoleForm(root, user) {
  const editor = root.querySelector("#userEditor")
  editor.classList.remove("hidden")
  editor.innerHTML = `<form class="inline-editor">
    <div class="form-section-title"><h3>调整 ${escapeHtml(user.display_name)} 的角色</h3><button type="button" class="text-button" data-close>关闭</button></div>
    ${roleOptions(user.roles)}<div id="editorError" class="error-box hidden"></div>
    <div class="form-actions"><span></span><button class="primary" type="submit">保存角色</button></div>
  </form>`
  editor.querySelector("[data-close]").addEventListener("click", () => editor.classList.add("hidden"))
  editor.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    await editorSubmit(editor, async () => {
      await api.updateUser(user.user_id, {roles:selectedRoles(editor)})
      await renderUsersPage(root)
    })
  })
}

function showPasswordForm(root, user) {
  const editor = root.querySelector("#userEditor")
  editor.classList.remove("hidden")
  editor.innerHTML = `<form class="inline-editor">
    <div class="form-section-title"><h3>重置 ${escapeHtml(user.display_name)} 的密码</h3><button type="button" class="text-button" data-close>关闭</button></div>
    <div class="form-grid two"><label class="full">临时密码<input id="temporaryPassword" type="password" minlength="10" required></label></div>
    <p class="field-hint">保存后，该用户的现有会话立即失效，下次登录必须修改临时密码。</p>
    <div id="editorError" class="error-box hidden"></div>
    <div class="form-actions"><span></span><button class="primary" type="submit">确认重置</button></div>
  </form>`
  editor.querySelector("[data-close]").addEventListener("click", () => editor.classList.add("hidden"))
  editor.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    await editorSubmit(editor, async () => {
      await api.updateUser(user.user_id, {temporary_password:editor.querySelector("#temporaryPassword").value})
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"密码已重置"}))
      await renderUsersPage(root)
    })
  })
}

function roleOptions(selected) {
  const current = new Set(selected)
  return `<fieldset class="role-picker"><legend>角色</legend>${roles.map(([value,label]) => `<label><input type="checkbox" value="${value}" ${current.has(value) ? "checked" : ""}><span>${label}</span></label>`).join("")}</fieldset>`
}

function selectedRoles(root) {
  return Array.from(root.querySelectorAll(".role-picker input:checked")).map((item) => item.value)
}

async function editorSubmit(editor, action) {
  const error = editor.querySelector("#editorError")
  error.classList.add("hidden")
  try { await action() } catch (reason) { error.textContent = reason.message || reason; error.classList.remove("hidden") }
}
