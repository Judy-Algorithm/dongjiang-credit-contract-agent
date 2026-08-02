from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from ..contract.revisions import ContractRevisionStore, content_disposition
from ..contract.translations import ContractTranslationStore
from ..integrations import IntegrationBundle
from ..operations import (
    AgentIncidentService,
    AgentOperationsService,
    AnalyticsService,
    BenchmarkService,
    ModelHealthService,
    SLAMonitor,
    SLAService,
    agent_incident_view,
)
from ..persistence import CaseRepository
from ..security import AuthStore, SecurityEmailSender
from .presentation import case_summary, case_view


STATIC_ROOT = Path(__file__).with_name("static")
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_REQUEST_BYTES = 30 * 1024 * 1024
SESSION_COOKIE = "dongjiang_session"
WAITING_ROLES = {
    "credit_approval": ["credit_approver"],
    "special_release": ["exception_approver"],
    "manager_approval": ["exception_approver"],
    "finance_legal_review": ["legal_reviewer"],
}
WAITING_LABELS = {
    "credit_approval": "待处理信用审批",
    "credit_supplement": "待补充信用资料",
    "special_release": "待处理特别放行",
    "contract_upload": "待上传合同",
    "sales_revision": "待修改合同",
    "manager_approval": "待处理管理层审批",
    "finance_legal_review": "待处理财务法务复核",
}


class AuthenticationError(PermissionError):
    pass


def _validate_customer(customer: dict[str, Any]) -> None:
    if not str(customer.get("customer_name") or "").strip():
        raise ValueError("请填写客户名称。")
    if customer.get("customer_type") not in {"new", "existing"}:
        raise ValueError("请选择客户类型。")
    if customer.get("business_type") not in {"TKP", "TKM"}:
        raise ValueError("请选择业务类型。")


def _stage_uploads(files: list[dict[str, Any]], target_dir: str | Path) -> list[str]:
    root = Path(target_dir)
    paths: list[str] = []
    total = 0
    for index, item in enumerate(files):
        safe_name = Path(str(item.get("name") or f"upload-{index}.bin")).name
        content = base64.b64decode(str(item.get("data_base64") or ""), validate=True)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(f"{safe_name}超过15MB单文件限制。")
        total += len(content)
        if total > MAX_REQUEST_BYTES:
            raise ValueError("上传文件总大小不能超过30MB。")
        target = root / f"{index:02d}-{safe_name}"
        target.write_bytes(content)
        paths.append(str(target))
    return paths


