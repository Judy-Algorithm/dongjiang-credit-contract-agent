import {api} from "../api.js?v=20260731-auth"
import {dateTime, escapeHtml} from "../format.js?v=20260731-auth"

const labels = {
  "auth.login":"登录","auth.logout":"退出登录","auth.password_changed":"修改密码","auth.password_reset":"管理员重置密码",
  "auth.password_reset_requested":"请求找回密码","auth.password_reset_confirmed":"确认找回密码","user.registered":"提交注册申请",
  "user.created":"创建用户","user.updated":"调整用户","case.created":"发起信审","case.action":"处理案件",
  "case.owner_assigned":"调整负责人",
  "integration.writeback_retried":"重试系统回写",
}

export async function renderAuditPage(root) {
  const data = await api.listAudit()
  root.innerHTML = `<header class="page-header"><div><h1>安全审计</h1><p>账号、权限和关键业务操作记录</p></div></header>
    <section class="panel"><div class="table-scroll"><table>
      <thead><tr><th>时间</th><th>事件</th><th>操作人</th><th>目标</th><th>结果</th></tr></thead>
      <tbody>${data.events.map((item) => `<tr><td>${dateTime(item.created_at)}</td><td><b>${escapeHtml(labels[item.event_type] || item.event_type)}</b></td><td>${escapeHtml(item.username || "系统")}</td><td>${escapeHtml(item.target_id || "—")}</td><td><span class="badge ${item.outcome === "success" ? "approved" : item.outcome === "failed" ? "high" : "pending"}">${escapeHtml(item.outcome)}</span></td></tr>`).join("")}</tbody>
    </table></div></section>`
}
