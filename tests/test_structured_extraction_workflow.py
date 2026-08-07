import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness
from dongjiang_agent.workflow.dynamic import assert_plan_integrity


class FakeStructuredExtractor:
    def extract(self, document_kind, *, redacted_text, documents):
        document = documents[0]
        fragment = document["fragments"][0]
        return {
            "status": "succeeded",
            "model": "fake-structured-model",
            "prompt_version": "test-v1",
            "summary": "识别到流动比率候选",
            "candidates": [
                {
                    "candidate_id": "FIELD-01",
                    "field": "current_ratio",
                    "label": "流动比率",
                    "value": 1.8,
                    "confidence": 0.92,
                    "evidence_query": "流动比率为1.8",
                    "document_id": document["document_id"],
                    "fragment_id": fragment["fragment_id"],
                    "location": dict(fragment["location"]),
                }
            ],
            "verification": {
                "status": "passed",
                "checks": {
                    "schema_valid": True,
                    "all_fields_allowlisted": True,
                    "all_evidence_located": True,
                    "confidence_valid": True,
                },
                "candidate_count": 1,
                "located_count": 1,
                "verifier": "independent_extraction_guard",
            },
        }


class StructuredExtractionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.credit_file = self.root / "credit-profile.txt"
        self.credit_file.write_text(
            "客户财务资料：流动比率为1.8，资产负债率为45%，主体评级AA。\n"
            "评级日期为2026-07-03，资料来源为客户财务部门。",
            encoding="utf-8",
        )
        self.harness = DongjiangWorkflowHarness(
            checkpoint_path=self.root / "workflow.sqlite",
            repository=CaseRepository(self.root / "cases"),
            vault_dir=self.root / "vault",
            inbox_dir=self.root / "inbox",
            output_dir=self.root / "output",
            archive_dir=self.root / "archive",
            execution_dir=self.root / "executions",
        )
        self.credit_actor = ActorContext(
            "credit-1", ("credit",), "test", "信用甲"
        )
        self.run = self.harness.start(
            {
                "customer_name": "结构化提取合成客户",
                "customer_type": "new",
                "business_type": "TKP",
                "monthly_order_amount": 1000000,
                "external_rating": "AA",
                "asset_liability_ratio": 0.45,
                "current_ratio": 1.5,
            },
            file_paths=[str(self.credit_file)],
            use_cached_credit=False,
            actor=ActorContext("sales-1", ("sales",), "test", "销售甲"),
        )
        self.assertEqual(self.run.waiting_for, "credit_approval")

    def tearDown(self):
        self.harness.close()
        self.temp.cleanup()

    def test_generate_does_not_change_official_fields_and_adopt_reruns_plan(self):
        before_plan_id = self.run.state["active_workflow_plan"]["plan_id"]
        generated, extraction = self.harness.generate_structured_extraction(
            self.run.case_id,
            document_kind="credit",
            actor=self.credit_actor,
            extractor=FakeStructuredExtractor(),
        )
        self.assertEqual(generated.state["customer"]["current_ratio"], 1.5)
        self.assertEqual(extraction["status"], "pending")
        self.assertEqual(extraction["verification"]["status"], "passed")

        adopted, result = self.harness.manage_structured_extraction(
            self.run.case_id,
            extraction_id=extraction["extraction_id"],
            action="adopt",
            actor=self.credit_actor,
            candidate_ids=["FIELD-01"],
            reason="已对照原始财务资料确认",
        )
        self.assertEqual(result["status"], "adopted")
        self.assertEqual(adopted.state["customer"]["current_ratio"], 1.8)
        self.assertEqual(adopted.waiting_for, "credit_approval")
        self.assertEqual(adopted.state["credit_verification"]["status"], "passed")
        next_plan = adopted.state["active_workflow_plan"]
        self.assertNotEqual(next_plan["plan_id"], before_plan_id)
        self.assertEqual(
            next_plan["runtime_snapshot"]["structured_extraction_ref"],
            extraction["extraction_id"],
        )
        assert_plan_integrity(next_plan)
        self.assertFalse(adopted.state.get("effective_credit_assessment"))
        self.assertTrue(
            any(
                item.get("stage") == "agent.extraction.adopted"
                for item in adopted.state["trace"]
            )
        )

    def test_reject_and_permission_boundaries_keep_official_state(self):
        with self.assertRaises(PermissionError):
            self.harness.generate_structured_extraction(
                self.run.case_id,
                document_kind="credit",
                actor=ActorContext("sales-1", ("sales",), "test", "销售甲"),
                extractor=FakeStructuredExtractor(),
            )
        _, extraction = self.harness.generate_structured_extraction(
            self.run.case_id,
            document_kind="credit",
            actor=self.credit_actor,
            extractor=FakeStructuredExtractor(),
        )
        rejected, result = self.harness.manage_structured_extraction(
            self.run.case_id,
            extraction_id=extraction["extraction_id"],
            action="reject",
            actor=self.credit_actor,
            reason="原文口径与财务确认口径不一致",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(rejected.state["customer"]["current_ratio"], 1.5)
        self.assertFalse(result["decision"]["official_state_changed"])


if __name__ == "__main__":
    unittest.main()
