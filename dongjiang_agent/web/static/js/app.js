import {api} from "./api.js"
import {clearAuth, currentAuth, hasRole, loadAuth} from "./auth.js"
import {currentRoute, installRouter, navigate} from "./router.js"
import {renderAuditPage} from "./pages/audit.js"
import {renderChangePasswordPage, renderLoginPage, renderSetupPage} from "./pages/auth.js"
import {renderCasesPage} from "./pages/cases.js"
import {renderNewCasePage} from "./pages/case-new.js"
import {renderCaseDetailPage} from "./pages/case-detail.js"
import {renderCaseActionPage} from "./pages/case-action.js"
import {renderUsersPage} from "./pages/users.js"
import {renderWritebacksPage} from "./pages/writebacks.js"

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
    if (!auth.authenticated && !["login","setup"].includes(route.name)) return navigate("/login", {replace:true})
    if (auth.authenticated && route.name === "login") return navigate("/cases?mine=1", {replace:true})
    if (auth.authenticated && auth.user.must_change_password && route.name !== "change-password") return navigate("/change-password", {replace:true})
    if (route.name === "not-found") return navigate(auth.authenticated ? "/cases" : "/login", {replace:true})

    renderHeader(auth, route)
    setActiveNav(route)
    if (route.name === "login") renderLoginPage(root)
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
  } catch (error) {
    root.innerHTML = `<div class="error-box"><b>页面加载失败</b><p>${escapeText(error.message || error)}</p><a href="/cases" data-link class="secondary">返回案件列表</a></div>`
  } finally {
    loading.classList.add("hidden")
    window.scrollTo({top:0, behavior:"instant"})
  }
}

function renderHeader(auth, route) {
  const publicPage = ["login","setup"].includes(route.name)
  shellHeader.classList.toggle("hidden", publicPage)
  root.classList.toggle("auth-shell", publicPage)
  if (publicPage || !auth.authenticated) return
  const user = auth.user
  shellHeader.innerHTML = `
    <a class="identity" href="/cases" data-link><span class="brand-mark">东江</span><span>信审与合同评审</span></a>
    <nav>
      <a href="/cases" data-link data-nav="cases">案件</a>
      <a href="/cases?mine=1" data-link data-nav="tasks">我的待办</a>
      ${hasRole("sales") ? `<a class="primary small" href="/cases/new" data-link data-nav="new">发起信审</a>` : ""}
      ${hasRole("admin") ? `<a href="/users" data-link data-nav="users">用户</a><a href="/audit" data-link data-nav="audit">审计</a><a href="/writebacks" data-link data-nav="writebacks">回写运维</a>` : ""}
      <div class="account-menu">
        <button id="accountButton" class="account-button" type="button"><span>${escapeText((user.display_name || user.username).slice(0,1))}</span><b>${escapeText(user.display_name || user.username)}</b></button>
        <div id="accountPopover" class="account-popover hidden">
          <strong>${escapeText(user.display_name)}</strong><small>${escapeText(user.role_labels.join(" · "))}</small>
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
  const active = route.name === "case-new" ? "new" : route.name === "users" ? "users" : route.name === "audit" ? "audit" : route.name === "writebacks" ? "writebacks" : route.name === "cases" && route.params.get("mine") === "1" ? "tasks" : "cases"
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
