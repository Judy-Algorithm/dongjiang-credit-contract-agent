import {api} from "../api.js?v=20260802-auth-simplified"
import {navigate} from "../router.js?v=20260802-auth-simplified"
import {escapeHtml, dateTime} from "../format.js?v=20260802-auth-simplified"

const categoryLabels = {registration:"账号", case:"案件", writeback:"回写", sla:"时效", agent_incident:"Agent异常", system:"系统"}

export async function renderNotificationsPage(root) {
  const data = await api.listNotifications()
  root.innerHTML = `
    <header class="page-header"><div><h1>通知中心</h1><p>集中查看账号审核、案件流转和系统回写提醒</p></div>${data.unread ? `<button id="readAll" class="secondary">全部标为已读</button>` : ""}</header>
    <section class="panel notification-panel">
      ${data.items.length ? `<div class="notification-list">${data.items.map(notificationRow).join("")}</div>` : `<div class="empty-state"><h2>暂无通知</h2><p>需要你处理的事项和系统结果会显示在这里。</p></div>`}
    </section>`
  root.querySelector("#readAll")?.addEventListener("click", async () => {
    await api.markAllNotificationsRead()
    window.dispatchEvent(new Event("app:navigation-summary-refresh"))
    window.dispatchEvent(new Event("app:navigate"))
  })
  root.querySelectorAll("[data-notification]").forEach((button) => button.addEventListener("click", async () => {
    await api.markNotificationRead(button.dataset.notification)
    window.dispatchEvent(new Event("app:navigation-summary-refresh"))
    const link = button.dataset.notificationLink
    if (link) navigate(link)
    else window.dispatchEvent(new Event("app:navigate"))
  }))
}

function notificationRow(item) {
  return `<button class="notification-row ${item.read_at ? "" : "unread"}" type="button" data-notification="${escapeHtml(item.notification_id)}" data-notification-link="${escapeHtml(item.link || "")}">
    <span class="notification-dot" aria-hidden="true"></span>
    <span class="notification-content"><span class="notification-meta"><b>${escapeHtml(categoryLabels[item.category] || "系统")}</b><time>${dateTime(item.created_at)}</time></span><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.body)}</span></span>
    <span class="notification-arrow">${item.link ? "查看 ›" : item.read_at ? "已读" : "标为已读"}</span>
  </button>`
}
