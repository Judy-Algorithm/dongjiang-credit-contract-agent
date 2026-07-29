import {currentRoute, installRouter, navigate} from "./router.js"
import {renderCasesPage} from "./pages/cases.js"
import {renderNewCasePage} from "./pages/case-new.js"
import {renderCaseDetailPage} from "./pages/case-detail.js"
import {renderCaseActionPage} from "./pages/case-action.js"

const root = document.getElementById("app")
const loading = document.getElementById("globalLoading")
const toast = document.getElementById("toast")

async function render() {
  const route = currentRoute()
  if (route.name === "not-found") {
    navigate("/cases", {replace:true})
    return
  }
  setActiveNav(route)
  loading.classList.remove("hidden")
  root.innerHTML = ""
  try {
    if (route.name === "cases") await renderCasesPage(root, route)
    if (route.name === "case-new") await renderNewCasePage(root, route)
    if (route.name === "case-detail") await renderCaseDetailPage(root, route)
    if (route.name === "case-action") await renderCaseActionPage(root, route)
  } catch (error) {
    root.innerHTML = `
      <div class="error-box">
        <b>页面加载失败</b>
        <p>${escapeText(error.message || error)}</p>
        <a href="/cases" data-link class="secondary">返回案件列表</a>
      </div>`
  } finally {
    loading.classList.add("hidden")
    window.scrollTo({top:0, behavior:"instant"})
  }
}

function setActiveNav(route) {
  const active = route.name === "case-new"
    ? "new"
    : route.name === "cases" && route.params.get("mine") === "1"
      ? "tasks"
      : "cases"
  document.querySelectorAll("[data-nav]").forEach((link) => link.classList.toggle("active", link.dataset.nav === active))
}

function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => (
    {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]
  ))
}

window.addEventListener("app:toast", (event) => {
  toast.textContent = event.detail
  toast.classList.remove("hidden")
  window.setTimeout(() => toast.classList.add("hidden"), 2200)
})

window.addEventListener("unhandledrejection", (event) => {
  event.preventDefault()
  toast.textContent = event.reason?.message || "操作失败"
  toast.classList.remove("hidden")
  window.setTimeout(() => toast.classList.add("hidden"), 3500)
})

installRouter(render)
render()
