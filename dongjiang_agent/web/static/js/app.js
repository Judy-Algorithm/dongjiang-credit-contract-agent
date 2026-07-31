import {api} from "./api.js?v=20260731-nav"
import {clearAuth, currentAuth, hasRole, loadAuth} from "./auth.js?v=20260731-nav"
import {currentRoute, installRouter, navigate} from "./router.js?v=20260731-nav"
import {renderAuditPage} from "./pages/audit.js?v=20260731-nav"
import {renderChangePasswordPage, renderForgotPasswordPage, renderLoginPage, renderRegisterPage, renderSetupPage} from "./pages/auth.js?v=20260731-nav"
import {renderCasesPage} from "./pages/cases.js?v=20260731-nav"
import {renderNewCasePage} from "./pages/case-new.js?v=20260731-nav"
import {renderCaseDetailPage} from "./pages/case-detail.js?v=20260731-agentops2"
import {renderCaseActionPage} from "./pages/case-action.js?v=20260731-nav"
import {renderUsersPage} from "./pages/users.js?v=20260731-nav"
import {renderWritebacksPage} from "./pages/writebacks.js?v=20260731-nav"
import {renderRegistrationsPage} from "./pages/registrations.js?v=20260731-nav"
import {renderNotificationsPage} from "./pages/notifications.js?v=20260731-nav"
import {renderOperationsPage} from "./pages/operations.js?v=20260731-nav"
import {renderAgentOperationsPage} from "./pages/agent-operations.js?v=20260731-agentops2"
import {renderAnalyticsPage} from "./pages/analytics.js?v=20260731-nav"

const root = document.getElementById("app")
const shellHeader = document.getElementById("appHeader")
const loading = document.getElementById("globalLoading")
const toast = document.getElementById("toast")

async function render() {
  const route = currentRoute()
  loading.classList.remove("hidden")
  root.innerHTML = ""
  try {
    const auth = await loadAuth()
    if (auth.setupRequired && route.name !== "setup") return navigate("/setup", {replace:true})
    if (!auth.setupRequired && route.name === "setup") return navigate(auth.authenticated ? "/cases" : "/login", {replace:true})
    if (!auth.authenticated && !["login","register","forgot-password","setup"].includes(route.name)) return navigate("/login", {replace:true})
    if (auth.authenticated && route.name === "login") return navigate("/cases", {replace:true})
    if (auth.authenticated && auth.user.must_change_password && route.name !== "change-password") return navigate("/change-password", {replace:true})
    if (route.name === "not-found") return navigate(auth.authenticated ? "/cases" : "/login", {replace:true})

    await renderHeader(auth, route)
    setActiveNav(route)
    if (route.name === "login") renderLoginPage(root)
    if (route.name === "register") renderRegisterPage(root)
    if (route.name === "forgot-password") renderForgotPasswordPage(root)
    if (route.name === "setup") renderSetupPage(root)
    if (route.name === "change-password") renderChangePasswordPage(root)
    if (route.name === "cases") await renderCasesPage(root, route)
    if (route.name === "case-new") {
      if (!hasRole("sales")) throw new Error("只有销售角色可以发起信审。")
      await renderNewCasePage(root, route)
    }
    if (route.name === "case-detail") await renderCaseDetailPage(root, route)
    if (route.name === "case-action") await renderCaseActionPage(root, route)
    if (route.name === "users") {
      if (!hasRole("admin")) throw new Error("只有管理员可以管理用户。")
      await renderUsersPage(root)
    }
    if (route.name === "audit") {
      if (!hasRole("admin")) throw new Error("只有管理员可以查看安全审计。")
      await renderAuditPage(root)
    }
    if (route.name === "writebacks") {
      if (!hasRole("admin")) throw new Error("只有管理员可以处理系统回写。")
      await renderWritebacksPage(root)
    }
    if (route.name === "registrations") {
      if (!hasRole("admin")) throw new Error("只有管理员可以审核注册申请。")
      await renderRegistrationsPage(root)
    }
    if (route.name === "notifications") await renderNotificationsPage(root)
    if (route.name === "operations") {
      if (!hasRole("admin")) throw new Error("只有管理员可以查看时效运营。")
      await renderOperationsPage(root)
    }
    if (route.name === "agent-operations") {
      if (!hasRole("admin")) throw new Error("只有管理员可以查看 Agent 运维。")
      await renderAgentOperationsPage(root)
    }
    if (route.name === "analytics") {
      if (!hasRole("admin")) throw new Error("只有管理员可以查看管理分析。")
      await renderAnalyticsPage(root, route)
    }
  } catch (error) {
    root.innerHTML = `<div class="error-box"><b>页面加载失败</b><p>${escapeText(error.message || error)}</p><a href="/cases" data-link class="secondary">返回案件列表</a></div>`
  } finally {
    loading.classList.add("hidden")
    window.scrollTo({top:0, behavior:"instant"})
  }
}

