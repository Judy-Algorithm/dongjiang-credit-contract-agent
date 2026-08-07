const root = document.getElementById("app")
const shellHeader = document.getElementById("appHeader")
const loading = document.getElementById("globalLoading")
const toast = document.getElementById("toast")
let api
let clearAuth
let currentAuth
let hasRole
let loadAuth
let setAuthenticated
let currentRoute
let installRouter
let navigate
let renderSequence = 0
let navigationTimer = null
const modulePromises = new Map()
const navigationSummary = {unread:0, loadedAt:0, inFlight:null}
const NAVIGATION_SUMMARY_TTL = 15000
let renderedHeaderUserId = ""
const pageModulePaths = {
  login: "./pages/auth.js?v=20260807-contract-package",
  register: "./pages/auth.js?v=20260807-contract-package",
  "forgot-password": "./pages/auth.js?v=20260807-contract-package",
  setup: "./pages/auth.js?v=20260807-contract-package",
  "change-password": "./pages/auth.js?v=20260807-contract-package",
  cases: "./pages/cases.js?v=20260807-contract-package",
  "case-new": "./pages/case-new.js?v=20260807-contract-package",
  "case-detail": "./pages/case-detail.js?v=20260807-pdf-translation",
  "case-action": "./pages/case-action.js?v=20260807-contract-package",
  users: "./pages/users.js?v=20260807-contract-package",
  audit: "./pages/audit.js?v=20260807-contract-package",
  writebacks: "./pages/writebacks.js?v=20260807-contract-package",
  notifications: "./pages/notifications.js?v=20260807-contract-package",
  operations: "./pages/operations.js?v=20260807-contract-package",
  "agent-operations": "./pages/agent-operations.js?v=20260807-contract-package",
  analytics: "./pages/analytics.js?v=20260807-contract-package",
  benchmarks: "./pages/benchmark.js?v=20260807-contract-package",
}

async function loadModule(path) {
  if (modulePromises.has(path)) return modulePromises.get(path)
  const promise = import(path).catch(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 180))
    return import(`${path}&retry=${Date.now()}`)
  }).catch((error) => {
    modulePromises.delete(path)
    throw error
  })
  modulePromises.set(path, promise)
  return promise
}

async function loadPageModule(routeName) {
  const path = pageModulePaths[routeName]
  if (!path) throw new Error("页面模块不存在。")
  return loadModule(path)
}

async function bootstrap() {
  const [authModule, apiModule, routerModule] = await Promise.all([
    loadModule("./auth.js?v=20260807-contract-package"),
    loadModule("./api.js?v=20260807-contract-package"),
    loadModule("./router.js?v=20260807-contract-package"),
  ])
  ;({clearAuth, currentAuth, hasRole, loadAuth, setAuthenticated} = authModule)
  api = apiModule.api
  ;({currentRoute, installRouter, navigate} = routerModule)
  installRouter(render)
  await render()
  warmPageModules()
}

