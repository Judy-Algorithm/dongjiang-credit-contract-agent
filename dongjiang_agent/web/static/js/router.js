export function navigate(path, {replace = false} = {}) {
  if (replace) history.replaceState({}, "", path)
  else history.pushState({}, "", path)
  window.dispatchEvent(new Event("app:navigate"))
}

export function currentRoute() {
  const path = window.location.pathname.replace(/\/+$/, "") || "/"
  const params = new URLSearchParams(window.location.search)
  if (path === "/login") return {name:"login", params}
  if (path === "/register") return {name:"register", params}
  if (path === "/forgot-password") return {name:"forgot-password", params}
  if (path === "/setup") return {name:"setup", params}
  if (path === "/change-password") return {name:"change-password", params}
  if (path === "/users") return {name:"users", params}
  if (path === "/registrations") return {name:"registrations", params}
  if (path === "/notifications") return {name:"notifications", params}
  if (path === "/audit") return {name:"audit", params}
  if (path === "/writebacks") return {name:"writebacks", params}
  if (path === "/" || path === "/cases") return {name:"cases", params}
  if (path === "/cases/new") return {name:"case-new", params}
  const action = path.match(/^\/cases\/([^/]+)\/action$/)
  if (action) return {name:"case-action", caseId:decodeURIComponent(action[1]), params}
  const detail = path.match(/^\/cases\/([^/]+)$/)
  if (detail) return {name:"case-detail", caseId:decodeURIComponent(detail[1]), params}
  return {name:"not-found", params}
}

export function installRouter(render) {
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[data-link]")
    if (!link || link.target || event.metaKey || event.ctrlKey) return
    const url = new URL(link.href)
    if (url.origin !== window.location.origin) return
    event.preventDefault()
    navigate(`${url.pathname}${url.search}`)
  })
  window.addEventListener("popstate", render)
  window.addEventListener("app:navigate", render)
}
