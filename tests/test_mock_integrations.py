import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dongjiang_agent.integrations import IntegrationBundle
from dongjiang_agent.integrations.mock import MockEnterpriseStore


class MockIntegrationTests(unittest.TestCase):
    def test_bundle_mock_mode_runs_oa_crm_sap_and_reuses_idempotency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {
                    "DONGJIANG_INTEGRATION_MODE": "mock",
                    "DONGJIANG_MOCK_ENTERPRISE_ROOT": str(root / "enterprise"),
                },
                clear=False,
            ):
                bundle = IntegrationBundle.from_environment(
                    audit_root=root / "audit"
                )
                submitted = bundle.submit_oa(
                    "DJ-MOCK-1", {"case_id": "DJ-MOCK-1"}
                )
                first = bundle.write_back(
                    "DJ-MOCK-1",
                    "CRM-MOCK-1",
                    {
                        "case_id": "DJ-MOCK-1",
                        "status": "credit_effective",
                        "approved_total_credit_limit": 3000000,
                        "approved_term_days": 90,
                    },
                    phase="credit_activation",
                )
                second = bundle.write_back(
                    "DJ-MOCK-1",
                    "CRM-MOCK-1",
                    {
                        "case_id": "DJ-MOCK-1",
                        "status": "credit_effective",
                        "approved_total_credit_limit": 3000000,
                        "approved_term_days": 90,
                    },
                    phase="credit_activation",
                )

            self.assertEqual(submitted["status"], "succeeded")
            for system in ("oa", "crm", "sap"):
                self.assertEqual(first[system]["status"], "succeeded")
                self.assertTrue(first[system]["response"]["mock"])
                self.assertFalse(first[system]["response"]["reused"])
                self.assertTrue(second[system]["response"]["reused"])
            stored = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (root / "enterprise").rglob("*.json")
            )
            self.assertNotIn("contract_text", stored)
            self.assertNotIn("customer_name", stored)

    def test_mock_store_does_not_duplicate_same_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MockEnterpriseStore(tmp)
            first = store.call(
                "oa",
                "write-result",
                "DJ-1",
                {"case_id": "DJ-1", "status": "approved"},
                idempotency_key="DJ-1:oa:final",
            )
            second = store.call(
                "oa",
                "write-result",
                "DJ-1",
                {"case_id": "DJ-1", "status": "approved"},
                idempotency_key="DJ-1:oa:final",
            )
            self.assertEqual(first["reference_id"], second["reference_id"])
            self.assertFalse(first["reused"])
            self.assertTrue(second["reused"])
            rows = json.loads(
                (Path(tmp) / "oa" / "write-result.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(rows), 1)


if __name__ == "__main__":
    unittest.main()