async function renderHeader(auth, route) {
  const publicPage = ["login","register","forgot-password","setup"].includes(route.name)
  shellHeader.classList.toggle("hidden", publicPage)
  root.classList.toggle("auth-shell", publicPage)
  if (publicPage || !auth.authenticated) return
  const user = auth.user
  let unread = 0
  let pendingRegistrations = 0
  try {
    unread = Number((await api.listNotifications()).unread || 0)
    if (hasRole("admin")) {
      const applications = (await api.listRegistrations()).applications || []
      pendingRegistrations = applications.filter((item) => item.registration_status === "pending").length
    }
  } catch (_) {
    // Header counters are helpful but must not block the working page.
  }
  shellHeader.innerHTML = `
    <a class="identity" href="/cases" data-link><span class="brand-mark">东江</span><span>信审与合同评审</span></a>
    <nav>
      <a href="/cases" data-link data-nav="cases">案件</a>
      ${hasRole("sales") ? `<a href="/cases/new" data-link data-nav="new">发起信审</a>` : ""}
      <a href="/notifications" data-link data-nav="notifications">通知${unread ? `<span class="nav-count">${Math.min(unread, 99)}</span>` : ""}</a>
      ${hasRole("admin") ? `<a href="/operations" data-link data-nav="operations">时效运营</a><a href="/agent-operations" data-link data-nav="agent-operations">Agent运维</a><a href="/analytics" data-link data-nav="analytics">管理分析</a><a href="/users" data-link data-nav="users">用户</a><a href="/audit" data-link data-nav="audit">审计</a><a href="/writebacks" data-link data-nav="writebacks">回写运维</a>` : ""}
      <div class="account-menu">
        <button id="accountButton" class="account-button" type="button" aria-label="${escapeText(user.display_name || user.username)}账户菜单${pendingRegistrations ? `，${pendingRegistrations}个注册申请待审核` : ""}"><span>${escapeText((user.display_name || user.username).slice(0,1))}</span><b>${escapeText(user.display_name || user.username)}</b>${pendingRegistrations ? `<em class="account-count">${Math.min(pendingRegistrations, 99)}</em>` : ""}</button>
        <div id="accountPopover" class="account-popover hidden">
          <strong>${escapeText(user.display_name)}</strong><small>${escapeText(user.role_labels.join(" · "))}</small>
          ${hasRole("admin") ? `<a class="${route.name === "registrations" ? "current" : ""}" href="/registrations" data-link>注册审核${pendingRegistrations ? `<span class="account-menu-count">${Math.min(pendingRegistrations, 99)}</span>` : ""}</a>` : ""}
          <a href="/change-password" data-link>修改密码</a><button id="logoutButton" type="button">退出登录</button>
        </div>
      </div>
    </nav>`
  const accountButton = shellHeader.querySelector("#accountButton")
  accountButton.addEventListener("click", () => shellHeader.querySelector("#accountPopover").classList.toggle("hidden"))
  shellHeader.querySelector("#logoutButton").addEventListener("click", async () => {
    await api.logout(); clearAuth(); navigate("/login", {replace:true})
  })
}

function setActiveNav(route) {
  const active = route.name === "case-new" ? "new" : route.name === "users" ? "users" : route.name === "notifications" ? "notifications" : route.name === "operations" ? "operations" : route.name === "agent-operations" ? "agent-operations" : route.name === "analytics" ? "analytics" : route.name === "audit" ? "audit" : route.name === "writebacks" ? "writebacks" : route.name === "registrations" ? "" : "cases"
  document.querySelectorAll("[data-nav]").forEach((link) => link.classList.toggle("active", link.dataset.nav === active))
}

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char])
}

window.addEventListener("app:toast", (event) => {
  toast.textContent = event.detail; toast.classList.remove("hidden"); window.setTimeout(() => toast.classList.add("hidden"), 2200)
})
window.addEventListener("app:unauthorized", () => {
  if (!currentAuth().authenticated) return
  clearAuth(); navigate("/login", {replace:true})
})
window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault(); toast.textContent = event.reason?.message || "操作失败"; toast.classList.remove("hidden"); window.setTimeout(() => toast.classList.add("hidden"), 3500)
})

installRouter(render)
render()