async function render() {
  const renderId = ++renderSequence
  const route = currentRoute()
  let committed = false
  startNavigationProgress()
  try {
    const auth = await loadAuth()
    if (auth.setupRequired && route.name !== "setup") return navigate("/setup", {replace:true})
    if (!auth.setupRequired && route.name === "setup") return navigate(auth.authenticated ? "/cases" : "/login", {replace:true})
    if (!auth.authenticated && !["login","register","forgot-password","setup"].includes(route.name)) return navigate("/login", {replace:true})
    if (auth.authenticated && route.name === "login") return navigate("/cases", {replace:true})
    if (route.name === "not-found") return navigate(auth.authenticated ? "/cases" : "/login", {replace:true})

    renderHeader(auth, route)
    setActiveNav(route)
    const page = await loadPageModule(route.name)
    const nextView = document.createElement("div")
    nextView.className = "route-view"
    if (route.name === "login") page.renderLoginPage(nextView)
    if (route.name === "register") page.renderRegisterPage(nextView)
    if (route.name === "forgot-password") page.renderForgotPasswordPage(nextView)
    if (route.name === "setup") page.renderSetupPage(nextView)
    if (route.name === "change-password") page.renderChangePasswordPage(nextView)
    if (route.name === "cases") await page.renderCasesPage(nextView, route)
    if (route.name === "case-new") {
      if (!hasRole("case_submitter")) throw new Error("只有业务经办人可以发起信审。")
      await page.renderNewCasePage(nextView, route)
    }
    if (route.name === "case-detail") await page.renderCaseDetailPage(nextView, route)
    if (route.name === "case-action") await page.renderCaseActionPage(nextView, route)
    if (route.name === "users") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以管理用户。")
      await page.renderUsersPage(nextView)
    }
    if (route.name === "audit") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以查看安全审计。")
      await page.renderAuditPage(nextView)
    }
    if (route.name === "writebacks") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以处理系统回写。")
      await page.renderWritebacksPage(nextView)
    }
    if (route.name === "notifications") await page.renderNotificationsPage(nextView)
    if (route.name === "operations") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以查看时效运营。")
      await page.renderOperationsPage(nextView)
    }
    if (route.name === "agent-operations") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以查看 Agent 运维。")
      await page.renderAgentOperationsPage(nextView)
    }
    if (route.name === "analytics") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以查看管理分析。")
      await page.renderAnalyticsPage(nextView, route)
    }
    if (route.name === "benchmarks") {
      if (!hasRole("system_admin")) throw new Error("只有系统管理员可以运行质量评测。")
      await page.renderBenchmarkPage(nextView)
    }
    if (renderId !== renderSequence) return
    root.replaceChildren(nextView)
    committed = true
    if (route.name === "notifications") refreshNavigationSummary(auth, {force:true})
  } catch (error) {
    if (renderId !== renderSequence) return
    root.innerHTML = `<div class="error-box"><b>页面加载失败</b><p>${escapeText(error.message || error)}</p><a href="/cases" data-link class="secondary">返回案件列表</a></div>`
    committed = true
  } finally {
    if (renderId === renderSequence) {
      stopNavigationProgress()
      if (committed) window.scrollTo({top:0, behavior:"instant"})
    }
  }
}

function renderHeader(auth, route) {
  const publicPage = ["login","register","forgot-password","setup"].includes(route.name)
  shellHeader.classList.toggle("hidden", publicPage)
  root.classList.toggle("auth-shell", publicPage)
  if (publicPage || !auth.authenticated) {
    renderedHeaderUserId = ""
    return
  }
  const user = auth.user
  const administrator = user.impersonated_by || null
  const canSwitchAccount = hasRole("system_admin") || Boolean(administrator)
  if (renderedHeaderUserId === user.user_id && shellHeader.childElementCount) {
    refreshNavigationSummary(auth)
    return
  }
  const unread = navigationSummary.unread
  shellHeader.innerHTML = `
    <a class="identity" href="/cases" data-link><span class="brand-mark">东江</span><span>信审与合同评审</span>${administrator ? `<em class="impersonation-pill">管理员模拟 · ${escapeText(user.display_name)}</em>` : ""}</a>
    <nav>
      <a href="/cases" data-link data-nav="cases">案件</a>
      ${hasRole("case_submitter") ? `<a href="/cases/new" data-link data-nav="new">发起信审</a>` : ""}
      <a href="/notifications" data-link data-nav="notifications">通知<span id="notificationCount" class="nav-count ${unread ? "" : "hidden"}">${Math.min(unread, 99)}</span></a>
      ${hasRole("system_admin") ? `<a href="/operations" data-link data-nav="operations">时效运营</a><a href="/agent-operations" data-link data-nav="agent-operations">Agent运维</a><a href="/analytics" data-link data-nav="analytics">管理分析</a><a href="/benchmarks" data-link data-nav="benchmarks">质量评测</a><a href="/audit" data-link data-nav="audit">审计</a><a href="/writebacks" data-link data-nav="writebacks">回写运维</a>` : ""}
      <div class="account-menu">
        <button id="accountButton" class="account-button" type="button" aria-label="${escapeText(user.display_name || user.username)}账户菜单"><span>${escapeText((user.display_name || user.username).slice(0,1))}</span><b>${escapeText(user.display_name || user.username)}</b></button>
        <div id="accountPopover" class="account-popover hidden">
          <strong>${escapeText(user.display_name)}</strong><small>${escapeText(user.role_labels.join(" · "))}</small>
          ${administrator ? `<div class="impersonation-note">${escapeText(administrator.display_name)} 正在模拟此账号，所有操作保留管理员审计记录。</div>` : ""}
          ${hasRole("system_admin") ? `<span class="account-section-label">系统管理</span><a class="${route.name === "users" ? "current" : ""}" href="/users" data-link data-account-nav="users">用户管理</a>` : ""}
          ${canSwitchAccount ? `<button id="switchAccountButton" type="button">切换账户</button>` : ""}
          ${administrator ? `<button id="returnAdminButton" type="button">返回系统管理员</button>` : `<a href="/change-password" data-link>修改密码</a>`}
          <button id="logoutButton" type="button">退出登录</button>
        </div>
      </div>
    </nav>`
  const accountButton = shellHeader.querySelector("#accountButton")
  accountButton.addEventListener("click", () => shellHeader.querySelector("#accountPopover").classList.toggle("hidden"))
  shellHeader.querySelector("#switchAccountButton")?.addEventListener("click", () => showAccountSwitchDialog(user))
  shellHeader.querySelector("#returnAdminButton")?.addEventListener("click", async () => {
    const data = await api.stopImpersonation()
    setAuthenticated(data)
    renderedHeaderUserId = ""
    navigationSummary.loadedAt = 0
    navigate("/cases", {replace:true})
  })
  shellHeader.querySelector("#logoutButton").addEventListener("click", async () => {
    await api.logout(); clearAuth(); navigate("/login", {replace:true})
  })
  renderedHeaderUserId = user.user_id
  refreshNavigationSummary(auth)
}

