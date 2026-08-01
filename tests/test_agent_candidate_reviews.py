import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.web.presentation import case_view
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness
from dongjiang_agent.workflow.candidate_reviews import (
    build_candidate_comparison,
    candidate_fingerprint,
    safe_candidate_review_view,
)


class AgentCandidateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.harness = DongjiangWorkflowHarness(
            checkpoint_path=self.root / "workflow.sqlite",
            repository=CaseRepository(self.root / "cases"),
            vault_dir=self.root / "vault",
            inbox_dir=self.root / "inbox",
            output_dir=self.root / "output",
        )
        self.admin = ActorContext("admin-1", ("admin",), "web", "管理员")
        self.credit = ActorContext("credit-1", ("credit",), "web", "信用甲")

    def tearDown(self):
        self.harness.close()
        self.temp.cleanup()

    def _credit_case_with_candidate(self):
        run = self.harness.start(
            {
                "customer_name": "候选审批测试客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
                "current_ratio": 1.5,
            },
            use_cached_credit=False,
            actor=self.admin,
        )
        assessment = dict(run.state["credit_assessment"])
        plan = run.state["workflow_plans"][-1]
        candidate_summary = {
            "score": max(0, float(assessment["score"]) - 3),
            "risk_level": "medium",
            "approved_credit_limit": max(
                0, float(assessment["approved_credit_limit"]) - 100_000
            ),
            "recommended_term_days": int(assessment["recommended_term_days"]),
            "requires_supplement": False,
            "credit_locked": False,
            "verification_status": "passed",
            "plan_id": plan["plan_id"],
            "spec_ref": str(plan.get("spec_hash") or "")[:12],
        }
        rerun = {
            "incident_id": "AINC-CANDIDATE-1",
            "plan_id": plan["plan_id"],
            "task_id": "credit.analysis.1",
            "task_type": "credit_financial_analysis",
            "status": "completed",
            "execution_audit": "conformant",
            "candidate_summary": candidate_summary,
            "completed_at": "2026-08-01T08:00:00+00:00",
            "official_state_changed": False,
            "note": "内部重跑备注不得返回",
        }
        incident = {
            "incident_id": "AINC-CANDIDATE-1",
            "plan_id": plan["plan_id"],
            "agent": "credit",
            "status": "rerun_completed",
            "rerun_history": [rerun],
            "history": [{"note": "内部历史备注不得返回"}],
        }
        self.harness.graph.update_state(
            self.harness._config(run.case_id), {"agent_incidents": [incident]}
        )
        return self.harness.get(run.case_id), incident, rerun

    def test_credit_comparison_is_safe_and_field_limited(self):
        run, incident, _ = self._credit_case_with_candidate()

        comparison = build_candidate_comparison(run.state, incident)
        payload = json.dumps(comparison, ensure_ascii=False)

        self.assertEqual(comparison["mode"], "field")
        self.assertGreater(comparison["changed_count"], 0)
        self.assertEqual(
            {item["field"] for item in comparison["differences"]},
            {
                "score",
                "risk_level",
                "approved_credit_limit",
                "recommended_term_days",
                "requires_supplement",
                "credit_locked",
                "verification_status",
            },
        )
        self.assertNotIn("内部重跑备注", payload)
        self.assertNotIn("history", payload)

    def test_contract_comparison_redacts_finding_text(self):
        state = {
            "contract_facts": [{"document_id": "DOC-1"}],
            "contract_reviews": [
                {
                    "decision": "manual_review",
                    "risk_level": "medium",
                    "findings": [
                        {
                            "rule_id": "RULE-1",
                            "message": "合同正文和规则消息不得返回",
                            "clause_excerpt": "敏感条款原文",
                        }
                    ],
                    "ai_assistance": {"findings": []},
                }
            ],
            "contract_verifications": [
                {
                    "document_id": "DOC-1",
                    "status": "passed",
                    "evidence_coverage": 1.0,
                }
            ],
        }
        incident = {
            "incident_id": "AINC-CONTRACT-1",
            "agent": "contract",
            "rerun_history": [
                {
                    "status": "completed",
                    "candidate_summary": {
                        "documents": [
                            {
                                "document_id": "DOC-1",
                                "decision": "pass",
                                "risk_level": "low",
                                "rule_finding_count": 0,
                                "ai_finding_count": 0,
                                "rule_ids": [],
                                "verification_status": "passed",
                                "evidence_coverage": 1.0,
                            }
                        ]
                    },
                    "completed_at": "2026-08-01T08:00:00+00:00",
                }
            ],
        }

        comparison = build_candidate_comparison(state, incident)
        payload = json.dumps(comparison, ensure_ascii=False)

        self.assertEqual(comparison["mode"], "document")
        self.assertTrue(comparison["documents"][0]["changed"])
        self.assertNotIn("合同正文和规则消息不得返回", payload)
        self.assertNotIn("敏感条款原文", payload)

    def test_request_requires_compatible_node_and_cannot_duplicate(self):
        run, incident, _ = self._credit_case_with_candidate()
        original = deepcopy(run.state["credit_assessment"])

        run, review = self.harness.manage_agent_candidate(
            run.case_id,
            incident_id=incident["incident_id"],
            action="request_adoption",
            actor=self.credit,
            reason="候选已复核，提交正式审批确认",
        )

        self.assertEqual(review["status"], "pending")
        self.assertEqual(run.state["credit_assessment"], original)
        self.assertEqual(run.waiting_for, "credit_approval")
        with self.assertRaisesRegex(ValueError, "已有待审批"):
            self.harness.manage_agent_candidate(
                run.case_id,
                incident_id=incident["incident_id"],
                action="request_adoption",
                actor=self.credit,
                reason="重复提交申请",
            )
        with self.assertRaises(PermissionError):
            self.harness.manage_agent_candidate(
                run.case_id,
                incident_id=incident["incident_id"],
                action="reject_candidate",
                actor=ActorContext("sales-1", ("sales",), "web", "销售甲"),
                reason="销售无权拒绝",
            )

    def test_unverified_candidate_cannot_enter_formal_approval(self):
        run, incident, _ = self._credit_case_with_candidate()
        incident["rerun_history"][-1]["execution_audit"] = "non_conformant"
        self.harness.graph.update_state(
            self.harness._config(run.case_id), {"agent_incidents": [incident]}
        )

        with self.assertRaisesRegex(ValueError, "执行审计未通过"):
            self.harness.manage_agent_candidate(
                run.case_id,
                incident_id=incident["incident_id"],
                action="request_adoption",
                actor=self.credit,
                reason="不应进入审批的候选",
            )

    def test_formal_approval_adopts_candidate_and_records_trace(self):
        run, incident, rerun = self._credit_case_with_candidate()
        run, review = self.harness.manage_agent_candidate(
            run.case_id,
            incident_id=incident["incident_id"],
            action="request_adoption",
            actor=self.credit,
            reason="独立核验通过，申请采纳候选",
        )

        approved = self.harness.resume(
            run.case_id,
            {
                "action": "approve",
                "comment": "正式审批确认候选",
                "candidate_adoption_request_id": review["request_id"],
            },
            actor=self.credit,
        )

        effective = approved.state["effective_credit_assessment"]
        candidate = rerun["candidate_summary"]
        self.assertEqual(
            effective["approved_credit_limit"], candidate["approved_credit_limit"]
        )
        self.assertEqual(effective["score"], candidate["score"])
        stored = approved.state["agent_candidate_reviews"][-1]
        self.assertEqual(stored["status"], "approved")
        self.assertTrue(stored["decision"]["official_state_changed"])
        self.assertTrue(
            any(
                item.get("stage") == "agent.candidate.approved"
                for item in approved.state["trace"]
            )
        )

    def test_changed_candidate_invalidates_pending_request(self):
        run, incident, rerun = self._credit_case_with_candidate()
        run, review = self.harness.manage_agent_candidate(
            run.case_id,
            incident_id=incident["incident_id"],
            action="request_adoption",
            actor=self.credit,
            reason="提交第一版候选",
        )
        changed = deepcopy(rerun)
        changed["completed_at"] = "2026-08-01T09:00:00+00:00"
        changed["candidate_summary"]["score"] += 1
        incident["rerun_history"].append(changed)
        self.harness.graph.update_state(
            self.harness._config(run.case_id), {"agent_incidents": [incident]}
        )
        official = deepcopy(run.state["credit_assessment"])

        with self.assertRaisesRegex(ValueError, "已经变化"):
            self.harness.resume(
                run.case_id,
                {
                    "action": "approve",
                    "candidate_adoption_request_id": review["request_id"],
                },
                actor=self.credit,
            )

        current = self.harness.get(run.case_id)
        self.assertEqual(current.state["credit_assessment"], official)
        self.assertEqual(current.waiting_for, "credit_approval")

    def test_failed_formal_approval_does_not_preapprove_candidate(self):
        run, incident, _ = self._credit_case_with_candidate()
        official = deepcopy(run.state["credit_assessment"])
        run, review = self.harness.manage_agent_candidate(
            run.case_id,
            incident_id=incident["incident_id"],
            action="request_adoption",
            actor=self.credit,
            reason="等待正式审批校验",
        )

        with self.assertRaisesRegex(ValueError, "有效期"):
            self.harness.resume(
                run.case_id,
                {
                    "action": "approve",
                    "validity_days": -1,
                    "candidate_adoption_request_id": review["request_id"],
                },
                actor=self.credit,
            )

        current = self.harness.get(run.case_id)
        self.assertEqual(current.waiting_for, "credit_approval")
        self.assertEqual(current.state["credit_assessment"], official)
        self.assertEqual(
            current.state["agent_candidate_reviews"][-1]["status"], "pending"
        )

    def test_reject_is_auditable_and_does_not_change_official_state(self):
        run, incident, rerun = self._credit_case_with_candidate()
        official = deepcopy(run.state["credit_assessment"])

        rejected, review = self.harness.manage_agent_candidate(
            run.case_id,
            incident_id=incident["incident_id"],
            action="reject_candidate",
            actor=self.credit,
            reason="候选依据不足，保留正式结果",
        )

        self.assertEqual(review["status"], "rejected")
        self.assertEqual(rejected.state["credit_assessment"], official)
        safe = safe_candidate_review_view(review)
        payload = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn("候选依据不足", payload)
        self.assertEqual(safe["candidate_ref"], candidate_fingerprint(rerun)[:12])
        view = case_view(rejected.state, waiting_for=rejected.waiting_for, actor={"roles": ["credit"]})
        self.assertNotIn("内部历史备注", json.dumps(view["agent_candidates"], ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
