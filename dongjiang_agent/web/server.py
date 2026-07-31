from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import mimetypes
import os
import tempfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..contract.revisions import ContractRevisionStore, content_disposition
from ..integrations import IntegrationBundle
from ..persistence import CaseRepository
from ..security import AuthStore, SecurityEmailSender
from .presentation import case_summary, case_view


STATIC_ROOT = Path(__file__).with_name("static")
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_REQUEST_BYTES = 30 * 1024 * 1024
SESSION_COOKIE = "dongjiang_session"


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

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
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
        if "admin" not in current and not current.intersection(roles):
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

        return ActorContext(
            str(user["user_id"]),
            tuple(user.get("roles") or ()),
            "web",
            str(user.get("display_name") or user.get("username") or ""),
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
        return view

    def _static(self, relative: str) -> None:
        requested = relative.lstrip("/")
        is_page_route = (
            not requested
            or requested in {"login", "register", "forgot-password", "setup", "change-password", "cases", "cases/new", "users", "audit", "writebacks"}
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
        self.send_header("Cache-Control", "no-store, max-age=0")
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
    ) -> dict[str, Any]:
        from ..ingestion import DocumentExtractor, location_label
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
        document = DocumentExtractor().extract(target)
        fragments = document.fragments
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
        return {
            "document": {
                "document_id": document_id,
                "name": record.get("name"),
                "media_type": document.media_type,
                "extractor": document.extractor,
                "warnings": document.warnings,
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
        }

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
            if user.get("must_change_password"):
                raise PermissionError("首次登录必须先修改初始密码。")
            if path == "/api/users":
                self._require_roles(user, "admin")
                with AuthStore() as store:
                    users = store.list_users()
                self._json(200, {"ok": True, "users": users})
                return
            if path == "/api/audit":
                self._require_roles(user, "admin")
                with AuthStore() as store:
                    events = store.list_audit()
                self._json(200, {"ok": True, "events": events})
                return
            if path == "/api/operations/writebacks":
                self._require_roles(user, "admin")
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
                len(parts) == 5
                and parts[:2] == ["api", "cases"]
                and parts[3] == "documents"
            ):
                fragment_id = parse_qs(parsed.query).get("fragment", [""])[0]
                payload = self._document_fragment(parts[2], parts[4], fragment_id)
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
                        remote_address=self._remote_address(),
                    )
                self._json(
                    201,
                    {"ok": True, "user": user, "message": "注册申请已提交，请等待管理员启用账号。"},
                )
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
            if path == "/api/auth/change-password":
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
            if user.get("must_change_password"):
                raise PermissionError("首次登录必须先修改初始密码。")
            if path == "/api/users":
                self._require_roles(user, "admin")
                with AuthStore() as store:
                    created = store.create_user(
                        username=str(payload.get("username") or ""),
                        display_name=str(payload.get("display_name") or ""),
                        password=str(payload.get("password") or ""),
                        email=str(payload.get("email") or ""),
                        roles=list(payload.get("roles") or []),
                        must_change_password=True,
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
                if resource == "writeback-retries":
                    self._retry_writeback(case_id, payload, user)
                    return
            if (
                len(parts) == 6
                and parts[:2] == ["api", "cases"]
                and parts[3] == "revisions"
                and parts[5] == "submit"
            ):
                self._submit_contract_revision(parts[2], parts[4], user)
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
            if user.get("must_change_password"):
                raise PermissionError("首次登录必须先修改初始密码。")
            parts = [item for item in path.split("/") if item]
            if len(parts) == 3 and parts[:2] == ["api", "users"]:
                self._require_roles(user, "admin")
                with AuthStore() as store:
                    if "temporary_password" in payload:
                        store.reset_password(
                            parts[2],
                            str(payload.get("temporary_password") or ""),
                            actor=user,
                            remote_address=self._remote_address(),
                        )
                    updated = store.update_user(
                        parts[2],
                        roles=list(payload["roles"]) if "roles" in payload else None,
                        active=bool(payload["active"]) if "active" in payload else None,
                        email=str(payload["email"]) if "email" in payload else None,
                        actor=user,
                        remote_address=self._remote_address(),
                    )
                self._json(200, {"ok": True, "user": updated})
                return
            if len(parts) == 3 and parts[:2] == ["api", "cases"]:
                self._require_roles(user, "admin")
                owner_id = str(payload.get("owner_user_id") or "")
                with AuthStore() as store:
                    owner = store.get_user(owner_id)
                    if not owner or not owner.get("active"):
                        raise ValueError("请选择启用的销售用户。")
                    if "sales" not in set(owner.get("roles") or []):
                        raise ValueError("案件负责人必须具有销售角色。")
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
        }
        with DongjiangWorkflowHarness() as harness:
            run = harness.resume(
                case_id,
                decision,
                actor=ActorContext(str(payload.get("actor_id") or "oa-callback"), ("finance",), "oa", "OA审批"),
            )
        self._json(200, {"ok": True, "case": case_view(dict(run.state), waiting_for=run.waiting_for)})

    def _create_case(self, payload: dict[str, Any], user: dict[str, Any]) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "sales")
        customer = dict(payload.get("customer") or {})
        _validate_customer(customer)
        with tempfile.TemporaryDirectory(prefix="dongjiang-case-upload-") as temp_dir:
            paths = _stage_uploads(payload.get("files") or [], temp_dir)
            with DongjiangWorkflowHarness() as harness:
                run = harness.start(
                    customer,
                    file_paths=paths,
                    use_cached_credit=bool(payload.get("use_cached_credit", True)),
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
        self._json(201, {"ok": True, "case": self._workflow_view(run, user)})

    def _create_contract_revision(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        self._require_roles(user, "sales", "legal")
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

    def _retry_writeback(
        self,
        case_id: str,
        payload: dict[str, Any],
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "admin")
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

    def _submit_contract_revision(
        self,
        case_id: str,
        revision_id: str,
        user: dict[str, Any],
    ) -> None:
        from ..workflow import DongjiangWorkflowHarness

        self._require_roles(user, "sales")
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
        if "admin" in set(user.get("roles") or []):
            return
        if current.waiting_for not in {"credit_supplement", "contract_upload", "sales_revision"}:
            return
        owner = dict(current.state.get("owner") or current.state.get("applicant") or {})
        if owner.get("user_id") and owner.get("user_id") != user.get("user_id"):
            raise PermissionError("该销售任务已分配给其他负责人。")

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
                }
                run = harness.resume(case_id, decision, actor=self._actor(user))
        with AuthStore() as store:
            store.audit(
                "case.action",
                actor=user,
                target_type="case",
                target_id=case_id,
                detail={"resource": resource, "action": action},
                remote_address=self._remote_address(),
            )
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
    print(f"东江一体化信审与合同评审：http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> None:
    from ..config import load_local_env

    load_local_env()
    serve()


if __name__ == "__main__":
    main()
