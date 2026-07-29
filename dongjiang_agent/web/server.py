from __future__ import annotations

import base64
import json
import mimetypes
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..persistence import CaseRepository
from .presentation import case_summary, case_view


STATIC_ROOT = Path(__file__).with_name("static")
MAX_FILE_BYTES = 15 * 1024 * 1024
MAX_REQUEST_BYTES = 30 * 1024 * 1024


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
    server_version = "DongjiangAudit/0.2"

    def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST_BYTES:
            raise ValueError("单次请求不能超过30MB。")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    @staticmethod
    def _workflow_view(run: Any) -> dict[str, Any]:
        return case_view(dict(run.state), waiting_for=run.waiting_for)

    def _static(self, relative: str) -> None:
        requested = relative.lstrip("/")
        is_page_route = (
            not requested
            or requested == "cases"
            or requested == "cases/new"
            or requested == "rules"
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
        self.end_headers()
        self.wfile.write(body)

    def _case_detail(self, case_id: str) -> dict[str, Any] | None:
        from ..workflow import DongjiangWorkflowHarness

        try:
            with DongjiangWorkflowHarness() as harness:
                run = harness.get(case_id)
            return self._workflow_view(run)
        except KeyError:
            stored = CaseRepository().get_case(case_id)
            return case_view(stored) if stored else None

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json(200, {"ok": True, "service": "dongjiang-credit-contract-agent"})
            return
        if path == "/api/cases":
            cases = CaseRepository().list_cases()
            self._json(
                200,
                {
                    "ok": True,
                    "cases": [case_summary(item) for item in cases[:100]],
                    "total": len(cases),
                },
            )
            return
        if path.startswith("/api/cases/"):
            case_id = path.rsplit("/", 1)[-1]
            case = self._case_detail(case_id)
            if case is None:
                self._json(404, {"ok": False, "error": "案件不存在"})
            else:
                self._json(200, {"ok": True, "case": case})
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/cases":
                self._create_case(payload)
                return

            parts = [item for item in path.split("/") if item]
            if len(parts) == 4 and parts[:2] == ["api", "cases"]:
                case_id, resource = parts[2], parts[3]
                if resource in {
                    "credit-documents",
                    "credit-actions",
                    "contracts",
                    "contract-actions",
                }:
                    self._handle_case_action(case_id, resource, payload)
                    return
            self._json(404, {"ok": False, "error": "API 不存在"})
        except KeyError:
            self._json(404, {"ok": False, "error": "案件不存在"})
        except PermissionError as exc:
            self._json(403, {"ok": False, "error": str(exc)})
        except Exception as exc:
            self._json(
                400,
                {"ok": False, "error": str(exc), "error_type": type(exc).__name__},
            )

    def _create_case(self, payload: dict[str, Any]) -> None:
        from ..workflow import ActorContext, DongjiangWorkflowHarness

        customer = dict(payload.get("customer") or {})
        _validate_customer(customer)
        with tempfile.TemporaryDirectory(prefix="dongjiang-case-upload-") as temp_dir:
            paths = _stage_uploads(payload.get("files") or [], temp_dir)
            with DongjiangWorkflowHarness() as harness:
                run = harness.start(
                    customer,
                    file_paths=paths,
                    use_cached_credit=bool(payload.get("use_cached_credit", True)),
                    actor=ActorContext("web-sales", ("sales",), "web"),
                )
        self._json(201, {"ok": True, "case": self._workflow_view(run)})

    def _handle_case_action(
        self,
        case_id: str,
        resource: str,
        payload: dict[str, Any],
    ) -> None:
        from ..workflow import ActorContext, DongjiangWorkflowHarness

        with tempfile.TemporaryDirectory(prefix="dongjiang-case-action-") as temp_dir:
            paths = _stage_uploads(payload.get("files") or [], temp_dir)
            with DongjiangWorkflowHarness() as harness:
                current = harness.get(case_id)
                if resource == "credit-documents":
                    if current.waiting_for != "credit_supplement":
                        raise ValueError("当前案件不需要补充信用资料。")
                    if not paths:
                        raise ValueError("请至少上传一份信用资料。")
                    action = "submit_supplement"
                    actor = ActorContext("web-sales", ("sales",), "web")
                elif resource == "credit-actions":
                    requested = str(payload.get("action") or "")
                    if current.waiting_for == "credit_approval":
                        if requested not in {
                            "approve",
                            "adjust_and_approve",
                            "request_supplement",
                            "reject",
                        }:
                            raise ValueError("不支持的信用审批操作。")
                        action = requested
                        actor = ActorContext("web-credit", ("credit",), "web")
                    elif (
                        current.waiting_for == "credit_supplement"
                        and requested == "close_case"
                    ):
                        action = requested
                        actor = ActorContext("web-sales", ("sales",), "web")
                    else:
                        raise ValueError("当前案件不支持该信用操作。")
                elif resource == "contracts":
                    if (
                        current.state.get("credit_status") != "effective"
                        or not current.state.get("effective_credit_assessment")
                    ):
                        raise PermissionError("信审结果尚未审批生效，不能上传合同。")
                    if current.waiting_for not in {"contract_upload", "sales_revision"}:
                        raise ValueError("当前案件不需要上传或修改合同。")
                    if not paths and not payload.get("contract_texts"):
                        raise ValueError("请上传合同文件或粘贴合同正文。")
                    action = (
                        "submit_revision"
                        if current.waiting_for == "sales_revision"
                        else "submit_contract"
                    )
                    actor = ActorContext("web-sales", ("sales",), "web")
                else:
                    action, actor = self._resolve_business_action(
                        current.waiting_for,
                        str(payload.get("action") or ""),
                        paths,
                    )
                decision = {
                    "action": action,
                    "comment": str(payload.get("comment") or ""),
                    "approved_credit_limit": payload.get("approved_credit_limit"),
                    "approved_term_days": payload.get("approved_term_days"),
                    "file_paths": paths,
                    "contract_texts": payload.get("contract_texts") or [],
                }
                run = harness.resume(case_id, decision, actor=actor)
        self._json(200, {"ok": True, "case": self._workflow_view(run)})

    @staticmethod
    def _resolve_business_action(
        waiting_for: str | None,
        requested: str,
        file_paths: list[str],
    ) -> tuple[str, Any]:
        from ..workflow import ActorContext

        action_map = {
            "contract_upload": {"close_case": ("close_case", "sales")},
            "sales_revision": {"close_case": ("close_case", "sales")},
            "manager_approval": {
                "approve": ("approve", "director"),
                "reject": ("reject", "director"),
            },
            "finance_legal_review": {
                "approve": ("approve", "finance"),
                "supplement": ("supplement", "finance"),
                "request_revision": ("revise_contract", "finance"),
            },
        }
        translated = action_map.get(str(waiting_for), {}).get(requested)
        if translated is None:
            raise ValueError("当前案件不支持该操作。")
        action, role = translated
        if action == "supplement" and not file_paths:
            raise ValueError("补充资料时请至少上传一个文件。")
        return action, ActorContext(f"web-{role}", (role,), "web")

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
