import json
import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.integrations import IntegrationBundle
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness, WorkflowRun


COMPLETE_SAFE_CONTRACT = """销售合同
甲方：东江集团
乙方：测试客户有限公司
合同标的：精密注塑组件。
合同金额：人民币2,000,000元，信用额度：2,000,000元。
付款及账期：月结60天。
知识产权：双方背景知识产权各自所有。
保密：双方不得披露商业秘密。
违约责任：违约方赔偿直接损失，累计不超过合同金额。
解除与终止：重大违约催告后可以解除。
争议解决：由深圳市人民法院管辖。
"""


class LangGraphWorkflowTests(unittest.TestCase):
    def harness(self, root: Path) -> DongjiangWorkflowHarness:
        return DongjiangWorkflowHarness(
            checkpoint_path=root / "workflow.sqlite",
            repository=CaseRepository(root / "cases"),
            vault_dir=root / "vault",
            inbox_dir=root / "inbox",
            output_dir=root / "output",
            evidence_dir=root / "evidence",
        )

    def test_interrupt_payload_counts_as_resumable_when_snapshot_next_is_empty(self):
        run = WorkflowRun(
            case_id="DJ-INTERRUPT",
            status="credit_pending_approval",
            stage="credit_pending_approval",
            waiting_for="credit_approval",
            next_nodes=[],
            interrupt={"type": "credit_approval"},
            state={"status": "credit_pending_approval"},
        )
        self.assertTrue(run.paused)

    def test_plain_approval_ignores_supplied_adjustment_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "按建议批准测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                        "external_rating": "AA",
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-model", ("sales",), "crm"),
                )
                model = run.state["credit_assessment"]
                approved = harness.resume(
                    run.case_id,
                    {
                        "action": "approve",
                        "approved_credit_limit": 1,
                        "approved_term_days": 1,
                        "comment": "页面即使传入修改值，也应采用模型建议",
                    },
                    actor=ActorContext("credit-model", ("credit",), "web"),
                )
                effective = approved.state["effective_credit_assessment"]
                self.assertEqual(
                    effective["approved_credit_limit"],
                    model["approved_credit_limit"],
                )
                self.assertEqual(
                    effective["recommended_term_days"],
                    model["recommended_term_days"],
                )

    def test_validation_failure_can_retry_from_same_human_interrupt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "审批失败重试测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                        "external_rating": "AA",
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-retry", ("sales",), "crm"),
                )
                with self.assertRaisesRegex(ValueError, "必须填写调整原因"):
                    harness.resume(
                        run.case_id,
                        {
                            "action": "adjust_and_approve",
                            "approved_credit_limit": 1_500_000,
                            "approved_term_days": 45,
                            "comment": "",
                        },
                        actor=ActorContext("credit-retry", ("credit",), "web"),
                    )

                failed = harness.get(run.case_id)
                self.assertEqual(failed.waiting_for, "credit_approval")
                self.assertTrue(failed.paused)
                retried = harness.resume(
                    run.case_id,
                    {"action": "approve", "comment": "改为按模型建议批准"},
                    actor=ActorContext("credit-retry", ("credit",), "web"),
                )
                self.assertEqual(retried.waiting_for, "contract_upload")
                self.assertEqual(retried.state["credit_status"], "effective")
                self.assertTrue(any(
                    item.get("stage") == "workflow.interrupt_retry_prepared"
                    for item in retried.state.get("trace") or []
                ))

    def test_blocked_contract_can_be_revised_and_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer = {
                "customer_name": "阻断流程测试客户",
                "customer_type": "existing",
                "business_type": "TKP",
                "monthly_order_amount": 2_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
                "current_ratio": 1.5,
            }
            blocked_contract = COMPLETE_SAFE_CONTRACT.replace(
                "违约责任：违约方赔偿直接损失，累计不超过合同金额。",
                "违约责任：买方可取消订单且不承担任何责任。",
            )
            with self.harness(root) as harness:
                run = harness.start(
                    customer,
                    use_cached_credit=False,
                    actor=ActorContext("sales-1", ("sales",), "crm"),
                )
                self.assertEqual(run.waiting_for, "credit_approval")
                run = harness.resume(
                    run.case_id,
                    {"action": "approve", "comment": "同意授信建议"},
                    actor=ActorContext("credit-1", ("credit",), "oa"),
                )
                self.assertEqual(run.waiting_for, "contract_upload")
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [blocked_contract]},
                    actor=ActorContext("sales-1", ("sales",), "crm"),
                )
                self.assertTrue(run.paused)
                self.assertEqual(run.waiting_for, "sales_revision")
                self.assertEqual(run.interrupt["type"], "sales_revision")
                checkpoint_text = json.dumps(run.state, ensure_ascii=False)
                self.assertNotIn("2,000,000", checkpoint_text)
                self.assertNotIn("东江集团", checkpoint_text)
                self.assertFalse(any((root / "inbox").rglob("*.txt")))
                stages = [item["stage"] for item in run.state["trace"]]
                self.assertEqual(stages.count("workflow.started"), 1)

                final = harness.resume(
                    run.case_id,
                    {
                        "action": "submit_revision",
                        "contract_texts": [COMPLETE_SAFE_CONTRACT],
                    },
                    actor=ActorContext("sales-1", ("sales",), "crm"),
                )
                self.assertFalse(final.paused)
                self.assertEqual(final.state["decision"], "pass")
                self.assertEqual(final.status, "approved")

    def test_original_documents_are_archived_by_case_with_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "信用资料.txt"
            source.write_text(
                "主体评级：AA\n资产负债率：45%\n流动比率：1.5", encoding="utf-8"
            )
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "归档测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "project_name": "归档项目",
                        "monthly_order_amount": 1_000_000,
                    },
                    file_paths=[str(source)],
                    use_cached_credit=False,
                )
            archived = run.state["source_documents"][0]
            self.assertEqual(archived["document_kind"], "credit")
            self.assertEqual(len(archived["sha256"]), 64)
            self.assertTrue(Path(archived["archived_path"]).is_file())
            self.assertIn(run.case_id, archived["archived_path"])

    def test_credit_approval_persists_scope_validity_and_tkm_exemption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "TKM审批条件客户",
                        "crm_customer_id": "CRM-TKM-1",
                        "customer_type": "new",
                        "business_type": "TKM",
                        "tkm_business_subtype": "precision",
                        "purchase_exemption_requested": True,
                        "project_name": "精密模具项目",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    },
                    use_cached_credit=False,
                )
                approved = harness.resume(
                    run.case_id,
                    {
                        "action": "approve",
                        "approval_scope": "仅限精密模具项目A",
                        "validity_days": 90,
                        "oa_evidence_id": "OA-2026-001",
                        "purchase_exemption_approved": True,
                    },
                )
            approval = approved.state["credit_approval"]
            effective = approved.state["effective_credit_assessment"]
            self.assertEqual(approval["approval_scope"], "仅限精密模具项目A")
            self.assertEqual(approval["validity_days"], 90)
            self.assertEqual(approval["oa_evidence_id"], "OA-2026-001")
            self.assertTrue(effective["purchase_exemption_approved"])

    def test_finalize_calls_configured_oa_crm_and_sap_ports(self):
        class Adapter:
            def __init__(self):
                self.calls = []

            def submit_review(self, case_id, payload):
                self.calls.append(("submit_review", case_id))
                return {"workflow_id": "OA-1"}

            def write_result(self, case_id, payload):
                self.calls.append(("write_result", case_id))
                return {"ok": True}

            def write_credit_decision(self, customer_id, payload):
                self.calls.append(("crm", customer_id))
                return {"ok": True}

            def write_credit_control(self, customer_id, payload):
                self.calls.append(("sap", customer_id))
                return {"ok": True}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = Adapter()
            integrations = IntegrationBundle(
                oa=adapter,
                crm=adapter,
                sap=adapter,
                audit_root=root / "integration-audit",
            )
            with DongjiangWorkflowHarness(
                checkpoint_path=root / "workflow.sqlite",
                repository=CaseRepository(root / "cases"),
                vault_dir=root / "vault",
                inbox_dir=root / "inbox",
                output_dir=root / "output",
                integrations=integrations,
            ) as harness:
                run = harness.start(
                    {
                        "customer_name": "回写测试客户",
                        "crm_customer_id": "CRM-100",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    },
                    use_cached_credit=False,
                )
                run = harness.resume(run.case_id, {"action": "approve"})
                final = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [COMPLETE_SAFE_CONTRACT]},
                )
            activation = final.state["writeback"]["credit_activation"]
            final_result = final.state["writeback"]["final"]
            self.assertEqual(activation["crm"]["status"], "succeeded")
            self.assertEqual(activation["sap"]["status"], "succeeded")
            self.assertEqual(final_result["oa"]["status"], "succeeded")
            self.assertIn(("submit_review", final.case_id), adapter.calls)
            self.assertIn(("crm", "CRM-100"), adapter.calls)
            self.assertIn(("sap", "CRM-100"), adapter.calls)
            self.assertTrue(Path(final.state["reports"]["json"]).is_file())

    def test_checkpoint_survives_process_boundary_and_roles_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer = {
                "customer_name": "等待合同客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
            }
            with self.harness(root) as first:
                run = first.start(
                    customer,
                    use_cached_credit=False,
                    actor=ActorContext("sales-2", ("sales",), "crm"),
                )
                case_id = run.case_id
                self.assertEqual(run.waiting_for, "credit_approval")

            with self.harness(root) as second:
                recovered = second.get(case_id)
                self.assertEqual(recovered.waiting_for, "credit_approval")
                with self.assertRaises(PermissionError):
                    second.resume(
                        case_id,
                        {"action": "approve"},
                        actor=ActorContext("sales-2", ("sales",), "crm"),
                    )
                approved = second.resume(
                    case_id,
                    {"action": "approve", "comment": "审批通过"},
                    actor=ActorContext("finance-1", ("finance",), "oa"),
                )
                self.assertEqual(approved.waiting_for, "contract_upload")
                with self.assertRaises(PermissionError):
                    second.resume(
                        case_id,
                        {"action": "submit_contract", "contract_texts": [COMPLETE_SAFE_CONTRACT]},
                        actor=ActorContext("finance-1", ("finance",), "oa"),
                    )
                final = second.resume(
                    case_id,
                    {"action": "submit_contract", "contract_texts": [COMPLETE_SAFE_CONTRACT]},
                    actor=ActorContext("sales-2", ("sales",), "crm"),
                )
                self.assertFalse(final.paused)
                self.assertEqual(final.state["decision"], "pass")

    def test_special_approval_requires_director_or_ceo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            customer = {
                "customer_name": "特批客户",
                "customer_type": "new",
                "business_type": "TKP",
                "registered_capital": 100_000_000,
                "years_in_business": 20,
                "asset_liability_ratio": 0.4,
                "net_margin": 0.15,
                "current_ratio": 2.0,
                "revenue_growth": 0.2,
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
            }
            over_limit = COMPLETE_SAFE_CONTRACT.replace(
                "信用额度：2,000,000元",
                "信用额度：5,000,000元",
            )
            with self.harness(root) as harness:
                run = harness.start(
                    customer,
                    use_cached_credit=False,
                    actor=ActorContext("sales-3", ("sales",), "crm"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "approve"},
                    actor=ActorContext("credit-3", ("credit",), "oa"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "contract_texts": [over_limit]},
                    actor=ActorContext("sales-3", ("sales",), "crm"),
                )
                self.assertEqual(run.waiting_for, "manager_approval")
                with self.assertRaises(PermissionError):
                    harness.resume(
                        run.case_id,
                        {"action": "approve"},
                        actor=ActorContext("sales-3", ("sales",), "crm"),
                    )
                with self.assertRaises(ValueError):
                    harness.resume(
                        run.case_id,
                        {"action": "approve", "comment": "同意本次例外"},
                        actor=ActorContext("director-1", ("director",), "oa"),
                    )
                evidence = root / "市场总监批准.txt"
                evidence.write_text("批准本次超额信用申请", encoding="utf-8")
                final = harness.resume(
                    run.case_id,
                    {
                        "action": "approve",
                        "comment": "同意本次例外",
                        "file_paths": [str(evidence)],
                    },
                    actor=ActorContext("director-1", ("director",), "oa"),
                )
                self.assertFalse(final.paused)
                self.assertEqual(final.status, "approved_by_exception")
                self.assertEqual(
                    final.state["exception_approval"]["approval_scope"],
                    f"仅限案件 {run.case_id} 的合同例外",
                )
                archived = final.state["approval_evidence"][0]
                self.assertEqual(archived["actor_id"], "director-1")
                self.assertTrue(Path(archived["archived_path"]).is_file())
                self.assertEqual(len(archived["sha256"]), 64)

    def test_current_overdue_requires_evidenced_special_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "逾期锁定客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                        "current_overdue_days": 31,
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-lock", ("sales",), "crm"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "approve", "comment": "同意授信建议"},
                    actor=ActorContext("credit-lock", ("credit",), "oa"),
                )
                self.assertEqual(run.status, "credit_control_locked")
                self.assertEqual(run.waiting_for, "special_release")
                self.assertTrue(run.state["effective_credit_assessment"]["credit_locked"])
                with self.assertRaises(ValueError):
                    harness.resume(
                        run.case_id,
                        {"action": "approve", "comment": "特别放行"},
                        actor=ActorContext("director-lock", ("director",), "oa"),
                    )
                evidence = root / "特别放行OA.txt"
                evidence.write_text("市场总监同意本次特别放行", encoding="utf-8")
                released = harness.resume(
                    run.case_id,
                    {
                        "action": "approve",
                        "comment": "特别放行一次",
                        "file_paths": [str(evidence)],
                    },
                    actor=ActorContext("director-lock", ("director",), "oa"),
                )
                self.assertEqual(released.waiting_for, "contract_upload")
                self.assertEqual(released.state["special_release"]["action"], "approve")

    def test_contract_cannot_be_submitted_before_credit_is_effective(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "门禁测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "asset_liability_ratio": 0.5,
                        "current_ratio": 1.3,
                        "external_rating": "A",
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-gate", ("sales",), "crm"),
                )
                self.assertEqual(run.status, "credit_pending_approval")
                self.assertEqual(run.state["credit_status"], "pending_approval")
                self.assertIsNone(run.state["effective_credit_assessment"])
                with self.assertRaises(PermissionError):
                    harness.resume(
                        run.case_id,
                        {
                            "action": "submit_contract",
                            "contract_texts": [COMPLETE_SAFE_CONTRACT],
                        },
                        actor=ActorContext("sales-gate", ("sales",), "crm"),
                    )

    def test_credit_adjustment_becomes_the_effective_contract_baseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "授信调整测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "asset_liability_ratio": 0.5,
                        "current_ratio": 1.3,
                        "external_rating": "A",
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-adjust", ("sales",), "crm"),
                )
                approved = harness.resume(
                    run.case_id,
                    {
                        "action": "adjust_and_approve",
                        "approved_credit_limit": 1_500_000,
                        "approved_term_days": 45,
                        "comment": "依据担保条件下调额度和账期",
                    },
                    actor=ActorContext("credit-adjust", ("credit",), "oa"),
                )
                self.assertEqual(approved.state["credit_status"], "effective")
                effective = approved.state["effective_credit_assessment"]
                self.assertEqual(effective["approved_credit_limit"], 1_500_000)
                self.assertEqual(effective["recommended_term_days"], 45)
                self.assertEqual(approved.waiting_for, "contract_upload")

    def test_insufficient_credit_data_requires_supplement_before_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "单一指标客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AAA",
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-data", ("sales",), "crm"),
                )
                self.assertEqual(run.status, "credit_supplement_required")
                self.assertEqual(run.waiting_for, "credit_supplement")
                self.assertTrue(run.state["credit_assessment"]["requires_supplement"])
                with self.assertRaises(PermissionError):
                    harness.resume(
                        run.case_id,
                        {"action": "approve"},
                        actor=ActorContext("credit-data", ("credit",), "oa"),
                    )

    def test_unreviewable_contract_is_never_auto_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsupported = root / "contract.bin"
            unsupported.write_bytes(b"not a supported contract")
            with self.harness(root) as harness:
                run = harness.start(
                    {
                        "customer_name": "解析失败客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "A",
                        "asset_liability_ratio": 0.5,
                        "current_ratio": 1.3,
                    },
                    use_cached_credit=False,
                    actor=ActorContext("sales-fail", ("sales",), "crm"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "approve"},
                    actor=ActorContext("credit-fail", ("credit",), "oa"),
                )
                run = harness.resume(
                    run.case_id,
                    {"action": "submit_contract", "file_paths": [str(unsupported)]},
                    actor=ActorContext("sales-fail", ("sales",), "crm"),
                )
                self.assertEqual(run.status, "blocked")
                self.assertEqual(run.waiting_for, "sales_revision")
                self.assertEqual(run.state["decision"], "block")
                self.assertEqual(
                    run.state["contract_reviews"][0]["findings"][0]["rule_id"],
                    "DOCUMENT-NO-REVIEWABLE-CONTRACT",
                )


if __name__ == "__main__":
    unittest.main()
