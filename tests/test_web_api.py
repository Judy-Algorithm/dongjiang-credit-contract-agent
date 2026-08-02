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
from pathlib import Path
from unittest.mock import ANY, patch
from urllib.parse import quote

from dongjiang_agent.integrations import IntegrationBundle
from dongjiang_agent.security import AuthStore
from dongjiang_agent.web.server import AuditRequestHandler
from dongjiang_agent.workflow import DongjiangWorkflowHarness


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
        self.create_user("credit.a", "信用甲", ["credit"])

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
        self.assertIn("没有执行该操作的权限", forbidden["error"])

        self.activate_user("credit.a")
        status, credit_notices = self.request("GET", "/api/notifications")
        self.assertEqual(status, 200)
        self.assertEqual(credit_notices["unread"], 1)
        self.assertEqual(credit_notices["items"][0]["link"], f"/cases/{case_id}/action")
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

        self.assertEqual(self.login("sales.a", "ActivePass456")[0], 200)
        status, sales_notices = self.request("GET", "/api/notifications")
        self.assertEqual(status, 200)
        self.assertEqual(sales_notices["unread"], 1)
        self.assertEqual(sales_notices["items"][0]["link"], f"/cases/{case_id}/action")

    def test_new_case_reuses_credit_only_when_explicitly_requested(self):
        self.create_user("sales.cache", "复用测试业务", ["sales"])
        self.create_user("credit.cache", "复用测试信用", ["credit"])
        self.activate_user("sales.cache")
        customer = {
            "customer_name": "有效授信复用测试客户",
            "unified_social_credit_code": "91440300CACHE000001",
            "customer_type": "new",
            "business_type": "TKP",
            "monthly_order_amount": 1_000_000,
            "external_rating": "AA",
            "asset_liability_ratio": 0.45,
            "current_ratio": 1.5,
        }
        status, first = self.request(
            "POST", "/api/cases", {"customer": customer, "use_cached_credit": False}
        )
        self.assertEqual(status, 201)

        self.activate_user("credit.cache")
        status, approved = self.request(
            "POST",
            f"/api/cases/{first['case']['case_id']}/credit-actions",
            {"action": "approve"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(approved["case"]["credit"]["effective"])

        self.assertEqual(self.login("sales.cache", "ActivePass456")[0], 200)
        status, independent = self.request("POST", "/api/cases", {"customer": customer})
        self.assertEqual(status, 201)
        self.assertEqual(independent["case"]["status"], "credit_pending_approval")
        self.assertFalse(independent["case"]["credit"]["effective"])

        status, reused = self.request(
            "POST", "/api/cases", {"customer": customer, "use_cached_credit": True}
        )
        self.assertEqual(status, 201)
        self.assertEqual(reused["case"]["status"], "awaiting_contract")
        self.assertTrue(reused["case"]["credit"]["effective"])

    def test_admin_user_management_rejects_multiple_roles_and_password_change(self):
        user = self.create_user("finance.a", "财务甲", ["credit"])
        status, updated = self.request(
            "PATCH",
            f"/api/users/{user['user_id']}",
            {"roles": ["credit_approver", "legal_reviewer"]},
        )
        self.assertEqual(status, 400)
        self.assertIn("单个角色", updated["error"])

        status, updated = self.request(
            "PATCH",
            f"/api/users/{user['user_id']}",
            {
                "display_name": "信用审批昵称",
                "username": "credit.renamed",
                "email": "credit-renamed@example.com",
                "roles": ["credit_approver"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["user"]["display_name"], "信用审批昵称")
        self.assertEqual(updated["user"]["username"], "credit.renamed")
        self.assertEqual(updated["user"]["email"], "credit-renamed@example.com")

        self.assertEqual(self.login("finance.a", "InitialPass123")[0], 401)
        self.assertEqual(self.login("credit.renamed", "InitialPass123")[0], 200)
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
        self.assertEqual(self.login("credit.renamed", "UpdatedPass456")[0], 200)

    def test_admin_can_switch_accounts_with_persistent_audited_impersonation(self):
        sales = self.create_user("sales.switch", "切换业务", ["sales"])
        credit = self.create_user("credit.switch", "切换信用", ["credit"])
        self.activate_user("sales.switch")

        status, denied = self.request("GET", "/api/auth/impersonation/users")
        self.assertEqual(status, 403)
        self.assertIn("系统管理员", denied["error"])

        self.activate_user("credit.switch")
        self.assertEqual(self.login("admin", "AdminPass123")[0], 200)
        status, users = self.request("GET", "/api/auth/impersonation/users")
        self.assertEqual(status, 200)
        self.assertEqual(
            {item["username"] for item in users["users"]},
            {"sales.switch", "credit.switch"},
        )

        status, switched = self.request(
            "POST", "/api/auth/impersonate", {"user_id": sales["user_id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(switched["user"]["username"], "sales.switch")
        self.assertEqual(switched["user"]["impersonated_by"]["username"], "admin")
        self.csrf_token = switched["csrf_token"]

        status, refreshed = self.request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertEqual(refreshed["user"]["username"], "sales.switch")
        self.assertEqual(refreshed["user"]["impersonated_by"]["username"], "admin")

        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "管理员切换账户测试客户",
                    "unified_social_credit_code": "91440300SWITCH00001",
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

        status, switched = self.request(
            "POST", "/api/auth/impersonate", {"user_id": credit["user_id"]}
        )
        self.assertEqual(status, 200)
        self.assertEqual(switched["user"]["username"], "credit.switch")
        self.csrf_token = switched["csrf_token"]
        status, approved = self.request(
            "POST", f"/api/cases/{case_id}/credit-actions", {"action": "approve"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(approved["case"]["credit"]["effective"])

        status, restored = self.request("POST", "/api/auth/impersonation/stop", {})
        self.assertEqual(status, 200)
        self.assertEqual(restored["user"]["username"], "admin")
        self.assertNotIn("impersonated_by", restored["user"])
        self.csrf_token = restored["csrf_token"]

        status, audit = self.request("GET", "/api/audit")
        self.assertEqual(status, 200)
        case_event = next(
            item
            for item in audit["events"]
            if item["event_type"] == "case.created" and item["target_id"] == case_id
        )
        self.assertEqual(case_event["username"], "admin")
        self.assertEqual(
            case_event["detail"]["impersonation"]["effective_username"],
            "sales.switch",
        )

    def test_admin_can_assign_case_owner_to_sales_user(self):
        sales = self.create_user("owner.sales", "案件销售", ["sales"])
        self.activate_user("owner.sales")
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
        self.assertEqual(self.login("admin", "AdminPass123")[0], 200)
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
        self.assertFalse(reset["user"]["must_change_password"])
        status, denied = self.login("legal.reset", "InitialPass123")
        self.assertEqual(status, 401)
        self.assertFalse(denied["ok"])
        status, authenticated = self.login("legal.reset", "ResetPass789")
        self.assertEqual(status, 200)
        self.assertFalse(authenticated["user"]["must_change_password"])
        self.assertEqual(self.request("GET", "/api/cases")[0], 200)

    @patch("dongjiang_agent.web.server.SecurityEmailSender")
    def test_verified_registration_is_immediately_active(self, sender_class):
        sender = sender_class.return_value
        sender.configured = True
        captured = {}
        sender.send_registration_code.side_effect = (
            lambda recipient, code: captured.update(recipient=recipient, code=code)
        )
        status, requested, _ = self.raw_request(
            "POST", "/api/auth/registration-code", {"email": "sales@example.com"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(requested["ok"])
        self.assertEqual(captured["recipient"], "sales@example.com")
        self.assertRegex(captured["code"], r"^\d{6}$")
        with AuthStore() as store:
            row = store.connection.execute(
                "SELECT code_hash FROM registration_verification_codes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            self.assertNotEqual(row["code_hash"], captured["code"])

        status, invalid, _ = self.raw_request(
            "POST",
            "/api/auth/register",
            {
                "display_name": "注册销售",
                "username": "registered.sales",
                "email": "sales@example.com",
                "password": "Registered123",
                "verification_code": "000000",
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(invalid["ok"])

        status, registered, _ = self.raw_request(
            "POST",
            "/api/auth/register",
            {
                "display_name": "注册销售",
                "username": "registered.sales",
                "email": "sales@example.com",
                "password": "Registered123",
                "verification_code": captured["code"],
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(registered["user"]["roles"], ["case_submitter"])
        self.assertTrue(registered["user"]["active"])
        self.assertEqual(registered["user"]["registration_status"], "approved")
        self.assertFalse(registered["user"]["must_change_password"])
        self.assertNotIn("csrf_token", registered)
        self.assertEqual(self.login("registered.sales", "Registered123")[0], 200)
        self.assertEqual(self.request("GET", "/api/cases")[0], 200)
        self.assertEqual(self.request("GET", "/api/registrations")[0], 404)
        self.assertEqual(
            self.request("POST", f"/api/registrations/{registered['user']['user_id']}/review", {})[0],
            404,
        )

    def test_notifications_are_scoped_to_recipient_and_support_read_all(self):
        first = self.create_user("notice.first", "通知甲", ["sales"])
        second = self.create_user("notice.second", "通知乙", ["sales"])
        with AuthStore() as store:
            store.create_notification(
                first["user_id"], category="system", title="甲的通知", body="仅甲可见"
            )
            store.create_notification(
                second["user_id"], category="system", title="乙的通知", body="仅乙可见"
            )
        self.activate_user("notice.first")
        status, notices = self.request("GET", "/api/notifications")
        self.assertEqual(status, 200)
        self.assertEqual([item["title"] for item in notices["items"]], ["甲的通知"])
        status, missing = self.request("POST", "/api/notifications/not-owned/read", {})
        self.assertEqual(status, 404)
        self.assertFalse(missing["ok"])
        self.assertEqual(self.request("POST", "/api/notifications/read-all", {})[0], 200)
        self.assertEqual(self.request("GET", "/api/notifications")[1]["unread"], 0)

    @patch("dongjiang_agent.web.server.SecurityEmailSender")
    def test_case_notification_email_failure_does_not_block_workflow(self, sender_class):
        credit = self.create_user("notice.credit", "邮件信用审批", ["credit"])
        self.assertEqual(
            self.request(
                "PATCH", f"/api/users/{credit['user_id']}", {"email": "credit-notice@example.com"}
            )[0],
            200,
        )
        sender = sender_class.return_value
        sender.configured = True
        sender.send_notification.side_effect = RuntimeError("SMTP unavailable")
        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "邮件失败不阻塞客户",
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
        self.assertEqual(created["case"]["status"], "credit_pending_approval")
        sender.send_notification.assert_called_with(
            "credit-notice@example.com",
            title="待处理信用审批",
            body=ANY,
            link=f"/cases/{created['case']['case_id']}/action",
        )

    @patch("dongjiang_agent.web.server.SecurityEmailSender")
    def test_email_code_resets_password_and_invalidates_session(self, sender_class):
        user = self.create_user("email.reset", "邮箱重置", ["sales"])
        status, updated = self.request(
            "PATCH",
            f"/api/users/{user['user_id']}",
            {"email": "reset@example.com"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["user"]["email"], "reset@example.com")
        self.activate_user("email.reset")

        sender = sender_class.return_value
        sender.configured = True
        captured = {}

        def capture_email(recipient, code):
            captured.update(recipient=recipient, code=code)

        sender.send_password_reset_code.side_effect = capture_email
        status, requested, _ = self.raw_request(
            "POST", "/api/auth/password-reset/request", {"email": "reset@example.com"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(requested["ok"])
        self.assertEqual(captured["recipient"], "reset@example.com")
        self.assertRegex(captured["code"], r"^\d{6}$")

        with AuthStore() as store:
            reset = store.connection.execute(
                "SELECT code_hash FROM password_reset_codes ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            self.assertNotEqual(reset["code_hash"], captured["code"])

        status, invalid, _ = self.raw_request(
            "POST",
            "/api/auth/password-reset/confirm",
            {
                "email": "reset@example.com",
                "code": "000000",
                "new_password": "RecoveredPass123",
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(invalid["ok"])

        status, confirmed, _ = self.raw_request(
            "POST",
            "/api/auth/password-reset/confirm",
            {
                "email": "reset@example.com",
                "code": captured["code"],
                "new_password": "RecoveredPass123",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(confirmed["ok"])
        status, unauthenticated = self.request("GET", "/api/cases")
        self.assertEqual(status, 401)
        self.assertEqual(self.login("email.reset", "ActivePass456")[0], 401)
        self.assertEqual(self.login("email.reset", "RecoveredPass123")[0], 200)

        status, reused, _ = self.raw_request(
            "POST",
            "/api/auth/password-reset/confirm",
            {
                "email": "reset@example.com",
                "code": captured["code"],
                "new_password": "AnotherPass123",
            },
        )
        self.assertEqual(status, 403)
        self.assertFalse(reused["ok"])

    def test_password_reset_requires_smtp_configuration(self):
        user = self.create_user("smtp.missing", "邮件未配置", ["sales"])
        status, _ = self.request(
            "PATCH", f"/api/users/{user['user_id']}", {"email": "smtp@example.com"}
        )
        self.assertEqual(status, 200)
        with patch.dict(os.environ, {}, clear=False):
            for name in (
                "DONGJIANG_SMTP_HOST",
                "DONGJIANG_SMTP_FROM",
                "DONGJIANG_SMTP_USERNAME",
            ):
                os.environ.pop(name, None)
            status, response, _ = self.raw_request(
                "POST", "/api/auth/password-reset/request", {"email": "smtp@example.com"}
            )
        self.assertEqual(status, 400)
        self.assertIn("邮件服务尚未配置", response["error"])

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
        self.create_user("sales.contract", "合同业务", ["sales"])
        self.create_user("credit.contract", "合同信用", ["credit"])
        self.activate_user("sales.contract")
        status, created = self.request(
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
        self.assertEqual(status, 201)
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

        self.activate_user("credit.contract")
        status, approved = self.request(
            "POST",
            f"/api/cases/{case_id}/credit-actions",
            {"action": "approve", "comment": "信审通过"},
        )
        self.assertEqual(status, 200)
        self.assertTrue(approved["case"]["credit"]["effective"])
        self.assertFalse(approved["case"]["permissions"]["can_upload_contract"])

        status, forbidden = self.request(
            "POST",
            f"/api/cases/{case_id}/contracts",
            {"contract_texts": [contract]},
        )
        self.assertEqual(status, 403)
        self.assertIn("没有执行该操作的权限", forbidden["error"])

        self.assertEqual(self.login("sales.contract", "ActivePass456")[0], 200)
        status, detail = self.request("GET", f"/api/cases/{case_id}")
        self.assertEqual(status, 200)
        self.assertTrue(detail["case"]["permissions"]["can_upload_contract"])
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

        for route in ("/cases/new", "/notifications", "/operations", "/agent-operations", "/analytics"):
            self.connection.request("GET", route)
            response = self.connection.getresponse()
            html = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertIn('id="app"', html)
            self.assertNotIn("运行风险演示案例", html)
            self.assertNotIn("华南精密制造示例有限公司", html)

        self.connection.request("GET", "/js/app.js?v=20260802-auth-simplified")
        response = self.connection.getresponse()
        javascript = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("max-age=31536000", response.headers["Cache-Control"])
        self.assertIn("immutable", response.headers["Cache-Control"])
        self.assertNotIn('data-nav="tasks">我的待办', javascript)
        self.assertIn('href="/cases/new" data-link data-nav="new">发起信审', javascript)
        self.assertIn('href="/agent-operations" data-link data-nav="agent-operations">Agent运维', javascript)
        self.assertNotIn('href="/users" data-link data-nav="users">用户', javascript)
        self.assertNotIn('class="primary small" href="/cases/new"', javascript)
        account_start = javascript.index('<div id="accountPopover"')
        account_end = javascript.index('</nav>', account_start)
        self.assertIn('系统管理', javascript[account_start:account_end])
        self.assertIn('href="/users"', javascript[account_start:account_end])
        self.assertNotIn('href="/registrations"', javascript[account_start:account_end])

        self.connection.request("GET", "/cases")
        response = self.connection.getresponse()
        response.read()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store, max-age=0")

    def test_navigation_summary_only_returns_unread_count(self):
        with AuthStore() as store:
            store.create_notification(
                self.request("GET", "/api/auth/status")[1]["user"]["user_id"],
                category="system",
                title="导航摘要测试",
                body="只统计未读数量",
            )
        status, summary = self.request("GET", "/api/navigation-summary")
        self.assertEqual(status, 200)
        self.assertEqual(summary["unread_notifications"], 1)
        self.assertEqual(
            set(summary),
            {
                "ok",
                "unread_notifications",
            },
        )

        self.create_user("summary.sales", "摘要销售", ["sales"])
        self.activate_user("summary.sales")
        status, sales_summary = self.request("GET", "/api/navigation-summary")
        self.assertEqual(status, 200)
        self.assertEqual(set(sales_summary), {"ok", "unread_notifications"})

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

    def test_xlsx_contract_upload_and_image_asset_preview(self):
        import openpyxl

        _, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "多格式合同客户",
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
        self.assertEqual(self.request(
            "POST", f"/api/cases/{case_id}/credit-actions", {"action": "approve"}
        )[0], 200)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "销售合同.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "合同条款"
            rows = [
                "甲方：东江；乙方：客户。合同标的：组件。",
                "付款：月结60天。知识产权：各自所有。保密：不得披露。",
                "违约责任：赔偿直接损失。解除与终止：违约可解除。争议解决：深圳法院。",
                "买方可取消订单且不承担任何责任。",
            ]
            for index, value in enumerate(rows, start=1):
                sheet.cell(index, 1, value)
            workbook.save(path)
            workbook.close()
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        status, submitted = self.request(
            "POST", f"/api/cases/{case_id}/contracts",
            {"files": [{"name": "销售合同.xlsx", "data_base64": encoded}]},
        )
        self.assertEqual(status, 200)
        finding = next(
            item for item in submitted["case"]["findings"]
            if item["rule_id"] == "DJ-CANCEL-WITHOUT-LIABILITY"
        )
        self.assertEqual(finding["location"]["kind"], "cell")
        self.assertEqual(finding["location"]["cell"], "A4")

        image_case = "DJ-IMAGE-ASSET"
        image_dir = Path(f"data/archive/{image_case}/contract")
        image_dir.mkdir(parents=True)
        image = image_dir / "scan.png"
        image.write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nWQAAAAASUVORK5CYII="
        ))
        Path("data/cases").mkdir(parents=True, exist_ok=True)
        Path(f"data/cases/{image_case}.json").write_text(json.dumps({
            "case_id": image_case,
            "customer": {"customer_name": "图片合同客户", "customer_type": "new", "business_type": "TKP"},
            "source_documents": [{
                "document_id": "DOC-IMAGE", "document_kind": "contract",
                "name": "scan.png", "archived_path": str(image.resolve()),
                "media_type": "png", "parse_status": "parsed",
            }],
        }, ensure_ascii=False), encoding="utf-8")
        state = json.loads(Path(f"data/cases/{image_case}.json").read_text(encoding="utf-8"))
        state["source_documents"][0]["extractor"] = "tesseract"
        state["source_documents"][0]["fragments"] = [{
            "fragment_id": "image-1",
            "text": "付款账期120天，买方无责任取消订单。",
            "location": {
                "kind": "image", "image": 1, "ocr": True,
                "ocr_regions": [
                    {"start": 0, "end": 8, "x": 0.1, "y": 0.1, "width": 0.4, "height": 0.1},
                    {"start": 8, "end": 18, "x": 0.1, "y": 0.3, "width": 0.6, "height": 0.1},
                ],
            },
        }]
        Path(f"data/cases/{image_case}.json").write_text(
            json.dumps(state, ensure_ascii=False), encoding="utf-8"
        )
        with patch(
            "dongjiang_agent.ingestion.DocumentExtractor.extract"
        ) as extractor:
            status, preview = self.request(
                "GET",
                f"/api/cases/{image_case}/documents/DOC-IMAGE?fragment=image-1&highlight={quote('取消订单')}",
            )
        extractor.assert_not_called()
        self.assertEqual(status, 200)
        self.assertEqual(len(preview["highlight_regions"]), 1)
        self.assertEqual(preview["highlight_regions"][0]["y"], 0.3)
        status, body, headers = self.download(
            f"/api/cases/{image_case}/documents/DOC-IMAGE/asset"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"\x89PNG"))
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertIn("inline", headers["Content-Disposition"])

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

    @patch("dongjiang_agent.web.server.ContractTranslationStore")
    def test_contract_translation_routes_confirm_download_and_audit(self, store_class):
        case_id = "DJ-TRANSLATE1"
        Path("data/cases").mkdir(parents=True)
        Path(f"data/cases/{case_id}.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "status": "approved",
                    "credit_status": "effective",
                    "customer": {
                        "customer_name": "翻译接口客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                    },
                    "source_documents": [],
                    "trace": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        artifact = Path("translation.docx")
        artifact.write_bytes(b"PK\x03\x04translated")
        draft = {
            "translation_id": "TR-API1",
            "document_id": "DOC-API1",
            "target_language": "en",
            "target_language_label": "英文",
            "segment_count": 2,
            "model": "test-model",
            "status": "draft",
            "entries": [
                {
                    "fragment_id": "paragraph-1",
                    "source_text": "原文",
                    "translated_text": "Source",
                }
            ],
        }
        confirmed = {**draft, "status": "confirmed"}
        store = store_class.return_value
        store.list.return_value = []
        store.create.return_value = draft
        store.detail.return_value = draft
        store.confirm.return_value = confirmed
        store.artifact_path.return_value = (artifact.resolve(), "合同-英文-双语.docx")

        status, created = self.request(
            "POST",
            f"/api/cases/{case_id}/translations",
            {"document_id": "DOC-API1", "target_language": "en"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(created["translation"]["translation_id"], "TR-API1")
        store.create.assert_called_once_with(
            ANY,
            document_id="DOC-API1",
            target_language="en",
            target_languages=[],
            base_translation_id="",
            actor={"actor_id": ANY, "display_name": "测试管理员"},
        )

        status, detail = self.request(
            "GET", f"/api/cases/{case_id}/translations/TR-API1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["translation"]["status"], "draft")

        status, result = self.request(
            "POST",
            f"/api/cases/{case_id}/translations/TR-API1/confirm",
            {
                "entries": [
                    {"fragment_id": "paragraph-1", "translated_text": "Source"}
                ],
                "review_note": "已逐段核对。",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["translation"]["status"], "confirmed")

        status, body, headers = self.download(
            f"/api/cases/{case_id}/translations/TR-API1/download"
        )
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"PK"))
        self.assertIn("attachment", headers["Content-Disposition"])
        status, audit = self.request("GET", "/api/audit")
        self.assertEqual(status, 200)
        event_types = {item["event_type"] for item in audit["events"]}
        self.assertIn("contract.translation_created", event_types)
        self.assertIn("contract.translation_confirmed", event_types)

    def test_admin_can_list_and_retry_failed_writeback(self):
        case_id = "DJ-WRITEBACK1"
        case_dir = Path("data/cases")
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / f"{case_id}.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "status": "completed",
                    "credit_status": "effective",
                    "customer": {
                        "customer_name": "回写失败客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                    },
                    "credit_assessment": {
                        "score": 80,
                        "risk_level": "low",
                        "approved_credit_limit": 1000000,
                        "recommended_term_days": 60,
                    },
                    "writeback": {
                        "final": {
                            "phase": "final",
                            "oa": {
                                "status": "failed",
                                "error": "temporary outage",
                                "attempted_at": "2026-07-31T01:00:00+00:00",
                            },
                            "crm": {"status": "not_configured"},
                            "sap": {"status": "not_configured"},
                        }
                    },
                    "contract_reviews": [],
                    "trace": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        status, failures = self.request("GET", "/api/operations/writebacks")
        self.assertEqual(status, 200)
        self.assertEqual(failures["total"], 1)
        self.assertEqual(failures["failures"][0]["system"], "oa")

        self.create_user("ops.sales", "普通销售", ["sales"])
        self.activate_user("ops.sales")
        self.assertEqual(self.request("GET", "/api/operations/writebacks")[0], 403)
        self.assertEqual(
            self.request(
                "POST",
                f"/api/cases/{case_id}/writeback-retries",
                {"phase": "final", "system": "oa"},
            )[0],
            403,
        )
        self.assertEqual(self.login("admin", "AdminPass123")[0], 200)

        class OAAdapter:
            def write_result(self, case_id, payload):
                return {"ok": True, "case_id": case_id}

        bundle = IntegrationBundle(oa=OAAdapter(), audit_root=Path("data/integration-audit"))
        with patch(
            "dongjiang_agent.web.server.IntegrationBundle.from_environment",
            return_value=bundle,
        ):
            status, retried = self.request(
                "POST",
                f"/api/cases/{case_id}/writeback-retries",
                {"phase": "final", "system": "oa"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(retried["result"]["status"], "succeeded")
        self.assertEqual(len(retried["result"]["retry_history"]), 1)
        stored = json.loads((case_dir / f"{case_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["writeback"]["final"]["oa"]["status"], "succeeded")
        self.assertEqual(stored["trace"][-1]["stage"], "integration.writeback_retried")
        status, failures = self.request("GET", "/api/operations/writebacks")
        self.assertEqual(status, 200)
        self.assertEqual(failures["total"], 0)

    def test_agent_operations_is_admin_only_and_exposes_aggregates(self):
        case_id = "DJ-AGENT-OPS1"
        case_dir = Path("data/cases")
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / f"{case_id}.json").write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "status": "completed",
                    "customer": {"customer_name": "Agent运维客户", "business_type": "TKP"},
                    "workflow_plans": [
                        {
                            "plan_id": "CREDIT-OPS-1",
                            "agent": "credit",
                            "label": "信用信审子 Agent",
                            "version": "2.0",
                            "frozen": True,
                            "spec_hash": "not-a-valid-hash",
                            "task_catalog_version": "dongjiang-controlled-tasks-v2",
                            "runtime_snapshot": {},
                            "tasks": [],
                        }
                    ],
                    "agent_runs": [
                        {
                            "plan_id": "CREDIT-OPS-1",
                            "task_id": "credit.verification",
                            "status": "failed",
                            "attempt_count": 1,
                            "input_summary": "客户敏感内容",
                            "output_summary": "模型原始输出",
                        }
                    ],
                    "execution_audits": [
                        {
                            "plan_id": "CREDIT-OPS-1",
                            "status": "non_conformant",
                            "integrity_valid": False,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        status, report = self.request("GET", "/api/operations/agents")
        self.assertEqual(status, 200)
        self.assertEqual(report["metrics"]["critical_plans"], 1)
        self.assertNotIn("客户敏感内容", json.dumps(report, ensure_ascii=False))
        self.assertNotIn("模型原始输出", json.dumps(report, ensure_ascii=False))

        self.create_user("agent.ops.sales", "普通销售", ["sales"])
        self.assertEqual(self.login("agent.ops.sales", "InitialPass123")[0], 200)
        status, denied = self.request("GET", "/api/operations/agents")
        self.assertEqual(status, 403)
        self.assertFalse(denied["ok"])

    def test_agent_incident_api_enforces_permissions_audits_notifies_and_redacts(self):
        assignee = self.create_user("agent.assignee", "异常责任人", ["credit"])
        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "异常接口测试客户",
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
        with DongjiangWorkflowHarness() as harness:
            workflow_run = harness.get(case_id)
            plan = workflow_run.state["workflow_plans"][-1]
        analysis_task = next(
            item for item in plan["tasks"] if item["phase"] == "analysis"
        )
        with DongjiangWorkflowHarness() as harness:
            harness.graph.update_state(
                harness._config(case_id),
                {
                    "execution_audits": [
                        {
                            "plan_id": plan["plan_id"],
                            "status": "non_conformant",
                            "integrity_valid": True,
                            "missing_tasks": [analysis_task["task_id"]],
                        }
                    ]
                },
            )

        status, acknowledged = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-incidents",
            {
                "action": "acknowledge",
                "plan_id": plan["plan_id"],
                "note": "接口确认备注不得原样返回",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(acknowledged["incident"]["status"], "acknowledged")
        self.assertNotIn(
            "接口确认备注不得原样返回",
            json.dumps(acknowledged, ensure_ascii=False),
        )

        status, assigned = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-incidents",
            {
                "action": "assign",
                "plan_id": plan["plan_id"],
                "assignee_user_id": assignee["user_id"],
                "note": "交由信用团队核查",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(assigned["incident"]["status"], "assigned")
        self.assertEqual(
            assigned["incident"]["assignee"]["user_id"], assignee["user_id"]
        )
        with AuthStore() as store:
            notices = store.list_notifications(assignee["user_id"])
            events = store.list_audit(limit=500)
        self.assertTrue(
            any(item["category"] == "agent_incident" for item in notices["items"])
        )
        event_types = {item["event_type"] for item in events}
        self.assertIn("agent.incident.acknowledge", event_types)
        self.assertIn("agent.incident.assign", event_types)

        status, rerun = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-incidents",
            {
                "action": "rerun",
                "plan_id": plan["plan_id"],
                "task_id": analysis_task["task_id"],
                "note": "候选重跑备注不得原样返回",
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(
            rerun["incident"]["latest_rerun"]["official_state_changed"]
        )
        payload_text = json.dumps(rerun, ensure_ascii=False)
        self.assertNotIn("候选重跑备注不得原样返回", payload_text)
        self.assertNotIn("history", rerun["incident"])
        self.assertNotIn("rerun_history", rerun["incident"])

        status, blocked = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-incidents",
            {
                "action": "rerun",
                "plan_id": plan["plan_id"],
                "task_id": "credit.scoring",
                "note": "尝试重跑决策节点",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("禁止直接重跑", blocked["error"])

        self.create_user("agent.incident.sales", "异常普通销售", ["sales"])
        self.activate_user("agent.incident.sales")
        status, denied = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-incidents",
            {"action": "resolve", "plan_id": plan["plan_id"], "note": "关闭异常"},
        )
        self.assertEqual(status, 403)
        self.assertFalse(denied["ok"])

    def test_agent_incident_api_rejects_healthy_plan(self):
        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "健康接口测试客户",
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
        plan_id = created["case"]["agent_execution"]["plans"][-1]["plan_id"]
        status, rejected = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-incidents",
            {"action": "acknowledge", "plan_id": plan_id},
        )
        self.assertEqual(status, 400)
        self.assertIn("未发现可处置", rejected["error"])

    def test_agent_candidate_api_is_redacted_and_formal_approval_controls_adoption(self):
        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "候选采纳接口客户",
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
        with DongjiangWorkflowHarness() as harness:
            run = harness.get(case_id)
            plan = run.state["workflow_plans"][-1]
            model = dict(run.state["credit_assessment"])
            candidate_limit = max(
                0, float(model["approved_credit_limit"]) - 100_000
            )
            incident = {
                "incident_id": "AINC-WEB-CANDIDATE",
                "plan_id": plan["plan_id"],
                "agent": "credit",
                "status": "rerun_completed",
                "history": [{"note": "异常内部备注不得出现在案件接口"}],
                "rerun_history": [
                    {
                        "incident_id": "AINC-WEB-CANDIDATE",
                        "plan_id": plan["plan_id"],
                        "task_id": "credit.analysis.1",
                        "status": "completed",
                        "execution_audit": "conformant",
                        "completed_at": "2026-08-01T08:00:00+00:00",
                        "candidate_summary": {
                            "score": float(model["score"]) - 2,
                            "risk_level": "medium",
                            "approved_credit_limit": candidate_limit,
                            "recommended_term_days": model[
                                "recommended_term_days"
                            ],
                            "requires_supplement": False,
                            "credit_locked": False,
                            "verification_status": "passed",
                        },
                        "note": "候选重跑备注不得出现在案件接口",
                    }
                ],
            }
            harness.graph.update_state(
                harness._config(case_id), {"agent_incidents": [incident]}
            )

        status, detail = self.request("GET", f"/api/cases/{case_id}")
        self.assertEqual(status, 200)
        self.assertEqual(len(detail["case"]["agent_candidates"]), 1)
        detail_text = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("异常内部备注不得出现在案件接口", detail_text)
        self.assertNotIn("候选重跑备注不得出现在案件接口", detail_text)

        status, requested = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-candidate-actions",
            {
                "action": "request_adoption",
                "incident_id": "AINC-WEB-CANDIDATE",
                "reason": "已完成独立复核，提交正式审批确认",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(requested["review"]["status"], "pending")
        self.assertTrue(requested["review"]["reason_recorded"])
        self.assertNotIn(
            "已完成独立复核",
            json.dumps(requested, ensure_ascii=False),
        )
        request_id = requested["review"]["request_id"]

        status, duplicate = self.request(
            "POST",
            f"/api/cases/{case_id}/agent-candidate-actions",
            {
                "action": "request_adoption",
                "incident_id": "AINC-WEB-CANDIDATE",
                "reason": "重复提交候选申请",
            },
        )
        self.assertEqual(status, 400)
        self.assertIn("已有待审批", duplicate["error"])

        status, approved = self.request(
            "POST",
            f"/api/cases/{case_id}/credit-actions",
            {
                "action": "approve",
                "comment": "正式审批确认采纳候选",
                "candidate_adoption_request_id": request_id,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            approved["case"]["credit"]["approved_result"]["credit_limit"],
            candidate_limit,
        )
        candidate = approved["case"]["agent_candidates"][0]
        self.assertEqual(candidate["status"], "approved")
        self.assertFalse(candidate["permissions"]["can_request_adoption"])
        with AuthStore() as store:
            event_types = {
                item["event_type"] for item in store.list_audit(limit=500)
            }
        self.assertIn("agent.candidate.request_adoption", event_types)
        self.assertIn("agent.candidate.approved", event_types)

    @patch("dongjiang_agent.web.server.SLAService")
    def test_sla_operations_are_admin_only_and_sweep_is_audited(self, service_class):
        service = service_class.return_value
        service.dashboard.return_value = {
            "items": [],
            "metrics": {"total": 0, "on_track": 0, "due_soon": 0, "overdue": 0},
            "by_task": [],
            "policy_version": "test-v1",
            "generated_at": "2026-07-31T00:00:00+00:00",
        }
        service.sweep.return_value = {
            "ok": True,
            "examined": 1,
            "due_soon": 0,
            "overdue": 1,
            "notifications_created": 1,
            "emails_sent": 0,
        }
        status, dashboard = self.request("GET", "/api/operations/sla")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["metrics"]["total"], 0)
        status, swept = self.request("POST", "/api/operations/sla/sweep", {})
        self.assertEqual(status, 200)
        self.assertEqual(swept["notifications_created"], 1)
        with AuthStore() as store:
            events = store.list_audit()
        self.assertTrue(any(item["event_type"] == "operations.sla_sweep" for item in events))

        self.create_user("sla.sales", "时效普通销售", ["sales"])
        self.activate_user("sla.sales")
        self.assertEqual(self.request("GET", "/api/operations/sla")[0], 403)
        self.assertEqual(self.request("POST", "/api/operations/sla/sweep", {})[0], 403)

    @patch("dongjiang_agent.web.server.AgentIncidentService")
    def test_agent_incident_sweep_is_admin_only_and_audited(self, service_class):
        service_class.return_value.sweep.return_value = {
            "ok": True,
            "examined_cases": 3,
            "opened_incidents": 1,
            "opened": [
                {
                    "case_id": "DJ-AUTO-1",
                    "incident_id": "AINC-AUTO-1",
                    "plan_id": "CREDIT-AUTO-1",
                    "severity": "critical",
                    "issue_types": ["execution_deviation"],
                }
            ],
            "notifications_created": 1,
            "emails_sent": 0,
            "audit_events": 1,
            "policy_version": "competition-agent-operations-v1",
            "generated_at": "2026-08-01T00:00:00+00:00",
        }
        status, result = self.request("POST", "/api/operations/agents/sweep", {})
        self.assertEqual(status, 200)
        self.assertEqual(result["opened_incidents"], 1)
        with AuthStore() as store:
            events = store.list_audit(limit=100)
        self.assertTrue(
            any(
                item["event_type"] == "operations.agent_incident_sweep"
                for item in events
            )
        )

        self.create_user("agent.sweep.sales", "扫描普通销售", ["sales"])
        self.activate_user("agent.sweep.sales")
        status, denied = self.request("POST", "/api/operations/agents/sweep", {})
        self.assertEqual(status, 403)
        self.assertFalse(denied["ok"])

    @patch("dongjiang_agent.web.server.AnalyticsService")
    def test_analytics_and_exports_are_admin_only(self, service_class):
        service = service_class.return_value
        service.report.return_value = {
            "range_days": 30,
            "range_label": "近30天",
            "generated_at": "2026-07-31T00:00:00+00:00",
            "policy_version": "test-v1",
            "metrics": {},
            "nodes": [],
            "backlog": [],
            "trend": [],
            "cases": [],
            "intervals": [],
        }
        service.export_csv.return_value = b"csv-report"
        service.export_xlsx.return_value = b"xlsx-report"

        status, analytics = self.request("GET", "/api/operations/analytics?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(analytics["range_days"], 30)
        status, body, headers = self.download("/api/operations/analytics/export.csv?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"csv-report")
        self.assertIn("text/csv", headers["Content-Type"])
        self.assertIn("attachment", headers["Content-Disposition"])
        status, body, headers = self.download("/api/operations/analytics/export.xlsx?days=30")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"xlsx-report")
        self.assertIn("spreadsheetml", headers["Content-Type"])

        self.create_user("analytics.sales", "分析普通销售", ["sales"])
        self.activate_user("analytics.sales")
        self.assertEqual(self.request("GET", "/api/operations/analytics?days=30")[0], 403)
        self.assertEqual(self.download("/api/operations/analytics/export.csv?days=30")[0], 403)

    @patch("dongjiang_agent.web.server.BenchmarkService")
    def test_benchmark_run_exports_and_audit_are_admin_only(self, service_class):
        report = {
            "run_id": "BRUN-TEST",
            "status": "passed",
            "repeat_count": 2,
            "suite": {"suite_id": "suite-test", "version": "1.0.0"},
            "runtime": {
                "external_ai": "disabled",
                "enterprise_integrations": "disabled",
            },
            "metrics": {"case_total": 10, "case_passed": 10},
            "cases": [],
        }
        service = service_class.return_value
        service.summary.return_value = {
            "suite": report["suite"],
            "latest": report,
            "status": "completed",
        }
        service.run.return_value = report
        service.export_csv.return_value = b"benchmark-csv"
        service.export_xlsx.return_value = b"benchmark-xlsx"

        status, summary = self.request("GET", "/api/operations/benchmarks")
        self.assertEqual(status, 200)
        self.assertEqual(summary["latest"]["run_id"], "BRUN-TEST")
        status, result = self.request(
            "POST", "/api/operations/benchmarks/run", {"repeats": 2}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["report"]["status"], "passed")
        service.run.assert_called_once_with(repeats=2)

        status, body, headers = self.download(
            "/api/operations/benchmarks/export.csv"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"benchmark-csv")
        self.assertIn("text/csv", headers["Content-Type"])
        status, body, headers = self.download(
            "/api/operations/benchmarks/export.xlsx"
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"benchmark-xlsx")
        self.assertIn("spreadsheetml", headers["Content-Type"])

        with AuthStore() as store:
            events = store.list_audit(limit=100)
        event = next(
            item
            for item in events
            if item["event_type"] == "operations.benchmark_run"
        )
        self.assertEqual(event["target_id"], "BRUN-TEST")
        self.assertEqual(event["detail"]["case_passed"], 10)

        self.create_user("benchmark.sales", "评测普通销售", ["sales"])
        self.activate_user("benchmark.sales")
        self.assertEqual(
            self.request("GET", "/api/operations/benchmarks")[0], 403
        )
        self.assertEqual(
            self.request(
                "POST", "/api/operations/benchmarks/run", {"repeats": 2}
            )[0],
            403,
        )
        self.assertEqual(
            self.download("/api/operations/benchmarks/export.csv")[0], 403
        )

        self.cookie = ""
        self.csrf_token = ""
        self.assertEqual(
            self.request("GET", "/api/operations/benchmarks")[0], 401
        )

    def test_benchmark_spa_route_and_static_module_exist(self):
        status, html, headers = self.download("/benchmarks")
        self.assertEqual(status, 200)
        self.assertIn(b"20260802-auth-simplified", html)
        self.assertIn("text/html", headers["Content-Type"])

        status, module, headers = self.download("/js/pages/benchmark.js")
        self.assertEqual(status, 200)
        self.assertIn(b"renderBenchmarkPage", module)
        self.assertIn("javascript", headers["Content-Type"])

        status, case_module, headers = self.download("/js/pages/case-detail.js")
        self.assertEqual(status, 200)
        self.assertIn(b"canGenerateExtraction", case_module)
        self.assertIn(b"structuredExtractionPanel", case_module)
        self.assertIn("javascript", headers["Content-Type"])

    def test_frontend_entrypoint_lazily_loads_route_modules_with_retry(self):
        status, module, headers = self.download(
            "/js/app.js?v=20260802-auth-simplified"
        )
        self.assertEqual(status, 200)
        source = module.decode("utf-8")
        self.assertIn("async function loadModule", source)
        self.assertIn("const promise = import(path)", source)
        self.assertIn("retry=${Date.now()}", source)
        self.assertIn("pageModulePaths", source)
        self.assertIn("refreshNavigationSummary", source)
        self.assertIn("startNavigationProgress", source)
        self.assertIn("warmPageModules", source)
        self.assertIn("Promise.all([", source)
        self.assertIn("root.replaceChildren(nextView)", source)
        self.assertNotIn('root.innerHTML = ""', source)
        self.assertNotIn('from "./pages/', source)
        self.assertIn("javascript", headers["Content-Type"])

        status, api_module, headers = self.download(
            "/js/api.js?v=20260802-auth-simplified"
        )
        self.assertEqual(status, 200)
        self.assertIn(b"responseCache", api_module)
        self.assertIn(b"cacheTtl", api_module)
        self.assertIn("immutable", headers["Cache-Control"])

    @patch("dongjiang_agent.web.server.ModelHealthService")
    def test_model_probe_is_admin_only_and_audited(self, service_class):
        service_class.return_value.probe.return_value = {
            "configured": True,
            "model": "test-model",
            "latest": {
                "status": "healthy",
                "http_status": None,
                "duration_ms": 12,
            },
            "recent_call_count": 1,
            "recent_success_rate": 1.0,
            "recent_failure_count": 0,
        }
        status, result = self.request(
            "POST", "/api/operations/model/probe", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["latest"]["status"], "healthy")
        with AuthStore() as store:
            events = store.list_audit(limit=100)
        event = next(
            item
            for item in events
            if item["event_type"] == "operations.model_probe"
        )
        self.assertEqual(event["detail"]["model"], "test-model")

        self.create_user("model.probe.sales", "模型探测销售", ["sales"])
        self.activate_user("model.probe.sales")
        self.assertEqual(
            self.request("POST", "/api/operations/model/probe", {})[0], 403
        )

    @patch("dongjiang_agent.workflow.harness.StructuredFieldExtractor")
    def test_structured_extraction_api_enforces_roles_and_redacts_audit(
        self, extractor_class
    ):
        report = (
            "客户：敏感客户名称\n"
            "资产负债率为45%，流动比率为1.8。\n"
            "联系人13800138000，邮箱secret@example.com。"
        ).encode("utf-8")
        status, created = self.request(
            "POST",
            "/api/cases",
            {
                "customer": {
                    "customer_name": "敏感客户名称",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                },
                "files": [
                    {
                        "name": "credit-profile.txt",
                        "data_base64": base64.b64encode(report).decode("ascii"),
                    }
                ],
                "use_cached_credit": False,
            },
        )
        self.assertEqual(status, 201)
        case_id = created["case"]["case_id"]
        state = self.request("GET", f"/api/cases/{case_id}")[1]["case"]
        document = next(
            item
            for item in state["source_documents"]
            if item["document_kind"] == "credit"
        )
        extractor_class.return_value.extract.return_value = {
            "status": "succeeded",
            "model": "fake-structured-model",
            "prompt_version": "test-v1",
            "summary": "候选已生成",
            "candidates": [
                {
                    "candidate_id": "FIELD-01",
                    "field": "current_ratio",
                    "label": "流动比率",
                    "value": 1.8,
                    "confidence": 0.94,
                    "evidence_query": "流动比率为1.8",
                    "document_id": document["document_id"],
                    "fragment_id": "line-2",
                    "location": {"line": 2},
                }
            ],
            "verification": {
                "status": "passed",
                "candidate_count": 1,
                "located_count": 1,
                "checks": {
                    "schema_valid": True,
                    "all_fields_allowlisted": True,
                    "all_evidence_located": True,
                    "confidence_valid": True,
                },
                "verifier": "independent_extraction_guard",
            },
        }

        self.create_user("extraction.credit", "提取信用", ["credit"])
        self.create_user("extraction.sales", "提取销售", ["sales"])
        self.activate_user("extraction.credit")

        status, generated = self.request(
            "POST",
            f"/api/cases/{case_id}/structured-extractions",
            {"document_kind": "credit"},
        )
        self.assertEqual(status, 200)
        extraction_id = generated["extraction"]["extraction_id"]
        self.assertEqual(generated["extraction"]["status"], "pending")
        self.assertAlmostEqual(
            generated["case"]["customer"]["current_ratio"], 1.5
        )
        sent_text = extractor_class.return_value.extract.call_args.kwargs[
            "redacted_text"
        ]
        self.assertNotIn("敏感客户名称", sent_text)
        self.assertNotIn("13800138000", sent_text)
        self.assertNotIn("secret@example.com", sent_text)

        reason = "人工核对原始财务资料后确认，不记录正文"
        status, adopted = self.request(
            "POST",
            f"/api/cases/{case_id}/structured-extraction-actions",
            {
                "extraction_id": extraction_id,
                "action": "adopt",
                "candidate_ids": ["FIELD-01"],
                "reason": reason,
            },
        )
        self.assertEqual(status, 200)
        self.assertAlmostEqual(adopted["case"]["customer"]["current_ratio"], 1.8)
        self.assertFalse(adopted["case"]["credit"]["effective"])
        self.assertIsNotNone(adopted["case"]["next_action"])
        self.assertTrue(
            adopted["extraction"]["decision"]["result_plan_id"]
        )

        with AuthStore() as store:
            events = store.list_audit(limit=100)
        extraction_events = [
            item
            for item in events
            if str(item["event_type"]).startswith("agent.extraction.")
        ]
        self.assertEqual(len(extraction_events), 2)
        serialized = json.dumps(extraction_events, ensure_ascii=False)
        self.assertNotIn("敏感客户名称", serialized)
        self.assertNotIn("13800138000", serialized)
        self.assertNotIn("secret@example.com", serialized)
        self.assertNotIn(reason, serialized)
        self.assertNotIn("流动比率为1.8", serialized)

        self.activate_user("extraction.sales")
        self.assertEqual(
            self.request(
                "POST",
                f"/api/cases/{case_id}/structured-extractions",
                {"document_kind": "credit"},
            )[0],
            403,
        )

    def test_mock_enterprise_approval_is_credit_approver_only(self):
        self.create_user("mock.submitter", "Mock业务经办", ["sales"])
        self.create_user("mock.credit", "Mock信用审批", ["credit"])
        self.activate_user("mock.submitter")
        with patch.dict(
            os.environ,
            {
                "DONGJIANG_INTEGRATION_MODE": "mock",
                "DONGJIANG_MOCK_ENTERPRISE_ROOT": str(
                    Path(self.temp.name) / "mock-enterprise"
                ),
            },
            clear=False,
        ):
            status, created = self.request(
                "POST",
                "/api/cases",
                {
                    "customer": {
                        "customer_name": "Mock企业系统客户",
                        "crm_customer_id": "CRM-MOCK-API",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1000000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    }
                },
            )
            self.assertEqual(status, 201)
            case_id = created["case"]["case_id"]
            self.activate_user("mock.credit")
            status, result = self.request(
                "POST", f"/api/cases/{case_id}/mock-enterprise-approval", {}
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["mode"], "mock")
        case = result["case"]
        self.assertTrue(case["credit"]["effective"])
        self.assertEqual(len(case["approval_chain"]), 5)
        phase = case["writeback"]["credit_activation"]
        for system in ("oa", "crm", "sap"):
            self.assertEqual(phase[system]["status"], "succeeded")
            self.assertTrue(phase[system]["response"]["mock"])
        with AuthStore() as store:
            events = store.list_audit(limit=100)
        self.assertTrue(
            any(
                item["event_type"] == "integration.mock_enterprise_approval"
                and item["target_id"] == case_id
                for item in events
            )
        )

        self.assertEqual(self.login("admin", "AdminPass123")[0], 200)
        with patch.dict(os.environ, {"DONGJIANG_INTEGRATION_MODE": "mock"}):
            self.assertEqual(
                self.request(
                    "POST",
                    f"/api/cases/{case_id}/mock-enterprise-approval",
                    {},
                )[0],
                403,
            )


if __name__ == "__main__":
    unittest.main()