class AuditRequestHandler(BaseHTTPRequestHandler):
    server_version = "DongjiangAudit/0.3"

    def _json(
        self,
        status: int,
        payload: dict[str, Any] | list[Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, target: Path, filename: str) -> None:
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.send_header("Content-Disposition", content_disposition(filename))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _inline_asset(self, target: Path, filename: str) -> None:
        body = target.read_bytes()
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        suffix = target.suffix.lower()
        fallback = f"document{suffix}" if suffix else "document"
        encoded_name = quote(Path(filename).name)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header(
            "Content-Disposition",
            f"inline; filename={fallback}; filename*=UTF-8''{encoded_name}",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "sandbox")
        self.end_headers()
        self.wfile.write(body)

    def _download(
        self, body: bytes, *, filename: str, content_type: str
    ) -> None:
        safe_name = filename.replace('"', "").replace("\r", "").replace("\n", "")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise ValueError("Content-Length不能为负数。")
        if length > MAX_REQUEST_BYTES:
            raise ValueError("单次请求不能超过30MB。")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _cookie_token(self) -> str:
        cookie = SimpleCookie()
        cookie.load(self.headers.get("Cookie", ""))
        item = cookie.get(SESSION_COOKIE)
        return item.value if item else ""

    def _session(
        self, *, require: bool = True
    ) -> tuple[dict[str, Any], str, str] | None:
        token = self._cookie_token()
        with AuthStore() as store:
            resolved = store.session_user(token)
        if not resolved:
            if require:
                raise AuthenticationError("请先登录。")
            return None
        user, csrf_token = resolved
        return user, csrf_token, token

    def _require_csrf(self, expected: str) -> None:
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(expected, supplied):
            raise PermissionError("安全令牌无效，请刷新页面后重试。")

    @staticmethod
    def _require_roles(user: dict[str, Any], *roles: str) -> None:
        current = set(user.get("roles") or [])
        if not current.intersection(roles):
            raise PermissionError("当前账号没有执行该操作的权限。")

    def _remote_address(self) -> str:
        direct = str(self.client_address[0])
        if ipaddress.ip_address(direct).is_loopback:
            forwarded = self.headers.get("CF-Connecting-IP", "").strip()
            try:
                return str(ipaddress.ip_address(forwarded)) if forwarded else direct
            except ValueError:
                return direct
        return direct

    @staticmethod
    def _actor(user: dict[str, Any]) -> Any:
        from ..workflow import ActorContext

        administrator = dict(user.get("impersonated_by") or {})
        return ActorContext(
            str(user["user_id"]),
            tuple(user.get("roles") or ()),
            "web",
            str(user.get("display_name") or user.get("username") or ""),
            str(administrator.get("user_id") or ""),
            str(administrator.get("display_name") or ""),
        )

    @staticmethod
    def _session_cookie(token: str, *, clear: bool = False) -> str:
        secure = os.getenv("DONGJIANG_COOKIE_SECURE", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        parts = [
            f"{SESSION_COOKIE}={'' if clear else token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if secure:
            parts.append("Secure")
        parts.append("Max-Age=0" if clear else "Max-Age=28800")
        return "; ".join(parts)

    def _workflow_view(self, run: Any, user: dict[str, Any]) -> dict[str, Any]:
        view = case_view(dict(run.state), waiting_for=run.waiting_for, actor=user)
        view["contract_revisions"] = ContractRevisionStore().list(str(view["case_id"]))
        view["contract_translations"] = ContractTranslationStore().list(
            str(view["case_id"])
        )
        return view

    @staticmethod
    def _notify_case_waiting(run: Any, actor: dict[str, Any]) -> None:
        waiting_for = str(run.waiting_for or "")
        if not waiting_for:
            return
        state = dict(run.state)
        customer_name = str((state.get("customer") or {}).get("customer_name") or "客户")
        title = WAITING_LABELS.get(waiting_for, "案件待处理")
        body = f"案件 {run.case_id}（{customer_name}）已流转至：{title}。"
        link = f"/cases/{run.case_id}/action"
        recipients: list[dict[str, Any]] = []
        with AuthStore() as store:
            if waiting_for in {"credit_supplement", "contract_upload", "sales_revision"}:
                owner = dict(state.get("owner") or state.get("applicant") or {})
                owner_id = str(owner.get("user_id") or "")
                if owner_id and owner_id != str(actor.get("user_id") or ""):
                    store.create_notification(
                        owner_id,
                        category="case",
                        title=title,
                        body=body,
                        link=link,
                    )
                    owner_user = store.get_user(owner_id)
                    if owner_user:
                        recipients.append(owner_user)
            else:
                roles = WAITING_ROLES.get(waiting_for)
                if roles:
                    recipients = store.notify_roles(
                        roles,
                        category="case",
                        title=title,
                        body=body,
                        link=link,
                        exclude_user_id=str(actor.get("user_id") or ""),
                    )
        AuditRequestHandler._send_notification_emails(recipients, title, body, link)

    @staticmethod
    def _send_notification_emails(
        recipients: list[dict[str, Any]], title: str, body: str, link: str
    ) -> None:
        sender = SecurityEmailSender()
        if not sender.configured:
            return
        for user in recipients:
            email = str(user.get("email") or "").strip()
            if not email:
                continue
            try:
                sender.send_notification(email, title=title, body=body, link=link)
            except Exception:
                pass

    @staticmethod
    def _notify_failed_writebacks(run: Any) -> None:
        failures: list[str] = []
        for phase, result in (run.state.get("writeback") or {}).items():
            if not isinstance(result, dict):
                continue
            for system in ("oa", "crm", "sap"):
                if str((result.get(system) or {}).get("status") or "") == "failed":
                    failures.append(f"{system.upper()} / {phase}")
        if not failures:
            return
        body = f"案件 {run.case_id} 的 {'、'.join(failures)} 回写失败，请进入回写运维处理。"
        with AuthStore() as store:
            recipients = store.notify_roles(
                ["system_admin"],
                category="writeback",
                title="系统回写失败",
                body=body,
                link="/writebacks",
            )
        AuditRequestHandler._send_notification_emails(
            recipients, "系统回写失败", body, "/writebacks"
        )

    def _static(self, relative: str) -> None:
        requested = relative.lstrip("/")
        is_page_route = (
            not requested
            or requested in {"login", "register", "forgot-password", "setup", "change-password", "cases", "cases/new", "users", "notifications", "operations", "agent-operations", "analytics", "benchmarks", "audit", "writebacks"}
            or (requested.startswith("cases/") and "." not in Path(requested).name)
        )
        name = "index.html" if is_page_route else requested
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT.resolve() not in target.parents or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            f"{mimetypes.guess_type(target.name)[0] or 'application/octet-stream'}; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if target.name == "index.html":
            self.send_header("Cache-Control", "no-store, max-age=0")
        elif "v=" in self.path:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "public, max-age=300")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _case_detail(
        self, case_id: str, user: dict[str, Any]
    ) -> dict[str, Any] | None:
        from ..workflow import DongjiangWorkflowHarness

        try:
            with DongjiangWorkflowHarness() as harness:
                run = harness.get(case_id)
            return self._workflow_view(run, user)
        except KeyError:
            stored = CaseRepository().get_case(case_id)
            if not stored:
                return None
            view = case_view(stored, actor=user)
            view["contract_revisions"] = ContractRevisionStore().list(case_id)
            view["contract_translations"] = ContractTranslationStore().list(case_id)
            return view

    @staticmethod
    def _case_state(case_id: str) -> dict[str, Any]:
        from ..workflow import DongjiangWorkflowHarness

        try:
            with DongjiangWorkflowHarness() as harness:
                return dict(harness.get(case_id).state)
        except KeyError:
            stored = CaseRepository().get_case(case_id)
            if not stored:
                raise KeyError("案件不存在。")
            return stored

    def _document_fragment(
        self,
        case_id: str,
        document_id: str,
        fragment_id: str,
        highlight_query: str = "",
    ) -> dict[str, Any]:
        from ..ingestion import DocumentExtractor, DocumentFragment, location_label
        from ..security import RedactionVault
        from ..workflow import DongjiangWorkflowHarness

        try:
            with DongjiangWorkflowHarness() as harness:
                state = dict(harness.get(case_id).state)
        except KeyError:
            state = CaseRepository().get_case(case_id) or {}
        record = next(
            (
                item
                for item in state.get("source_documents") or []
                if item.get("document_id") == document_id
            ),
            None,
        )
        if not record:
            raise KeyError("文档不存在。")
        target = Path(str(record.get("archived_path") or "")).resolve()
        expected_root = (Path("data/archive") / case_id).resolve()
        if expected_root not in target.parents or not target.is_file():
            raise PermissionError("文档归档路径无效。")
        stored_fragments = list(record.get("fragments") or [])
        if stored_fragments:
            vault = RedactionVault(case_id, "data/vault")
            fragments = [
                DocumentFragment(
                    str(item.get("fragment_id") or ""),
                    vault.restore(str(item.get("text") or "")),
                    dict(item.get("location") or {}),
                )
                for item in stored_fragments
                if item.get("fragment_id")
            ]
            media_type = str(
                record.get("media_type") or target.suffix.lstrip(".")
            ).lower()
            extractor = str(record.get("extractor") or "archive-index")
            warnings = list(record.get("warnings") or [])
        else:
            document = DocumentExtractor().extract(target)
            fragments = document.fragments
            media_type = document.media_type
            extractor = document.extractor
            warnings = document.warnings
        if fragment_id:
            index = next(
                (
                    number
                    for number, item in enumerate(fragments)
                    if item.fragment_id == fragment_id
                ),
                -1,
            )
        else:
            index = 0 if fragments else -1
        if index < 0:
            raise ValueError("文档片段不存在或没有可预览文本。")
        selected = fragments[index]
        context = fragments[max(0, index - 1): min(len(fragments), index + 2)]
        normalized_text = re.sub(r"\s+", "", selected.text).lower()
        normalized_query = re.sub(r"\s+", "", str(highlight_query or "")).lower()
        match_start = normalized_text.find(normalized_query) if normalized_query else -1
        match_end = match_start + len(normalized_query) if match_start >= 0 else -1
        highlight_regions = []
        for region in selected.location.get("ocr_regions") or []:
            start = int(region.get("start") or 0)
            end = int(region.get("end") or 0)
            if match_start >= 0 and end > match_start and start < match_end:
                highlight_regions.append({
                    key: float(region.get(key) or 0)
                    for key in ("x", "y", "width", "height")
                })
        return {
            "document": {
                "document_id": document_id,
                "name": record.get("name"),
                "media_type": media_type,
                "extractor": extractor,
                "warnings": warnings,
            },
            "selected_fragment_id": selected.fragment_id,
            "selected_location": selected.location,
            "selected_location_label": location_label(selected.location),
            "fragments": [
                {
                    "fragment_id": item.fragment_id,
                    "text": item.text,
                    "location": item.location,
                    "location_label": location_label(item.location),
                    "selected": item.fragment_id == selected.fragment_id,
                }
                for item in context
            ],
            "asset_url": (
                f"/api/cases/{case_id}/documents/{document_id}/asset"
                if media_type in {
                    "pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp"
                }
                else ""
            ),
            "asset_kind": (
                "pdf"
                if media_type == "pdf"
                else "image"
                if media_type in {"png", "jpg", "jpeg", "tif", "tiff", "bmp"}
                else ""
            ),
            "page_asset_url": (
                f"/api/cases/{case_id}/documents/{document_id}/page/"
                f"{int(selected.location.get('page') or 1)}"
                if media_type == "pdf"
                else ""
            ),
            "highlight_regions": highlight_regions,
        }

    @staticmethod
    def _document_asset(case_id: str, document_id: str) -> tuple[Path, str]:
        state = AuditRequestHandler._case_state(case_id)
        record = next(
            (
                item
                for item in state.get("source_documents") or []
                if item.get("document_id") == document_id
            ),
            None,
        )
        if not record:
            raise KeyError("文档不存在。")
        target = Path(str(record.get("archived_path") or "")).resolve()
        expected_root = (Path("data/archive") / case_id).resolve()
        if expected_root not in target.parents or not target.is_file():
            raise PermissionError("文档归档路径无效。")
        media_type = str(record.get("media_type") or target.suffix.lstrip(".")).lower()
        if media_type not in {"pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp"}:
            raise ValueError("该文件格式不支持原件内嵌预览。")
        return target, str(record.get("name") or target.name)

    def _document_pdf_page(
        self, case_id: str, document_id: str, page_number: int
    ) -> None:
        from ..ingestion import DocumentExtractor

        target, filename = self._document_asset(case_id, document_id)
        if target.suffix.lower() != ".pdf":
            raise ValueError("只有PDF支持页面渲染。")
        if page_number < 1 or page_number > 2000:
            raise ValueError("PDF页码无效。")
        with tempfile.TemporaryDirectory(prefix="dongjiang-pdf-preview-") as tmp:
            preview = Path(tmp) / f"page-{page_number}.png"
            DocumentExtractor._render_pdf_page(target, page_number, preview)
            self._inline_asset(preview, f"{Path(filename).stem}-page-{page_number}.png")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                self._json(200, {"ok": True, "service": "dongjiang-credit-contract-agent"})
                return
            if path == "/api/auth/status":
                with AuthStore() as store:
                    setup_required = store.setup_required()
                session = self._session(require=False)
                self._json(
                    200,
                    {
                        "ok": True,
                        "setup_required": setup_required,
                        "authenticated": bool(session),
                        "user": session[0] if session else None,
                        "csrf_token": session[1] if session else None,
                    },
                )
                return
            if not path.startswith("/api/"):
                self._static(path)
                return
            session = self._session()
            assert session is not None
            user = session[0]
            if path == "/api/users":
                self._require_roles(user, "system_admin")
                with AuthStore() as store:
                    users = store.list_users()
                self._json(200, {"ok": True, "users": users})
                return
            if path == "/api/notifications":
                with AuthStore() as store:
                    result = store.list_notifications(str(user["user_id"]))
                self._json(200, {"ok": True, **result})
                return
            if path == "/api/navigation-summary":
                with AuthStore() as store:
                    unread = store.unread_notification_count(str(user["user_id"]))
                self._json(
                    200,
                    {
                        "ok": True,
                        "unread_notifications": unread,
                    },
                )
                return
            if path == "/api/auth/impersonation/users":
                administrator = dict(user.get("impersonated_by") or {})
                if not administrator and "system_admin" not in set(user.get("roles") or []):
                    raise PermissionError("只有系统管理员可以切换账户。")
                with AuthStore() as store:
                    users = [
                        item
                        for item in store.list_users()
                        if item.get("active")
                        and "system_admin" not in set(item.get("roles") or [])
                    ]
                self._json(200, {"ok": True, "users": users})
                return
            if path == "/api/audit":
                self._require_roles(user, "system_admin")
                with AuthStore() as store:
                    events = store.list_audit()
                self._json(200, {"ok": True, "events": events})
                return
            if path == "/api/operations/writebacks":
                self._require_roles(user, "system_admin")
                failures: list[dict[str, Any]] = []
                for case in CaseRepository().list_cases():
                    for phase, phase_result in (case.get("writeback") or {}).items():
                        if not isinstance(phase_result, dict):
                            continue
                        for system in ("oa", "crm", "sap"):
                            result = phase_result.get(system) or {}
                            if not isinstance(result, dict) or result.get("status") != "failed":
                                continue
                            failures.append(
                                {
                                    "case_id": case.get("case_id"),
                                    "customer_name": (case.get("customer") or {}).get("customer_name"),
                                    "phase": phase,
                                    "system": system,
                                    "attempted_at": result.get("attempted_at"),
                                    "error": result.get("error"),
                                    "error_type": result.get("error_type"),
                                    "retry_count": len(result.get("retry_history") or []),
                                }
                            )
                failures.sort(key=lambda item: str(item.get("attempted_at") or ""), reverse=True)
                self._json(200, {"ok": True, "failures": failures, "total": len(failures)})
                return
            if path == "/api/operations/sla":
                self._require_roles(user, "system_admin")
                self._json(200, {"ok": True, **SLAService().dashboard()})
                return
            if path == "/api/operations/agents":
                self._require_roles(user, "system_admin")
                self._json(
                    200,
                    {
                        "ok": True,
                        **AgentOperationsService().report(),
                        "model_health": ModelHealthService().summary(),
                    },
                )
                return
            if path == "/api/operations/analytics":
                self._require_roles(user, "system_admin")
                days = parse_qs(parsed.query).get("days", ["30"])[0]
                self._json(200, {"ok": True, **AnalyticsService().report(days=days)})
                return
            if path == "/api/operations/benchmarks":
                self._require_roles(user, "system_admin")
                self._json(200, {"ok": True, **BenchmarkService().summary()})
                return
            if path in {
                "/api/operations/benchmarks/export.csv",
                "/api/operations/benchmarks/export.xlsx",
            }:
                self._require_roles(user, "system_admin")
                service = BenchmarkService()
                report = service.summary().get("latest")
                if not report:
                    raise ValueError("尚未运行质量评测，暂无可导出的报告。")
                if path.endswith(".csv"):
                    self._download(
                        service.export_csv(report),
                        filename=f"dongjiang-quality-benchmark-{report['run_id']}.csv",
                        content_type="text/csv; charset=utf-8",
                    )
                else:
                    self._download(
                        service.export_xlsx(report),
                        filename=f"dongjiang-quality-benchmark-{report['run_id']}.xlsx",
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                return
            if path in {
                "/api/operations/analytics/export.csv",
                "/api/operations/analytics/export.xlsx",
            }:
                self._require_roles(user, "system_admin")
                days = parse_qs(parsed.query).get("days", ["30"])[0]
                service = AnalyticsService()
                report = service.report(days=days)
                suffix = "all" if report["range_days"] == 0 else f"{report['range_days']}d"
                if path.endswith(".csv"):
                    self._download(
                        service.export_csv(report),
                        filename=f"dongjiang-approval-analytics-{suffix}.csv",
                        content_type="text/csv; charset=utf-8",
                    )
                else:
                    self._download(
                        service.export_xlsx(report),
                        filename=f"dongjiang-approval-analytics-{suffix}.xlsx",
                        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                return
            if path == "/api/cases":
                cases = CaseRepository().list_cases()
                rows = [case_summary(item, actor=user) for item in cases[:100]]
                mine = parse_qs(parsed.query).get("mine", [""])[0] == "1"
                if mine:
                    rows = [item for item in rows if item["is_my_task"]]
                self._json(200, {"ok": True, "cases": rows, "total": len(rows)})
                return
            parts = [item for item in path.split("/") if item]
            if (
                len(parts) == 7
                and parts[:2] == ["api", "cases"]
                and parts[3] == "documents"
                and parts[5] == "page"
            ):
                self._document_pdf_page(parts[2], parts[4], int(parts[6]))
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "cases"]
                and parts[3] == "documents"
                and parts[5] == "asset"
            ):
                target, filename = self._document_asset(parts[2], parts[4])
                self._inline_asset(target, filename)
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "cases"]
                and parts[3] == "documents"
            ):
                query = parse_qs(parsed.query)
                fragment_id = query.get("fragment", [""])[0]
                highlight = query.get("highlight", [""])[0][:500]
                payload = self._document_fragment(
                    parts[2], parts[4], fragment_id, highlight
                )
                self._json(200, {"ok": True, **payload})
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "cases"]
                and parts[3] == "revisions"
            ):
                target, filename = ContractRevisionStore().artifact_path(
                    parts[2], parts[4], parts[5]
                )
                self._file(target, filename)
                return
            if (
                len(parts) == 5
                and parts[:2] == ["api", "cases"]
                and parts[3] == "translations"
            ):
                detail = ContractTranslationStore().detail(parts[2], parts[4])
                self._json(200, {"ok": True, "translation": detail})
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "cases"]
                and parts[3] == "translations"
                and parts[5] == "download"
            ):
                target, filename = ContractTranslationStore().artifact_path(
                    parts[2], parts[4]
                )
                self._file(target, filename)
                return
            if path.startswith("/api/cases/"):
                case_id = path.rsplit("/", 1)[-1]
                case = self._case_detail(case_id, user)
                if case is None:
                    self._json(404, {"ok": False, "error": "案件不存在"})
                else:
                    self._json(200, {"ok": True, "case": case})
                return
            self._json(404, {"ok": False, "error": "API 不存在"})
        except AuthenticationError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc).strip("'")})
        except PermissionError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/auth/setup":
                with AuthStore() as store:
                    user = store.bootstrap_admin(
                        str(payload.get("username") or ""),
                        str(payload.get("display_name") or ""),
                        str(payload.get("password") or ""),
                        self._remote_address(),
                        str(payload.get("email") or ""),
                    )
                    session = store.create_session(str(user["user_id"]))
                self._json(
                    201,
                    {"ok": True, "user": user, "csrf_token": session["csrf_token"]},
                    headers={"Set-Cookie": self._session_cookie(session["token"])},
                )
                return
            if path == "/api/auth/register":
                with AuthStore() as store:
                    user = store.register_user(
                        username=str(payload.get("username") or ""),
                        display_name=str(payload.get("display_name") or ""),
                        email=str(payload.get("email") or ""),
                        password=str(payload.get("password") or ""),
                        verification_code=str(payload.get("verification_code") or ""),
                        remote_address=self._remote_address(),
                    )
                self._json(
                    201,
                    {"ok": True, "user": user, "message": "注册成功，请使用新账号登录。"},
                )
                return
            if path == "/api/auth/registration-code":
                sender = SecurityEmailSender()
                if not sender.configured:
                    raise RuntimeError("邮件服务尚未配置，请联系管理员。")
                with AuthStore() as store:
                    issued = store.issue_registration_verification_code(
                        str(payload.get("email") or ""), self._remote_address()
                    )
                if issued:
                    try:
                        sender.send_registration_code(*issued)
                    except Exception:
                        with AuthStore() as store:
                            store.invalidate_latest_registration_code(issued[0])
                        raise
                self._json(200, {"ok": True, "message": "验证码已发送，请检查邮箱。"})
                return
            if path == "/api/auth/password-reset/request":
                email = str(payload.get("email") or "")
                sender = SecurityEmailSender()
                if not sender.configured:
                    raise RuntimeError("邮件服务尚未配置，请联系管理员重置密码。")
                with AuthStore() as store:
                    normalized = store.normalize_email(email)
                    issued = store.issue_password_reset_code(
                        normalized, self._remote_address()
                    )
                if issued:
                    try:
                        sender.send_password_reset_code(*issued)
                    except Exception:
                        with AuthStore() as store:
                            store.invalidate_latest_password_reset_code(issued[0])
                        raise
                self._json(
                    200,
                    {"ok": True, "message": "如果该邮箱已绑定账号，验证码邮件将在几分钟内送达。"},
                )
                return
            if path == "/api/auth/password-reset/confirm":
                with AuthStore() as store:
                    store.reset_password_with_code(
                        str(payload.get("email") or ""),
                        str(payload.get("code") or ""),
                        str(payload.get("new_password") or ""),
                        self._remote_address(),
                    )
                self._json(200, {"ok": True})
                return
            if path == "/api/auth/login":
                with AuthStore() as store:
                    user = store.authenticate(
                        str(payload.get("username") or ""),
                        str(payload.get("password") or ""),
                        self._remote_address(),
                    )
                    if not user:
                        raise AuthenticationError("用户名或密码不正确。")
                    session = store.create_session(str(user["user_id"]))
                self._json(
                    200,
                    {"ok": True, "user": user, "csrf_token": session["csrf_token"]},
                    headers={"Set-Cookie": self._session_cookie(session["token"])},
                )
                return
            if path.startswith("/api/integrations/oa/callback/"):
                self._oa_callback(path.rsplit("/", 1)[-1], payload)
                return
            session = self._session()
            assert session is not None
            user, csrf_token, token = session
            self._require_csrf(csrf_token)
            if path == "/api/auth/logout":
                with AuthStore() as store:
                    store.delete_session(token)
                    store.audit("auth.logout", actor=user, remote_address=self._remote_address())
                self._json(
                    200,
                    {"ok": True},
                    headers={"Set-Cookie": self._session_cookie("", clear=True)},
                )
                return
            if path == "/api/auth/impersonate":
                administrator = dict(user.get("impersonated_by") or {})
                if not administrator:
                    self._require_roles(user, "system_admin")
                    administrator = dict(user)
                target_user_id = str(payload.get("user_id") or "")
                with AuthStore() as store:
                    target = store.start_impersonation(token, target_user_id)
                    store.audit(
                        "auth.impersonation_started",
                        actor=administrator,
                        target_type="user",
                        target_id=str(target["user_id"]),
                        detail={
                            "effective_username": target.get("username"),
                            "effective_role": (target.get("roles") or [""])[0],
                        },
                        remote_address=self._remote_address(),
                    )
                    resolved = store.session_user(token)
                assert resolved is not None
                effective_user, effective_csrf = resolved
                self._json(
                    200,
                    {
                        "ok": True,
                        "user": effective_user,
                        "csrf_token": effective_csrf,
                    },
                )
                return
            if path == "/api/auth/impersonation/stop":
                administrator = dict(user.get("impersonated_by") or {})
                if not administrator:
                    raise PermissionError("当前未处于管理员模拟状态。")
                with AuthStore() as store:
                    restored = store.stop_impersonation(token)
                    store.audit(
                        "auth.impersonation_stopped",
                        actor=restored,
                        target_type="user",
                        target_id=str(user.get("user_id") or ""),
                        detail={"effective_username": user.get("username")},
                        remote_address=self._remote_address(),
                    )
                    resolved = store.session_user(token)
                assert resolved is not None
                restored_user, restored_csrf = resolved
                self._json(
                    200,
                    {
                        "ok": True,
                        "user": restored_user,
                        "csrf_token": restored_csrf,
                    },
                )
                return
            if path == "/api/auth/change-password":
                if user.get("impersonated_by"):
                    raise PermissionError("管理员模拟期间不能修改目标账号密码。")
                with AuthStore() as store:
                    store.change_password(
                        str(user["user_id"]),
                        str(payload.get("current_password") or ""),
                        str(payload.get("new_password") or ""),
                        user,
                        self._remote_address(),
                    )
                self._json(
                    200,
                    {"ok": True},
                    headers={"Set-Cookie": self._session_cookie("", clear=True)},
                )
                return
            if path == "/api/notifications/read-all":
                with AuthStore() as store:
                    store.mark_all_notifications_read(str(user["user_id"]))
                self._json(200, {"ok": True})
                return
            if path == "/api/operations/sla/sweep":
                self._require_roles(user, "system_admin")
                result = SLAService().sweep()
                with AuthStore() as store:
                    store.audit(
                        "operations.sla_sweep",
                        actor=user,
                        target_type="operations",
                        detail=result,
                        remote_address=self._remote_address(),
                    )
                self._json(200, result)
                return
            if path == "/api/operations/agents/sweep":
                self._require_roles(user, "system_admin")
                result = AgentIncidentService().sweep()
                with AuthStore() as store:
                    store.audit(
                        "operations.agent_incident_sweep",
                        actor=user,
                        target_type="operations",
                        detail={
                            "examined_cases": result.get("examined_cases"),
                            "opened_incidents": result.get("opened_incidents"),
                            "notifications_created": result.get(
                                "notifications_created"
                            ),
                            "policy_version": result.get("policy_version"),
                        },
                        remote_address=self._remote_address(),
                    )
                self._json(200, result)
                return
            if path == "/api/operations/model/probe":
                self._require_roles(user, "system_admin")
                result = ModelHealthService().probe()
                with AuthStore() as store:
                    store.audit(
                        "operations.model_probe",
                        actor=user,
                        target_type="model_health",
                        detail={
                            "status": (result.get("latest") or {}).get("status"),
                            "model": result.get("model"),
                            "http_status": (result.get("latest") or {}).get(
                                "http_status"
                            ),
                        },
                        remote_address=self._remote_address(),
                    )
                self._json(200, {"ok": True, **result})
                return
            if path == "/api/operations/benchmarks/run":
                self._require_roles(user, "system_admin")
                repeats = int(payload.get("repeats") or 2)
                report = BenchmarkService().run(repeats=repeats)
                with AuthStore() as store:
                    store.audit(
                        "operations.benchmark_run",
                        actor=user,
                        target_type="benchmark",
                        target_id=str(report.get("run_id") or ""),
                        detail={
                            "suite_id": (report.get("suite") or {}).get("suite_id"),
                            "suite_version": (report.get("suite") or {}).get("version"),
                            "case_total": (report.get("metrics") or {}).get("case_total"),
                            "case_passed": (report.get("metrics") or {}).get("case_passed"),
                            "status": report.get("status"),
                            "repeat_count": report.get("repeat_count"),
                            "external_ai": (report.get("runtime") or {}).get("external_ai"),
                            "enterprise_integrations": (report.get("runtime") or {}).get("enterprise_integrations"),
                        },
                        remote_address=self._remote_address(),
                    )
                self._json(200, {"ok": True, "report": report})
                return
            parts = [item for item in path.split("/") if item]
            if len(parts) == 4 and parts[:2] == ["api", "notifications"] and parts[3] == "read":
                with AuthStore() as store:
                    store.mark_notification_read(str(user["user_id"]), parts[2])
                self._json(200, {"ok": True})
                return
            if path == "/api/users":
                self._require_roles(user, "system_admin")
                with AuthStore() as store:
                    created = store.create_user(
                        username=str(payload.get("username") or ""),
                        display_name=str(payload.get("display_name") or ""),
                        password=str(payload.get("password") or ""),
                        email=str(payload.get("email") or ""),
                        roles=list(payload.get("roles") or []),
                        must_change_password=False,
                        actor=user,
                        remote_address=self._remote_address(),
                    )
                self._json(201, {"ok": True, "user": created})
                return
            if path == "/api/cases":
                self._create_case(payload, user)
                return
            parts = [item for item in path.split("/") if item]
            if len(parts) == 4 and parts[:2] == ["api", "cases"]:
                case_id, resource = parts[2], parts[3]
                if resource in {"credit-documents", "credit-actions", "contracts", "contract-actions"}:
                    self._handle_case_action(case_id, resource, payload, user)
                    return
                if resource == "revisions":
                    self._create_contract_revision(case_id, payload, user)
                    return
                if resource == "translations":
                    self._create_contract_translation(case_id, payload, user)
                    return
                if resource == "structured-extractions":
                    self._generate_structured_extraction(case_id, payload, user)
                    return
                if resource == "structured-extraction-actions":
                    self._manage_structured_extraction(case_id, payload, user)
                    return
                if resource == "mock-enterprise-approval":
                    self._run_mock_enterprise_approval(case_id, payload, user)
                    return
                if resource == "writeback-retries":
                    self._retry_writeback(case_id, payload, user)
                    return
                if resource == "agent-incidents":
                    self._manage_agent_incident(case_id, payload, user)
                    return
                if resource == "agent-candidate-actions":
                    self._manage_agent_candidate(case_id, payload, user)
                    return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "cases"]
                and parts[3] == "revisions"
                and parts[5] == "submit"
            ):
                self._submit_contract_revision(parts[2], parts[4], user)
                return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "cases"]
                and parts[3] == "translations"
                and parts[5] == "confirm"
            ):
                self._confirm_contract_translation(
                    parts[2], parts[4], payload, user
                )
                return
            self._json(404, {"ok": False, "error": "API 不存在"})
        except AuthenticationError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc).strip("'") or "资源不存在"})
        except PermissionError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})

    def do_PATCH(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            session = self._session()
            assert session is not None
            user, csrf_token, _ = session
            self._require_csrf(csrf_token)
            parts = [item for item in path.split("/") if item]
            if len(parts) == 3 and parts[:2] == ["api", "users"]:
                self._require_roles(user, "system_admin")
                with AuthStore() as store:
                    temporary_password = str(
                        payload.get("temporary_password") or ""
                    ).strip()
                    if temporary_password:
                        store.validate_password(temporary_password)
                    updated = store.update_user(
                        parts[2],
                        username=str(payload["username"]) if "username" in payload else None,
                        display_name=(
                            str(payload["display_name"])
                            if "display_name" in payload
                            else None
                        ),
                        roles=list(payload["roles"]) if "roles" in payload else None,
                        active=bool(payload["active"]) if "active" in payload else None,
                        email=str(payload["email"]) if "email" in payload else None,
                        actor=user,
                        remote_address=self._remote_address(),
                    )
                    if temporary_password:
                        store.reset_password(
                            parts[2],
                            temporary_password,
                            actor=user,
                            remote_address=self._remote_address(),
                        )
                        updated = store.get_user(parts[2]) or updated
                self._json(200, {"ok": True, "user": updated})
                return
            if len(parts) == 3 and parts[:2] == ["api", "cases"]:
                self._require_roles(user, "system_admin")
                owner_id = str(payload.get("owner_user_id") or "")
                with AuthStore() as store:
                    owner = store.get_user(owner_id)
                    if not owner or not owner.get("active"):
                        raise ValueError("请选择启用的业务经办人。")
                    if "case_submitter" not in set(owner.get("roles") or []):
                        raise ValueError("案件负责人必须是业务经办人。")
                    from ..workflow import DongjiangWorkflowHarness

                    with DongjiangWorkflowHarness() as harness:
                        run = harness.assign_owner(
                            parts[2],
                            {
                                "user_id": owner["user_id"],
                                "display_name": owner["display_name"],
                            },
                        )
                    store.audit(
                        "case.owner_assigned",
                        actor=user,
                        target_type="case",
                        target_id=parts[2],
                        detail={"owner_user_id": owner["user_id"]},
                        remote_address=self._remote_address(),
                    )
                self._json(200, {"ok": True, "case": self._workflow_view(run, user)})
                return
            self._json(404, {"ok": False, "error": "API 不存在"})
        except AuthenticationError as exc:
            self._json(401, {"ok": False, "error": str(exc)})
        except KeyError as exc:
            self._json(404, {"ok": False, "error": str(exc).strip("'")})
        except PermissionError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(400, {"ok": False, "error": str(exc), "error_type": type(exc).__name__})

    def _oa_callback(self, case_id: str, payload: dict[str, Any]) -> None:
        from ..workflow import ActorContext, DongjiangWorkflowHarness

        expected = os.getenv("DONGJIANG_OA_CALLBACK_TOKEN", "").strip()
        supplied = self.headers.get("X-OA-Callback-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            raise PermissionError("OA回调鉴权失败。")
        if not isinstance(payload.get("approval_chain"), list):
            raise ValueError("OA回调必须包含完整 approval_chain。")
        decision = {
            "action": payload.get("action"),
            "approved_credit_limit": payload.get("approved_credit_limit"),
            "approved_term_days": payload.get("approved_term_days"),
            "purchase_exemption_approved": payload.get("purchase_exemption_approved"),
            "approval_scope": payload.get("approval_scope"),
            "validity_days": payload.get("validity_days"),
            "oa_evidence_id": payload.get("oa_evidence_id"),
            "approval_chain": payload.get("approval_chain"),
            "comment": payload.get("comment"),
            "candidate_adoption_request_id": str(
                payload.get("candidate_adoption_request_id") or ""
            ),
        }
        with DongjiangWorkflowHarness() as harness:
            run = harness.resume(
                case_id,
                decision,
                actor=ActorContext(
                    str(payload.get("actor_id") or "oa-callback"),
                    ("credit_approver",),
                    "oa",
                    "OA审批",
                ),
            )
        self._notify_case_waiting(run, {"user_id": str(payload.get("actor_id") or "oa-callback")})
        self._notify_failed_writebacks(run)
        self._json(200, {"ok": True, "case": case_view(dict(run.state), waiting_for=run.waiting_for)})

    def _create_case(self, payload: dict[str, Any], user: dict[str, Any]) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "case_submitter")
        customer = dict(payload.get("customer") or {})
        _validate_customer(customer)
        with tempfile.TemporaryDirectory(prefix="dongjiang-case-upload-") as temp_dir:
            paths = _stage_uploads(payload.get("files") or [], temp_dir)
            with DongjiangWorkflowHarness() as harness:
                run = harness.start(
                    customer,
                    file_paths=paths,
                    use_cached_credit=bool(payload.get("use_cached_credit", False)),
                    actor=self._actor(user),
                )
        with AuthStore() as store:
            store.audit(
                "case.created",
                actor=user,
                target_type="case",
                target_id=run.case_id,
                remote_address=self._remote_address(),
            )
        self._notify_case_waiting(run, user)
        self._notify_failed_writebacks(run)
        self._json(201, {"ok": True, "case": self._workflow_view(run, user)})

    def _create_contract_revision(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        self._require_roles(user, "case_submitter", "legal_reviewer")
        actor = {
            "actor_id": user.get("user_id"),
            "display_name": user.get("display_name") or user.get("username"),
        }
        state = self._case_state(case_id)
        if state.get("waiting_for") != "sales_revision":
            raise ValueError("当前案件不在等待合同修订状态。")
        revision = ContractRevisionStore().create(
            state,
            document_id=str(payload.get("document_id") or ""),
            decisions=list(payload.get("decisions") or []),
            actor=actor,
        )
        with AuthStore() as store:
            store.audit(
                "contract.revision_created",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={
                    "revision_id": revision["revision_id"],
                    "decision_count": len(revision.get("decisions") or []),
                },
                remote_address=self._remote_address(),
            )
        self._json(201, {"ok": True, "revision": revision})

    def _create_contract_translation(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        self._require_roles(user, "case_submitter", "legal_reviewer")
        actor = {
            "actor_id": user.get("user_id"),
            "display_name": user.get("display_name") or user.get("username"),
        }
        translation = ContractTranslationStore().create(
            self._case_state(case_id),
            document_id=str(payload.get("document_id") or ""),
            target_language=str(payload.get("target_language") or ""),
            target_languages=list(payload.get("target_languages") or []),
            base_translation_id=str(payload.get("base_translation_id") or ""),
            actor=actor,
        )
        target_languages = list(translation.get("target_languages") or [])
        if not target_languages and translation.get("target_language"):
            target_languages = [str(translation["target_language"])]
        with AuthStore() as store:
            store.audit(
                "contract.translation_created",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={
                    "translation_id": translation["translation_id"],
                    "document_id": translation["document_id"],
                    "target_languages": target_languages,
                    "segment_count": translation["segment_count"],
                    "reused_translation_count": int(
                        translation.get("reused_translation_count") or 0
                    ),
                    "translated_translation_count": int(
                        translation.get("translated_translation_count") or 0
                    ),
                    "model": translation["model"],
                },
                remote_address=self._remote_address(),
            )
        self._json(201, {"ok": True, "translation": translation})

    def _confirm_contract_translation(
        self,
        case_id: str,
        translation_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        self._require_roles(user, "case_submitter", "legal_reviewer")
        actor = {
            "actor_id": user.get("user_id"),
            "display_name": user.get("display_name") or user.get("username"),
        }
        translation = ContractTranslationStore().confirm(
            case_id,
            translation_id,
            entries=list(payload.get("entries") or []),
            review_note=str(payload.get("review_note") or ""),
            actor=actor,
        )
        target_languages = list(translation.get("target_languages") or [])
        if not target_languages and translation.get("target_language"):
            target_languages = [str(translation["target_language"])]
        with AuthStore() as store:
            store.audit(
                "contract.translation_confirmed",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={
                    "translation_id": translation_id,
                    "target_languages": target_languages,
                    "segment_count": translation["segment_count"],
                },
                remote_address=self._remote_address(),
            )
        self._json(200, {"ok": True, "translation": translation})

    def _retry_writeback(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "system_admin")
        phase = str(payload.get("phase") or "").strip()
        system = str(payload.get("system") or "").strip().lower()
        if not phase or system not in {"oa", "crm", "sap"}:
            raise ValueError("请选择有效的回写阶段和目标系统。")
        actor = self._actor(user)
        run = None
        with DongjiangWorkflowHarness() as harness:
            try:
                harness.get(case_id)
            except KeyError:
                pass
            else:
                run = harness.retry_writeback(
                    case_id,
                    phase=phase,
                    system=system,
                    actor=actor,
                )
        if run is not None:
            result = dict((run.state.get("writeback") or {}).get(phase, {}).get(system) or {})
            view = self._workflow_view(run, user)
        else:
            repository = CaseRepository()
            state = repository.get_case(case_id)
            if not state:
                raise KeyError("案件不存在。")
            writeback = dict(state.get("writeback") or {})
            phase_result = dict(writeback.get(phase) or {})
            existing = dict(phase_result.get(system) or {})
            if not phase_result:
                raise KeyError("回写阶段不存在。")
            if str(existing.get("status") or "") != "failed":
                raise ValueError("只有失败的回写记录可以重试。")
            customer_id, request_payload = DongjiangWorkflowHarness._writeback_payload(
                state, phase
            )
            result = IntegrationBundle.from_environment().retry_writeback(
                case_id,
                system,
                customer_id,
                request_payload,
                phase=phase,
            )
            retry_history = list(existing.get("retry_history") or [])
            retry_history.append(
                {
                    **dict(result),
                    "actor_id": actor.actor_id,
                    "actor_name": actor.display_name or actor.actor_id,
                }
            )
            phase_result[system] = {**dict(result), "retry_history": retry_history}
            result = dict(phase_result[system])
            writeback[phase] = phase_result
            state["writeback"] = writeback
            state.setdefault("trace", []).append(
                {
                    "ts": result.get("attempted_at"),
                    "stage": "integration.writeback_retried",
                    "message": f"{system.upper()} {phase} 回写已人工重试。",
                    "data": {
                        "phase": phase,
                        "system": system,
                        "status": result.get("status"),
                        "actor_id": actor.actor_id,
                    },
                }
            )
            repository.save_dict(state)
            view = case_view(state, actor=user)
        with AuthStore() as store:
            store.audit(
                "integration.writeback_retried",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={"phase": phase, "system": system, "status": result.get("status")},
                remote_address=self._remote_address(),
            )
        self._json(200, {"ok": True, "result": result, "case": view})

    def _manage_agent_incident(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "system_admin")
        action = str(payload.get("action") or "").strip()
        plan_id = str(payload.get("plan_id") or "").strip()
        note = str(payload.get("note") or "")
        task_id = str(payload.get("task_id") or "").strip()
        if not plan_id:
            raise ValueError("请选择需要处置的Agent计划。")
        assignee: dict[str, Any] = {}
        assignee_user_id = str(payload.get("assignee_user_id") or "").strip()
        recipients: list[dict[str, Any]] = []
        with AuthStore() as store:
            if action == "assign":
                target = store.get_user(assignee_user_id)
                if not target or not target.get("active"):
                    raise ValueError("异常责任人不存在或已停用。")
                assignee = {
                    "user_id": target["user_id"],
                    "display_name": target.get("display_name") or target.get("username"),
                }
        with DongjiangWorkflowHarness() as harness:
            _run, incident = harness.manage_agent_incident(
                case_id,
                plan_id=plan_id,
                action=action,
                actor=self._actor(user),
                note=note,
                assignee=assignee,
                task_id=task_id,
            )
        link = f"/cases/{case_id}?tab=agents"
        with AuthStore() as store:
            store.audit(
                f"agent.incident.{action}",
                actor=user,
                target_type="agent_incident",
                target_id=str(incident.get("incident_id") or ""),
                detail={
                    "case_id": case_id,
                    "plan_id": plan_id,
                    "task_id": task_id,
                    "assignee_user_id": assignee.get("user_id"),
                    "status": incident.get("status"),
                    "official_state_changed": False,
                },
                remote_address=self._remote_address(),
            )
            if action == "assign" and assignee.get("user_id"):
                notification = store.create_notification(
                    str(assignee["user_id"]),
                    category="agent_incident",
                    title="Agent运行异常已分派",
                    body=f"案件 {case_id} 的Agent运行异常已分派给你处理。",
                    link=link,
                    dedupe_key=(
                        f"agent-incident:{incident.get('incident_id')}:"
                        f"assign:{assignee.get('user_id')}:{incident.get('updated_at')}"
                    ),
                )
                if notification:
                    recipients = [assignee]
            elif action in {"rerun", "resolve"}:
                recipients = store.notify_roles(
                    ["system_admin"],
                    category="agent_incident",
                    title=(
                        "Agent候选重跑已完成"
                        if action == "rerun"
                        else "Agent运行异常已关闭"
                    ),
                    body=f"案件 {case_id} 的Agent异常处置状态已更新。",
                    link=link,
                    exclude_user_id=str(user.get("user_id") or ""),
                )
        if recipients:
            self._send_notification_emails(
                recipients,
                "Agent运行异常处置",
                f"案件 {case_id} 的Agent异常状态已更新。",
                link,
            )
        self._json(
            200,
            {
                "ok": True,
                "incident": agent_incident_view(incident),
                "case_id": case_id,
            },
        )

    def _manage_agent_candidate(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness
        from ..workflow.candidate_reviews import safe_candidate_review_view

        self._require_roles(
            user, "credit_approver", "legal_reviewer", "exception_approver"
        )
        action = str(payload.get("action") or "").strip()
        incident_id = str(payload.get("incident_id") or "").strip()
        request_id = str(payload.get("request_id") or "").strip()
        if not incident_id:
            raise ValueError("请选择需要处置的Agent候选。")
        with DongjiangWorkflowHarness() as harness:
            run, review = harness.manage_agent_candidate(
                case_id,
                incident_id=incident_id,
                action=action,
                actor=self._actor(user),
                reason=str(payload.get("reason") or ""),
                request_id=request_id,
            )
        link = f"/cases/{case_id}?tab=agents"
        review_view = safe_candidate_review_view(review)
        with AuthStore() as store:
            store.audit(
                f"agent.candidate.{action}",
                actor=user,
                target_type="agent_candidate_review",
                target_id=str(review.get("request_id") or ""),
                detail={
                    "case_id": case_id,
                    "incident_id": incident_id,
                    "plan_id": review.get("plan_id"),
                    "agent": review.get("agent"),
                    "status": review.get("status"),
                    "official_state_changed": False,
                    "reason_recorded": bool(str(review.get("reason") or "").strip()),
                },
                remote_address=self._remote_address(),
            )
            roles = list(
                WAITING_ROLES.get(str(review.get("eligible_waiting_for") or ""))
                or ["system_admin"]
            )
            title = (
                "Agent候选等待正式审批"
                if action == "request_adoption"
                else "Agent候选已拒绝"
            )
            recipients = store.notify_roles(
                roles,
                category="agent_candidate",
                title=title,
                body=f"案件 {case_id} 的Agent候选处置状态已更新。",
                link=link,
                exclude_user_id=str(user.get("user_id") or ""),
            )
            self_notification = store.create_notification(
                str(user.get("user_id") or ""),
                category="agent_candidate",
                title=title,
                body=f"案件 {case_id} 的Agent候选处置状态已更新。",
                link=link,
                dedupe_key=(
                    f"agent-candidate:{review.get('request_id')}:"
                    f"{action}:{review.get('status')}"
                ),
            )
            if self_notification:
                recipients.append(user)
        self._send_notification_emails(
            recipients,
            title,
            f"案件 {case_id} 的Agent候选处置状态已更新。",
            link,
        )
        self._json(
            200,
            {
                "ok": True,
                "review": review_view,
                "case": self._workflow_view(run, user),
            },
        )

    def _generate_structured_extraction(
        self, case_id: str, payload: dict[str, Any], user: dict[str, Any]
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(
            user, "credit_approver", "legal_reviewer", "exception_approver"
        )
        document_kind = str(payload.get("document_kind") or "")
        with DongjiangWorkflowHarness() as harness:
            run, extraction = harness.generate_structured_extraction(
                case_id,
                document_kind=document_kind,
                actor=self._actor(user),
            )
        with AuthStore() as store:
            store.audit(
                "agent.extraction.generated",
                actor=user,
                target_type="structured_extraction",
                target_id=str(extraction.get("extraction_id") or ""),
                detail={
                    "case_id": case_id,
                    "document_kind": document_kind,
                    "candidate_count": len(extraction.get("candidates") or []),
                    "verification_status": (
                        extraction.get("verification") or {}
                    ).get("status"),
                    "official_state_changed": False,
                },
                remote_address=self._remote_address(),
            )
        self._json(
            200,
            {"ok": True, "extraction": extraction, "case": self._workflow_view(run, user)},
        )

    def _manage_structured_extraction(
        self, case_id: str, payload: dict[str, Any], user: dict[str, Any]
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(
            user, "credit_approver", "legal_reviewer", "exception_approver"
        )
        action = str(payload.get("action") or "")
        with DongjiangWorkflowHarness() as harness:
            run, extraction = harness.manage_structured_extraction(
                case_id,
                extraction_id=str(payload.get("extraction_id") or ""),
                action=action,
                actor=self._actor(user),
                candidate_ids=[
                    str(item) for item in payload.get("candidate_ids") or []
                ],
                reason=str(payload.get("reason") or ""),
            )
        with AuthStore() as store:
            store.audit(
                f"agent.extraction.{action}",
                actor=user,
                target_type="structured_extraction",
                target_id=str(extraction.get("extraction_id") or ""),
                detail={
                    "case_id": case_id,
                    "document_kind": extraction.get("document_kind"),
                    "candidate_count": len(
                        (extraction.get("decision") or {}).get("candidate_ids") or []
                    ),
                    "official_state_changed": bool(
                        (extraction.get("decision") or {}).get(
                            "official_state_changed"
                        )
                    ),
                },
                remote_address=self._remote_address(),
            )
        self._json(
            200,
            {"ok": True, "extraction": extraction, "case": self._workflow_view(run, user)},
        )

    def _run_mock_enterprise_approval(
        self, case_id: str, payload: dict[str, Any], user: dict[str, Any]
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "credit_approver")
        if os.getenv("DONGJIANG_INTEGRATION_MODE", "").strip().lower() != "mock":
            raise PermissionError("企业系统Mock模式未启用。")
        with DongjiangWorkflowHarness() as harness:
            current = harness.get(case_id)
            if current.waiting_for != "credit_approval":
                raise ValueError("Mock OA审批仅支持等待信用审批的案件。")
            assessment = dict(current.state.get("credit_assessment") or {})
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            oa_evidence_id = f"MOCK-OA-EVIDENCE-{case_id}"
            chain = [
                item
                for item in current.state.get("approval_chain") or []
                if str(item.get("stage") or "") == "applicant"
            ] + [
                {
                    "stage": stage,
                    "status": "approved",
                    "acted_at": now,
                    "actor": {
                        "actor_id": f"mock-{stage}",
                        "display_name": label,
                        "source_system": "mock-oa",
                    },
                    "comment": "比赛演示Mock审批通过",
                    "oa_evidence_id": oa_evidence_id,
                }
                for stage, label in (
                    ("marketing_director", "Mock所属市场总监"),
                    ("credit_control", "Mock信用管理"),
                    ("senior_finance_manager", "Mock高级财务经理"),
                    ("group_finance_director", "Mock集团财务总监"),
                )
            ]
            run = harness.resume(
                case_id,
                {
                    "action": "approve",
                    "approved_credit_limit": assessment.get(
                        "approved_credit_limit"
                    ),
                    "approved_term_days": assessment.get(
                        "recommended_term_days"
                    ),
                    "purchase_exemption_approved": bool(
                        assessment.get("purchase_exemption_requested")
                    ),
                    "approval_scope": str(payload.get("approval_scope") or "")
                    or f"Mock比赛演示案件 {case_id}",
                    "validity_days": int(payload.get("validity_days") or 180),
                    "oa_evidence_id": oa_evidence_id,
                    "approval_chain": chain,
                    "comment": "Mock OA完整审批链自动回调",
                },
                actor=self._actor(user),
            )
        with AuthStore() as store:
            store.audit(
                "integration.mock_enterprise_approval",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={
                    "mode": "mock",
                    "approval_chain_count": len(chain),
                    "oa_evidence_id": oa_evidence_id,
                    "credit_status": run.state.get("credit_status"),
                    "official_state_changed": True,
                },
                remote_address=self._remote_address(),
            )
        self._notify_case_waiting(run, user)
        self._notify_failed_writebacks(run)
        self._json(
            200,
            {
                "ok": True,
                "mode": "mock",
                "case": self._workflow_view(run, user),
            },
        )

    def _submit_contract_revision(
        self,
        case_id: str,
        revision_id: str,
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "case_submitter")
        store = ContractRevisionStore()
        manifest = store.get(case_id, revision_id)
        if manifest.get("status") != "draft":
            raise ValueError("该修订版本已经提交，不能重复送审。")
        clean_path, _ = store.artifact_path(case_id, revision_id, "clean")
        with DongjiangWorkflowHarness() as harness:
            current = harness.get(case_id)
            self._assert_owner(current, user)
            if current.waiting_for != "sales_revision":
                raise ValueError("当前案件不在等待合同修订状态。")
            run = harness.resume(
                case_id,
                {"action": "submit_revision", "file_paths": [str(clean_path)]},
                actor=self._actor(user),
            )
        result = {
            "status": run.status,
            "decision": run.state.get("decision"),
            "waiting_for": run.waiting_for,
            "finding_count": sum(
                len(item.get("findings") or [])
                for item in run.state.get("contract_reviews") or []
            ),
        }
        revision = store.mark_submitted(case_id, revision_id, result)
        with AuthStore() as auth:
            auth.audit(
                "contract.revision_submitted",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={"revision_id": revision_id, **result},
                remote_address=self._remote_address(),
            )
        self._notify_case_waiting(run, user)
        self._notify_failed_writebacks(run)
        self._json(
            200,
            {
                "ok": True,
                "revision": revision,
                "case": self._workflow_view(run, user),
            },
        )

    @staticmethod
    def _assert_owner(current: Any, user: dict[str, Any]) -> None:
        if current.waiting_for not in {"credit_supplement", "contract_upload", "sales_revision"}:
            return
        owner = dict(current.state.get("owner") or current.state.get("applicant") or {})
        if owner.get("user_id") and owner.get("user_id") != user.get("user_id"):
            raise PermissionError("该业务任务已分配给其他负责人。")

    def _handle_case_action(
        self,
        case_id: str,
        resource: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        with tempfile.TemporaryDirectory(prefix="dongjiang-case-action-") as temp_dir:
            paths = _stage_uploads(payload.get("files") or [], temp_dir)
            with DongjiangWorkflowHarness() as harness:
                current = harness.get(case_id)
                if resource in {"credit-documents", "contracts"}:
                    self._require_roles(user, "case_submitter")
                elif resource == "credit-actions":
                    if current.waiting_for == "credit_approval":
                        self._require_roles(user, "credit_approver")
                    elif current.waiting_for == "credit_supplement":
                        self._require_roles(user, "case_submitter")
                elif resource == "contract-actions":
                    required_role = {
                        "contract_upload": "case_submitter",
                        "sales_revision": "case_submitter",
                        "manager_approval": "exception_approver",
                        "special_release": "exception_approver",
                        "finance_legal_review": "legal_reviewer",
                    }.get(str(current.waiting_for or ""))
                    if required_role:
                        self._require_roles(user, required_role)
                self._assert_owner(current, user)
                if resource == "credit-documents":
                    if current.waiting_for != "credit_supplement":
                        raise ValueError("当前案件不需要补充信用资料。")
                    if not paths:
                        raise ValueError("请至少上传一份信用资料。")
                    action = "submit_supplement"
                elif resource == "credit-actions":
                    requested = str(payload.get("action") or "")
                    if current.waiting_for == "credit_approval":
                        if requested not in {"approve", "adjust_and_approve", "request_supplement", "reject"}:
                            raise ValueError("不支持的信用审批操作。")
                        action = requested
                    elif current.waiting_for == "credit_supplement" and requested == "close_case":
                        action = requested
                    else:
                        raise ValueError("当前案件不支持该信用操作。")
                elif resource == "contracts":
                    if current.state.get("credit_status") != "effective" or not current.state.get("effective_credit_assessment"):
                        raise PermissionError("信审结果尚未审批生效，不能上传合同。")
                    if current.waiting_for not in {"contract_upload", "sales_revision"}:
                        raise ValueError("当前案件不需要上传或修改合同。")
                    if not paths and not payload.get("contract_texts"):
                        raise ValueError("请上传合同文件或粘贴合同正文。")
                    action = "submit_revision" if current.waiting_for == "sales_revision" else "submit_contract"
                else:
                    action = self._resolve_business_action(
                        current.waiting_for, str(payload.get("action") or ""), paths
                    )
                decision = {
                    "action": action,
                    "comment": str(payload.get("comment") or ""),
                    "approved_credit_limit": payload.get("approved_credit_limit"),
                    "approved_term_days": payload.get("approved_term_days"),
                    "purchase_exemption_approved": payload.get("purchase_exemption_approved"),
                    "approval_scope": payload.get("approval_scope"),
                    "validity_days": payload.get("validity_days"),
                    "oa_evidence_id": payload.get("oa_evidence_id"),
                    "approval_chain": payload.get("approval_chain") or [],
                    "file_paths": paths,
                    "contract_texts": payload.get("contract_texts") or [],
                    "candidate_adoption_request_id": str(
                        payload.get("candidate_adoption_request_id") or ""
                    ),
                }
                run = harness.resume(case_id, decision, actor=self._actor(user))
        with AuthStore() as store:
            store.audit(
                "case.action",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={
                    "resource": resource,
                    "action": action,
                    "candidate_adoption_request_id": str(
                        payload.get("candidate_adoption_request_id") or ""
                    ),
                },
                remote_address=self._remote_address(),
            )
            candidate_request_id = str(
                payload.get("candidate_adoption_request_id") or ""
            )
            candidate_review = next(
                (
                    item
                    for item in run.state.get("agent_candidate_reviews") or []
                    if str(item.get("request_id") or "") == candidate_request_id
                ),
                None,
            )
            if candidate_review and candidate_review.get("status") == "approved":
                store.audit(
                    "agent.candidate.approved",
                    actor=user,
                    target_type="agent_candidate_review",
                    target_id=candidate_request_id,
                    detail={
                        "case_id": case_id,
                        "incident_id": candidate_review.get("incident_id"),
                        "plan_id": candidate_review.get("plan_id"),
                        "agent": candidate_review.get("agent"),
                        "waiting_for": (candidate_review.get("decision") or {}).get(
                            "waiting_for"
                        ),
                        "official_state_changed": True,
                    },
                    remote_address=self._remote_address(),
                )
                requester_id = str(
                    (candidate_review.get("requested_by") or {}).get("user_id") or ""
                )
                if requester_id:
                    store.create_notification(
                        requester_id,
                        category="agent_candidate",
                        title="Agent候选已由正式审批采纳",
                        body=f"案件 {case_id} 的Agent候选已在正式审批节点生效。",
                        link=f"/cases/{case_id}?tab=agents",
                        dedupe_key=f"agent-candidate:{candidate_request_id}:approved",
                    )
        self._notify_case_waiting(run, user)
        self._notify_failed_writebacks(run)
        self._json(200, {"ok": True, "case": self._workflow_view(run, user)})

    @staticmethod
    def _resolve_business_action(
        waiting_for: str | None, requested: str, file_paths: list[str]
    ) -> str:
        action_map = {
            "contract_upload": {"close_case": "close_case"},
            "sales_revision": {"close_case": "close_case"},
            "manager_approval": {"approve": "approve", "reject": "reject"},
            "special_release": {"approve": "approve", "reject": "reject"},
            "finance_legal_review": {
                "approve": "approve",
                "supplement": "supplement",
                "request_revision": "revise_contract",
            },
        }
        action = action_map.get(str(waiting_for), {}).get(requested)
        if action is None:
            raise ValueError("当前案件不支持该操作。")
        if action == "supplement" and not file_paths:
            raise ValueError("补充资料时请至少上传一个文件。")
        return action

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[web] {self.address_string()} {fmt % args}")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), AuditRequestHandler)
    monitor = None
    if os.getenv("DONGJIANG_SLA_MONITOR_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        monitor = SLAMonitor()
        monitor.start()
    print(f"东江一体化信审与合同评审：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if monitor:
            monitor.stop()
            monitor.join(timeout=3)
        server.server_close()


def main() -> None:
    from ..config import load_local_env

    load_local_env()
    serve()


if __name__ == "__main__":
    main()
