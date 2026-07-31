import {api} from "../api.js"
import {clearAuth, setAuthenticated} from "../auth.js"
import {navigate} from "../router.js"
import {escapeHtml} from "../format.js"

export function renderLoginPage(root) {
  root.innerHTML = authLayout("登录", "使用企业账号进入信审工作台", `
    <form id="loginForm" class="auth-form">
      <label>用户名<input id="username" autocomplete="username" required autofocus></label>
      <label>密码<input id="password" type="password" autocomplete="current-password" required></label>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">登录</button>
    </form>`)
  root.querySelector("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault()
    await submitAuth(root, async () => {
      const data = await api.login({
        username:root.querySelector("#username").value,
        password:root.querySelector("#password").value,
      })
      setAuthenticated(data)
      navigate(data.user.must_change_password ? "/change-password" : "/cases?mine=1", {replace:true})
    })
  })
}

export function renderSetupPage(root) {
  root.innerHTML = authLayout("初始化管理员", "首次启动只需完成一次", `
    <form id="setupForm" class="auth-form">
      <label>姓名<input id="displayName" autocomplete="name" required autofocus></label>
      <label>管理员用户名<input id="username" autocomplete="username" minlength="3" required></label>
      <label>密码<input id="password" type="password" autocomplete="new-password" minlength="10" required></label>
      <label>确认密码<input id="confirmPassword" type="password" autocomplete="new-password" minlength="10" required></label>
      <p class="field-hint">至少10个字符，同时包含字母和数字。</p>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">创建管理员并进入系统</button>
    </form>`)
  root.querySelector("#setupForm").addEventListener("submit", async (event) => {
    event.preventDefault()
    await submitAuth(root, async () => {
      const password = root.querySelector("#password").value
      if (password !== root.querySelector("#confirmPassword").value) throw new Error("两次输入的密码不一致。")
      const data = await api.setup({
        display_name:root.querySelector("#displayName").value,
        username:root.querySelector("#username").value,
        password,
      })
      setAuthenticated(data)
      navigate("/cases?mine=1", {replace:true})
    })
  })
}

export function renderChangePasswordPage(root) {
  root.innerHTML = authLayout("修改密码", "初始密码仅用于首次登录", `
    <form id="passwordForm" class="auth-form">
      <label>当前密码<input id="currentPassword" type="password" autocomplete="current-password" required autofocus></label>
      <label>新密码<input id="newPassword" type="password" autocomplete="new-password" minlength="10" required></label>
      <label>确认新密码<input id="confirmPassword" type="password" autocomplete="new-password" minlength="10" required></label>
      <p class="field-hint">修改后会退出当前会话，请使用新密码重新登录。</p>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">更新密码</button>
    </form>`)
  root.querySelector("#passwordForm").addEventListener("submit", async (event) => {
    event.preventDefault()
    await submitAuth(root, async () => {
      const newPassword = root.querySelector("#newPassword").value
      if (newPassword !== root.querySelector("#confirmPassword").value) throw new Error("两次输入的新密码不一致。")
      await api.changePassword({
        current_password:root.querySelector("#currentPassword").value,
        new_password:newPassword,
      })
      clearAuth()
      navigate("/login", {replace:true})
    })
  })
}

function authLayout(title, subtitle, body) {
  return `<section class="auth-screen">
    <div class="auth-brand"><span class="brand-mark">东江</span><div><strong>信审与合同评审</strong><small>Credit & Contract Review</small></div></div>
    <div class="auth-panel">
      <header><h1>${escapeHtml(title)}</h1><p>${escapeHtml(subtitle)}</p></header>
      ${body}
    </div>
    <p class="auth-footer">企业内部系统 · 操作全程留痕</p>
  </section>`
}

async function submitAuth(root, action) {
  const error = root.querySelector("#authError")
  const button = root.querySelector("button[type=submit]")
  error.classList.add("hidden")
  button.disabled = true
  try {
    await action()
  } catch (reason) {
    error.textContent = reason.message || String(reason)
    error.classList.remove("hidden")
  } finally {
    button.disabled = false
  }
}
