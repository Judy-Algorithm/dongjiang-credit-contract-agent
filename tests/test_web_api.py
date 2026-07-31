import base64
import http.client
import json
import os
import tempfile
import threading
import unittest
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

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        os.chdir(self.previous_cwd)
        self.temp.cleanup()

    def request(self, method, path, payload=None):
        body = None
        headers = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        self.connection.request(method, path, body=body, headers=headers)
        response = self.connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        return response.status, data

    def request_with_headers(self, method, path, payload, headers):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json", **headers}
        self.connection.request(method, path, body=body, headers=merged)
        response = self.connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))

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


if __name__ == "__main__":
    unittest.main()
