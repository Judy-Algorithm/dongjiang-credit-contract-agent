import {api} from "../api.js?v=20260731-sla"
import {clearAuth, setAuthenticated} from "../auth.js?v=20260731-sla"
import {navigate} from "../router.js?v=20260731-sla"
import {escapeHtml} from "../format.js?v=20260731-sla"

export function renderLoginPage(root) {
  root.innerHTML = authLayout("登录", "使用企业账号进入信审工作台", `
    <form id="loginForm" class="auth-form">
      <label>用户名<input id="username" autocomplete="username" required autofocus></label>
      <label>密码<input id="password" type="password" autocomplete="current-password" required></label>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">登录</button>
      <div class="auth-links"><a href="/register" data-link>注册账号</a><a href="/forgot-password" data-link>忘记密码</a></div>
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

export function renderRegisterPage(root) {
  root.innerHTML = authLayout("注册账号", "验证邮箱后提交账号申请", `
    <form id="registerForm" class="auth-form">
      <label>姓名<input id="displayName" autocomplete="name" required autofocus></label>
      <label>用户名<input id="username" autocomplete="username" minlength="3" maxlength="40" required></label>
      <label>邮箱<div class="code-input-row"><input id="email" type="email" autocomplete="email" required><button id="sendRegistrationCode" class="secondary" type="button">发送验证码</button></div></label>
      <label>邮箱验证码<input id="verificationCode" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="6位数字" required></label>
      <label>密码<input id="password" type="password" autocomplete="new-password" minlength="10" required></label>
      <label>确认密码<input id="confirmPassword" type="password" autocomplete="new-password" minlength="10" required></label>
      <p class="field-hint">验证码10分钟内有效。申请提交后由管理员审核并分配角色，结果会发送到该邮箱。</p>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">提交注册申请</button>
      <div class="auth-links single"><a href="/login" data-link>返回登录</a></div>
    </form>`)
  const codeButton = root.querySelector("#sendRegistrationCode")
  codeButton.addEventListener("click", async () => {
    const error = root.querySelector("#authError")
    const email = root.querySelector("#email")
    error.classList.add("hidden")
    if (!email.reportValidity()) return
    codeButton.disabled = true
    try {
      const data = await api.requestRegistrationCode({email:email.value.trim()})
      window.dispatchEvent(new CustomEvent("app:toast", {detail:data.message}))
      root.querySelector("#verificationCode").focus()
      let seconds = 60
      codeButton.textContent = `${seconds}秒后重发`
      const timer = window.setInterval(() => {
        if (!document.body.contains(codeButton)) return window.clearInterval(timer)
        seconds -= 1
        codeButton.textContent = seconds > 0 ? `${seconds}秒后重发` : "重新发送"
        if (seconds <= 0) { codeButton.disabled = false; window.clearInterval(timer) }
      }, 1000)
    } catch (reason) {
      error.textContent = reason.message || String(reason)
      error.classList.remove("hidden")
      codeButton.disabled = false
    }
  })
  root.querySelector("#registerForm").addEventListener("submit", async (event) => {
    event.preventDefault()
    await submitAuth(root, async () => {
      const password = root.querySelector("#password").value
      if (password !== root.querySelector("#confirmPassword").value) throw new Error("两次输入的密码不一致。")
      const data = await api.register({
        display_name:root.querySelector("#displayName").value,
        username:root.querySelector("#username").value,
        email:root.querySelector("#email").value,
        verification_code:root.querySelector("#verificationCode").value,
        password,
      })
      window.dispatchEvent(new CustomEvent("app:toast", {detail:data.message}))
      navigate("/login", {replace:true})
    })
  })
}

export function renderForgotPasswordPage(root) {
  root.innerHTML = authLayout("找回密码", "使用账号绑定邮箱接收验证码", `
    <form id="resetRequestForm" class="auth-form">
      <label>邮箱<input id="email" type="email" autocomplete="email" required autofocus></label>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">发送验证码</button>
      <div class="auth-links single"><a href="/login" data-link>返回登录</a></div>
    </form>
    <form id="resetConfirmForm" class="auth-form hidden">
      <div class="auth-success" id="resetMessage"></div>
      <label>邮箱<input id="confirmEmail" type="email" autocomplete="email" required readonly></label>
      <label>6位验证码<input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label>
      <label>新密码<input id="newPassword" type="password" autocomplete="new-password" minlength="10" required></label>
      <label>确认新密码<input id="confirmPassword" type="password" autocomplete="new-password" minlength="10" required></label>
      <div id="authError" class="error-box hidden"></div>
      <button class="primary auth-submit" type="submit">重置密码</button>
      <div class="auth-links"><button id="resendCode" class="link-button" type="button">重新发送</button><a href="/login" data-link>返回登录</a></div>
    </form>`)
  const requestForm = root.querySelector("#resetRequestForm")
  const confirmForm = root.querySelector("#resetConfirmForm")
  requestForm.addEventListener("submit", async (event) => {
    event.preventDefault()
    await submitAuth(requestForm, async () => {
      const email = requestForm.querySelector("#email").value.trim()
      const data = await api.requestPasswordReset({email})
      confirmForm.querySelector("#confirmEmail").value = email
      confirmForm.querySelector("#resetMessage").textContent = data.message
      requestForm.classList.add("hidden")
      confirmForm.classList.remove("hidden")
      confirmForm.querySelector("#code").focus()
    })
  })
  confirmForm.addEventListener("submit", async (event) => {
    event.preventDefault()
    await submitAuth(confirmForm, async () => {
      const newPassword = confirmForm.querySelector("#newPassword").value
      if (newPassword !== confirmForm.querySelector("#confirmPassword").value) throw new Error("两次输入的新密码不一致。")
      await api.confirmPasswordReset({
        email:confirmForm.querySelector("#confirmEmail").value,
        code:confirmForm.querySelector("#code").value,
        new_password:newPassword,
      })
      window.dispatchEvent(new CustomEvent("app:toast", {detail:"密码已重置，请重新登录"}))
      navigate("/login", {replace:true})
    })
  })
  confirmForm.querySelector("#resendCode").addEventListener("click", () => {
    confirmForm.classList.add("hidden")
    requestForm.classList.remove("hidden")
  })
}

export function renderSetupPage(root) {
  root.innerHTML = authLayout("初始化管理员", "首次启动只需完成一次", `
    <form id="setupForm" class="auth-form">
      <label>姓名<input id="displayName" autocomplete="name" required autofocus></label>
      <label>管理员用户名<input id="username" autocomplete="username" minlength="3" required></label>
      <label>管理员邮箱<input id="email" type="email" autocomplete="email" required></label>
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
        email:root.querySelector("#email").value,
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