async function showAccountSwitchDialog(currentUser) {
  shellHeader.querySelector("#accountPopover")?.classList.add("hidden")
  const data = await api.listImpersonationUsers()
  const users = (data.users || []).filter((item) => item.user_id !== currentUser.user_id)
  if (!users.length) {
    window.dispatchEvent(new CustomEvent("app:toast", {detail:"没有其他可切换的启用账号"}))
    return
  }
  const roleMeta = {
    case_submitter:{mark:"业",tone:"submitter"},
    credit_approver:{mark:"信",tone:"credit"},
    legal_reviewer:{mark:"法",tone:"legal"},
    exception_approver:{mark:"批",tone:"approval"},
  }
  const currentRole = currentUser.role_labels?.join(" · ") || "当前账号"
  document.querySelector("#accountSwitchDialog")?.remove()
  document.body.insertAdjacentHTML("beforeend", `<div id="accountSwitchDialog" class="modal-backdrop account-switch-backdrop"><form class="modal-panel account-switch-dialog">
    <header class="account-switch-header"><div><span class="account-switch-kicker">ADMIN ACCOUNT SWITCH</span><h3>切换工作身份</h3><p>选择一个角色账号，立即查看对应待办并继续处理案件。</p></div><button type="button" class="account-switch-close" data-close aria-label="关闭">×</button></header>
    <div class="account-switch-current"><div class="account-switch-current-mark">${escapeText((currentUser.display_name || currentUser.username).slice(0,1))}</div><div><span>当前身份</span><b>${escapeText(currentUser.display_name)}</b><small>${escapeText(currentUser.username)} · ${escapeText(currentRole)}</small></div>${currentUser.impersonated_by ? `<em>管理员模拟中</em>` : `<em class="admin">系统管理员</em>`}</div>
    <div class="account-switch-tip"><b>安全模拟</b><span>无需目标账号密码；业务权限按目标角色执行，审计仍记录真实管理员。</span></div>
    <div class="account-switch-grid">${users.map((item) => { const role = item.roles?.[0] || "case_submitter"; const meta = roleMeta[role] || roleMeta.case_submitter; return `<label class="account-switch-card ${escapeText(meta.tone)}"><input type="radio" name="targetAccount" value="${escapeText(item.user_id)}" required><span class="account-switch-avatar">${escapeText(meta.mark)}</span><span class="account-switch-person"><b>${escapeText(item.display_name)}</b><small>${escapeText(item.username)}</small><em>${escapeText(item.role_labels.join(" · "))}</em></span><i>✓</i></label>` }).join("")}</div>
    <div id="accountSwitchError" class="error-box hidden"></div>
    <footer class="account-switch-actions">${currentUser.impersonated_by ? `<button id="switchReturnAdmin" class="secondary" type="button">返回系统管理员</button>` : `<span></span>`}<button class="primary" type="submit">切换到所选账户</button></footer>
  </form></div>`)
  const dialog = document.querySelector("#accountSwitchDialog")
  dialog.querySelector("[data-close]").addEventListener("click", () => dialog.remove())
  dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.remove() })
  dialog.querySelector("#switchReturnAdmin")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true
    const data = await api.stopImpersonation()
    setAuthenticated(data)
    renderedHeaderUserId = ""
    navigationSummary.loadedAt = 0
    dialog.remove()
    navigate("/cases", {replace:true})
  })
  dialog.querySelector("form").addEventListener("submit", async (event) => {
    event.preventDefault()
    const submit = event.submitter || dialog.querySelector('button[type="submit"]')
    const error = dialog.querySelector("#accountSwitchError")
    submit.disabled = true
    error.classList.add("hidden")
    try {
      const selected = dialog.querySelector('input[name="targetAccount"]:checked')
      if (!selected) throw new Error("请先选择一个工作身份。")
      const switched = await api.startImpersonation(selected.value)
      setAuthenticated(switched)
      renderedHeaderUserId = ""
      navigationSummary.loadedAt = 0
      dialog.remove()
      navigate("/cases", {replace:true})
    } catch (reason) {
      error.textContent = reason.message || String(reason)
      error.classList.remove("hidden")
      submit.disabled = false
    }
  })
}

