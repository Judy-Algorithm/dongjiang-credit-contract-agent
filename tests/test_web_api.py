import base64
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer

from dongjiang_agent.web.server import AuditRequestHandler


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_cwd = os.getcwd()
        self.temp = tempfile.TemporaryDirectory()
        os.chdir(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AuditRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_port,
            timeout=8,
        )
        self.cookie = ""
        self.csrf_token = ""
        status, setup, headers = self.raw_request(
            "POST",
            "/api/auth/setup",
            {
                "display_name": "测试管理员",
                "username": "admin",
                "password": "AdminPass123",
            },
        )
        self.assertEqual(status, 201)
        self.cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
        self.csrf_token = setup["csrf_token"]

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def raw_request(self, method, path, payload=None, extra_headers=None):
        body = None
        headers = dict(extra_headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        self.connection.request(method, path, body=body, headers=headers)
        response = self.connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        return response.status, data, dict(response.getheaders())

    def request(self, method, path, payload=None):
        headers = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if method not in {"GET", "HEAD"} and self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        status, data, _ = self.raw_request(method, path, payload, headers)
        return status, data

    def request_with_headers(self, method, path, payload, headers):
        merged = dict(headers)
        if self.cookie:
            merged["Cookie"] = self.cookie
        if method not in {"GET", "HEAD"} and self.csrf_token:
            merged.setdefault("X-CSRF-Token", self.csrf_token)
        status, data, _ = self.raw_request(method, path, payload, merged)
        return status, data

    def download(self, path):
        headers = {"Cookie": self.cookie} if self.cookie else {}
        self.connection.request("GET", path, headers=headers)
        response = self.connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def login(self, username, password):
        status, data, headers = self.raw_request(
            "POST", "/api/auth/login", {"username": username, "password": password}
        )
        if status == 200:
            self.cookie = headers.get("Set-Cookie", "").split(";", 1)[0]
            self.csrf_token = data["csrf_token"]
        return status, data

    def activate_user(self, username):
        self.assertEqual(self.login(username, "InitialPass123")[0], 200)
        status, _ = self.request(
            "POST",
            "/api/auth/change-password",
            {"current_password": "InitialPass123", "new_password": "ActivePass456"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.login(username, "ActivePass456")[0], 200)

    def create_user(self, username, display_name, roles):
        status, data = self.request(
            "POST",
            "/api/users",
            {
                "username": username,
                "display_name": display_name,
                "password": "InitialPass123",
                "roles": roles,
            },
        )
        self.assertEqual(status, 201)
        return data["user"]

    def test_authentication_csrf_and_role_task_filtering(self):
        sales = self.create_user("sales.a", "销售甲", ["sales"])
        credit = self.create_user("credit.a", "信用甲", ["credit"])

        self.activate_user("sales.a")
        status, denied = self.raw_request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "CSRF测试客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                }
            },
            {"Cookie": self.cookie},
        )[:2]
        self.assertEqual(status, 403)
        self.assertIn("安全令牌", denied["error"])

        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "角色待办客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                },
                "use_cached_credit": False,
            },
        )
        self.assertEqual(status, 201)
        case_id = created["case"]["case_id"]
        self.assertEqual(created["case"]["applicant"]["user_id"], sales["user_id"])

        status, sales_tasks = self.request("GET", "/api/cases?mine=1")
        self.assertEqual(status, 200)
        self.assertEqual(sales_tasks["total"], 0)

        status, forbidden = self.request(
            "POST", f"/api/cases/{case_id}/credit-actions", {"action": "approve"}
        )
        self.assertEqual(status, 403)
        self.assertIn("角色", forbidden["error"])

        self.activate_user("credit.a")
        status, credit_tasks = self.request("GET", "/api/cases?mine=1")
        self.assertEqual(status, 200)
        self.assertEqual(credit_tasks["total"], 1)
        self.assertEqual(credit_tasks["cases"][0]["case_id"], case_id)
        status, approved = self.request(
            "POST", f"/api/cases/{case_id}/credit-actions", {"action": "approve"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(approved["case"]["credit"]["effective"])

        status, detail = self.request("GET", f"/api/cases/{case_id}")
        self.assertEqual(status, 200)
        self.assertIsNone(detail["case"]["next_action"])
        self.assertEqual(detail["case"]["pending_action"]["type"], "upload_contract")

    def test_admin_user_management_and_password_change(self):
        user = self.create_user("finance.a", "财务甲", ["finance"])
        status, updated = self.request(
            "PATCH", f"/api/users/{user['user_id']}", {"roles": ["finance", "legal"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["user"]["roles"], ["finance", "legal"])

        self.assertEqual(self.login("finance.a", "InitialPass123")[0], 200)
        status, changed = self.request(
            "POST",
            "/api/auth/change-password",
            {"current_password": "InitialPass123", "new_password": "UpdatedPass456"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(changed["ok"])
        status, unauthenticated = self.request("GET", "/api/cases")
        self.assertEqual(status, 401)
        self.assertFalse(unauthenticated["ok"])
        self.assertEqual(self.login("finance.a", "UpdatedPass456")[0], 200)

    def test_admin_can_assign_case_owner_to_sales_user(self):
        sales = self.create_user("owner.sales", "案件销售", ["sales"])
        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "负责人改派客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                },
                "use_cached_credit": False,
            },
        )
        self.assertEqual(status, 201)
        case_id = created["case"]["case_id"]
        status, assigned = self.request(
            "PATCH", f"/api/cases/{case_id}", {"owner_user_id": sales["user_id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(assigned["case"]["owner"]["user_id"], sales["user_id"])

    def test_admin_password_reset_invalidates_old_password(self):
        user = self.create_user("legal.reset", "重置法务", ["legal"])
        status, reset = self.request(
            "PATCH",
            f"/api/users/{user['user_id']}",
            {"temporary_password": "ResetPass789"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(reset["user"]["must_change_password"])
        status, denied = self.login("legal.reset", "InitialPass123")
        self.assertEqual(status, 401)
        self.assertFalse(denied["ok"])
        status, authenticated = self.login("legal.reset", "ResetPass789")
        self.assertEqual(status, 200)
        self.assertTrue(authenticated["user"]["must_change_password"])
        status, blocked = self.request("GET", "/api/cases")
        self.assertEqual(status, 403)
        self.assertIn("首次登录", blocked["error"])

    def test_business_api_accepts_real_file_and_exposes_no_workflow_state(self):
        report = (
            "中诚信国际主体评级报告\n"
            "主体评级：AA+\n评级展望：稳定\n评级日期：2026-07-03\n"
            "资产负债率：45%\n流动比率：1.8\n"
        ).encode("utf-8")
        payload = {
            "customer": {
                "customer_name": "文件上传测试客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
            },
            "files": [
                {
                    "name": "中诚信评级报告.txt",
                    "data_base64": base64.b64encode(report).decode("ascii"),
                }
            ],
            "use_cached_credit": False,
        }
        status, created = self.request("POST", "/api/cases", payload)
        self.assertEqual(status, 201)
        self.assertTrue(created["ok"])
        case = created["case"]
        case_id = case["case_id"]
        self.assertEqual(case["status_label"], "等待信用审批")
        self.assertEqual(case["next_action"]["label"], "处理信用审批")
        self.assertFalse(case["permissions"]["can_upload_contract"])
        self.assertEqual(
            case["credit"]["rating_resolution"]["selected"]["agency"],
            "中诚信国际",
        )
        self.assertNotIn("waiting_for", case)
        self.assertNotIn("trace", case)
        self.assertNotIn("workflow", created)

        status, dashboard = self.request("GET", "/api/cases")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["total"], 1)
        self.assertEqual(dashboard["cases"][0]["case_id"], case_id)

        status, detail = self.request("GET", f"/api/cases/{case_id}")
        self.assertEqual(status, 200)
        self.assertIn("data_coverage_ratio", detail["case"]["credit"])
        self.assertIn("supplement_reasons", detail["case"]["credit"])
        self.assertEqual(detail["case"]["customer"]["customer_name"], "文件上传测试客户")

    def test_contract_submission_uses_business_route(self):
        _, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "合同接口测试客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                },
                "use_cached_credit": False,
            },
        )
        case_id = created["case"]["case_id"]
        contract = """销售合同
甲方：东江集团；乙方：合同接口测试客户。
合同标的：精密组件。合同金额：2,000,000元，信用额度：2,000,000元。
付款及账期：月结60天。知识产权：各自所有。保密：不得披露。
违约责任：赔偿直接损失。解除与终止：违约催告后解除。争议解决：深圳法院。
"""
        status, denied = self.request(
            "POST",
            f"/api/cases/{case_id}/contracts",
            {"contract_texts": [contract]},
        )
        self.assertEqual(status, 403)
        self.assertIn("尚未审批生效", denied["error"])

        status, approved = self.request(
            "POST",
            f"/api/cases/{case_id}/credit-actions",
            {"action": "approve", "comment": "信审通过"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(approved["case"]["credit"]["effective"])
        self.assertTrue(approved["case"]["permissions"]["can_upload_contract"])

        status, submitted = self.request(
            "POST",
            f"/api/cases/{case_id}/contracts",
            {"contract_texts": [contract]},
        )
        self.assertEqual(status, 200)
        self.assertTrue(submitted["ok"])
        self.assertEqual(submitted["case"]["status_label"], "已通过")
        self.assertIsNone(submitted["case"]["next_action"])

    def test_overdue_lock_requires_archived_special_release_evidence(self):
        _, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "Web特别放行客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                    "current_overdue_days": 31,
                },
                "use_cached_credit": False,
            },
        )
        case_id = created["case"]["case_id"]
        status, locked = self.request(
            "POST",
            f"/api/cases/{case_id}/credit-actions",
            {"action": "approve", "comment": "信审通过"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(locked["case"]["status"], "credit_control_locked")
        self.assertEqual(locked["case"]["next_action"]["type"], "special_release")
        self.assertFalse(locked["case"]["permissions"]["can_upload_contract"])

        status, denied = self.request(
            "POST",
            f"/api/cases/{case_id}/contract-actions",
            {"action": "approve", "comment": "同意放行"},
        )
        self.assertEqual(status, 400)
        self.assertIn("必须上传审批证据", denied["error"])

        evidence = base64.b64encode("市场总监同意特别放行".encode("utf-8")).decode("ascii")
        status, released = self.request(
            "POST",
            f"/api/cases/{case_id}/contract-actions",
            {
                "action": "approve",
                "comment": "仅放行本次订单",
                "files": [{"name": "特别放行批准.txt", "data_base64": evidence}],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(released["case"]["permissions"]["can_upload_contract"])
        self.assertEqual(len(released["case"]["approval_evidence"]), 1)
        self.assertEqual(released["case"]["approval_evidence"][0]["name"], "特别放行批准.txt")
        self.assertNotIn("archived_path", released["case"]["approval_evidence"][0])

    def test_no_demo_endpoint_and_frontend_routes_support_refresh(self):
        status, missing = self.request("POST", "/api/demo", {})
        self.assertEqual(status, 404)
        self.assertFalse(missing["ok"])

        self.connection.request("GET", "/cases/new")
        response = self.connection.getresponse()
        html = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn('id="app"', html)
        self.assertNotIn("运行风险演示案例", html)
        self.assertNotIn("华南精密制造示例有限公司", html)

    def test_oa_callback_requires_token_and_complete_approval_chain(self):
        _, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "OA回调测试客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "project_name": "OA项目",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                },
                "use_cached_credit": False,
            },
        )
        case_id = created["case"]["case_id"]
        status, _ = self.request(
            "POST", f"/api/integrations/oa/callback/{case_id}", {"action": "approve"}
        )
        self.assertEqual(status, 403)
        previous = os.environ.get("DONGJIANG_OA_CALLBACK_TOKEN")
        os.environ["DONGJIANG_OA_CALLBACK_TOKEN"] = "callback-secret"
        try:
            chain = [
                {"stage": stage, "status": "approved"}
                for stage in (
                    "marketing_director",
                    "credit_control",
                    "senior_finance_manager",
                    "group_finance_director",
                )
            ]
            status, approved = self.request_with_headers(
                "POST",
                f"/api/integrations/oa/callback/{case_id}",
                {
                    "action": "approve",
                    "approval_scope": "客户级TKP信用",
                    "validity_days": 180,
                    "oa_evidence_id": "OA-CALLBACK-1",
                    "approval_chain": chain,
                },
                {"X-OA-Callback-Token": "callback-secret"},
            )
        finally:
            if previous is None:
                os.environ.pop("DONGJIANG_OA_CALLBACK_TOKEN", None)
            else:
                os.environ["DONGJIANG_OA_CALLBACK_TOKEN"] = previous
        self.assertEqual(status, 200)
        self.assertTrue(approved["case"]["credit"]["effective"])

    def test_contract_risk_exposes_line_location_and_controlled_preview(self):
        _, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "证据定位客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                },
                "use_cached_credit": False,
            },
        )
        case_id = created["case"]["case_id"]
        self.assertEqual(
            self.request(
                "POST", f"/api/cases/{case_id}/credit-actions", {"action": "approve"}
            )[0],
            200,
        )
        contract = """销售合同
甲方：东江集团；乙方：证据定位客户。
合同标的：精密组件。合同金额：100万元。信用额度：100万元。
付款及账期：月结60天。知识产权：各自所有。保密：不得披露。
买方可随时取消订单且不承担任何责任。
违约责任：赔偿直接损失。解除与终止：违约催告后解除。争议解决：深圳法院。
"""
        encoded = base64.b64encode(contract.encode("utf-8")).decode("ascii")
        status, submitted = self.request(
            "POST",
            f"/api/cases/{case_id}/contracts",
            {"files": [{"name": "定位合同.txt", "data_base64": encoded}]},
        )
        self.assertEqual(status, 200)
        finding = next(
            item
            for item in submitted["case"]["findings"]
            if item["rule_id"] == "DJ-CANCEL-WITHOUT-LIABILITY"
        )
        self.assertEqual(finding["location"], {"kind": "line", "line": 5})
        self.assertEqual(finding["location_label"], "第 5 行")
        self.assertTrue(finding["document_id"].startswith("DOC-"))

        status, preview = self.request(
            "GET",
            f"/api/cases/{case_id}/documents/{finding['document_id']}?fragment={finding['fragment_id']}",
        )
        self.assertEqual(status, 200)
        selected = next(item for item in preview["fragments"] if item["selected"])
        self.assertIn("取消订单且不承担任何责任", selected["text"])
        self.assertEqual(preview["selected_location_label"], "第 5 行")

        status, missing = self.request(
            "GET", f"/api/cases/{case_id}/documents/DOC-NOT-FOUND?fragment=line-1"
        )
        self.assertEqual(status, 404)
        self.assertFalse(missing["ok"])

    def test_contract_revision_generates_downloads_and_resubmits_clean_version(self):
        _, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "合同修订闭环客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                },
                "use_cached_credit": False,
            },
        )
        case_id = created["case"]["case_id"]
        self.assertEqual(
            self.request(
                "POST", f"/api/cases/{case_id}/credit-actions", {"action": "approve"}
            )[0],
            200,
        )
        contract = """销售合同
甲方：东江集团；乙方：合同修订闭环客户。
合同标的：精密组件。合同金额：100万元。信用额度：100万元。
付款及账期：月结60天。知识产权：各自所有。保密：不得披露。
买方可随时取消订单且不承担任何责任。
违约责任：赔偿直接损失。解除与终止：违约催告后解除。争议解决：深圳法院。
"""
        status, submitted = self.request(
            "POST",
            f"/api/cases/{case_id}/contracts",
            {"contract_texts": [contract]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(submitted["case"]["status"], "blocked")
        finding = next(
            item
            for item in submitted["case"]["findings"]
            if item["rule_id"] == "DJ-CANCEL-WITHOUT-LIABILITY"
        )
        self.assertTrue(finding["finding_key"])
        self.assertIn("提前30日", finding["suggested_replacement"])

        status, created_revision = self.request(
            "POST",
            f"/api/cases/{case_id}/revisions",
            {
                "document_id": finding["document_id"],
                "decisions": [
                    {
                        "finding_key": finding["finding_key"],
                        "action": "accept",
                        "reason": "采用标准取消补偿机制",
                    }
                ],
            },
        )
        self.assertEqual(status, 201)
        revision = created_revision["revision"]
        revision_id = revision["revision_id"]
        self.assertNotIn("path", json.dumps(revision, ensure_ascii=False))

        status, body, headers = self.download(
            f"/api/cases/{case_id}/revisions/{revision_id}/redline"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        self.assertIn("attachment", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        self.assertIn("<w:del", xml)
        self.assertIn("<w:ins", xml)

        status, resubmitted = self.request(
            "POST",
            f"/api/cases/{case_id}/revisions/{revision_id}/submit",
            {},
        )
        self.assertEqual(status, 200)
        self.assertEqual(resubmitted["revision"]["status"], "submitted")
        self.assertEqual(resubmitted["case"]["status"], "approved")
        self.assertFalse(
            any(
                item["rule_id"] == "DJ-CANCEL-WITHOUT-LIABILITY"
                for item in resubmitted["case"]["findings"]
            )
        )
        status, detail = self.request("GET", f"/api/cases/{case_id}")
        self.assertEqual(status, 200)
        self.assertEqual(detail["case"]["contract_revisions"][0]["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