function refreshNavigationSummary(auth, {force = false} = {}) {
  if (!auth.authenticated) return
  const fresh = Date.now() - navigationSummary.loadedAt < NAVIGATION_SUMMARY_TTL
  if (!force && fresh) return
  if (navigationSummary.inFlight) return navigationSummary.inFlight
  navigationSummary.inFlight = api.navigationSummary(force).then((data) => {
    navigationSummary.unread = Number(data.unread_notifications || 0)
    navigationSummary.loadedAt = Date.now()
    updateHeaderCounts(auth.user)
  }).catch(() => {}).finally(() => { navigationSummary.inFlight = null })
  return navigationSummary.inFlight
}

function updateHeaderCounts(user) {
  const notificationCount = shellHeader.querySelector("#notificationCount")
  if (notificationCount) {
    notificationCount.textContent = Math.min(navigationSummary.unread, 99)
    notificationCount.classList.toggle("hidden", !navigationSummary.unread)
  }
  const accountButton = shellHeader.querySelector("#accountButton")
  if (accountButton) accountButton.setAttribute("aria-label", `${user.display_name || user.username}账户菜单`)
}

function startNavigationProgress() {
  window.clearTimeout(navigationTimer)
  navigationTimer = window.setTimeout(() => loading.classList.remove("hidden"), 180)
}

function stopNavigationProgress() {
  window.clearTimeout(navigationTimer)
  loading.classList.add("hidden")
}

function warmPageModules() {
  const routes = ["cases", "case-detail", "case-action", "notifications"]
  if (hasRole("case_submitter")) routes.push("case-new")
  if (hasRole("system_admin")) routes.push("users", "operations", "agent-operations", "analytics", "benchmarks", "audit", "writebacks")
  const warm = () => {
    routes.forEach((name) => loadPageModule(name).catch(() => {}))
    Promise.allSettled([api.listCases(), api.listNotifications()])
  }
  if ("requestIdleCallback" in window) window.requestIdleCallback(warm, {timeout:1500})
  else window.setTimeout(warm, 300)
}

function setActiveNav(route) {
  const active = route.name === "case-new" ? "new" : route.name === "users" ? "users" : route.name === "notifications" ? "notifications" : route.name === "operations" ? "operations" : route.name === "agent-operations" ? "agent-operations" : route.name === "analytics" ? "analytics" : route.name === "benchmarks" ? "benchmarks" : route.name === "audit" ? "audit" : route.name === "writebacks" ? "writebacks" : "cases"
  document.querySelectorAll("[data-nav]").forEach((link) => link.classList.toggle("active", link.dataset.nav === active))
  document.querySelectorAll("[data-account-nav]").forEach((link) => link.classList.toggle("current", link.dataset.accountNav === route.name))
  shellHeader.querySelector("#accountPopover")?.classList.add("hidden")
}

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char])
}

window.addEventListener("app:toast", (event) => {
  toast.textContent = event.detail; toast.classList.remove("hidden"); window.setTimeout(() => toast.classList.add("hidden"), 2200)
})
window.addEventListener("app:navigation-summary-refresh", () => {
  const auth = currentAuth()
  navigationSummary.loadedAt = 0
  refreshNavigationSummary(auth, {force:true})
})
window.addEventListener("app:unauthorized", () => {
  if (!currentAuth().authenticated) return
  clearAuth(); navigate("/login", {replace:true})
})
window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault(); toast.textContent = event.reason?.message || "操作失败"; toast.classList.remove("hidden"); window.setTimeout(() => toast.classList.add("hidden"), 3500)
})

bootstrap().catch((error) => {
  loading.classList.add("hidden")
  root.innerHTML = `<div class="error-box"><b>页面资源加载失败</b><p>${escapeText(error.message || "网络连接短暂异常，请重新加载页面。")}</p><a href="${escapeText(window.location.href)}" class="secondary">重新加载</a></div>`
})
