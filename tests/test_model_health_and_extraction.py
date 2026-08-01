import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dongjiang_agent.config import load_local_env
from dongjiang_agent.llm import ModelHealthStore, OpenAICompatibleGateway
from dongjiang_agent.llm.structured_extractor import StructuredFieldExtractor


class FakeGateway:
    model = "fake-structured-model"
    available = True

    def __init__(self, response):
        self.response = response

    def analyze_redacted(self, redacted_text, instruction):
        self.redacted_text = redacted_text
        self.instruction = instruction
        return self.response


class ModelHealthAndExtractionTests(unittest.TestCase):
    def test_local_env_override_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "OPENAI_API_KEY=file-key\nDONGJIANG_ENV_FILE_OVERRIDE=true\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OPENAI_API_KEY": "stale-process-key"}, clear=False):
                load_local_env(env_path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "file-key")

            env_path.write_text("OPENAI_API_KEY=file-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "production-key"}, clear=False):
                load_local_env(env_path, override=False)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "production-key")

    def test_health_store_contains_no_prompt_key_or_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "health.json"
            store = ModelHealthStore(path)
            gateway = OpenAICompatibleGateway(
                base_url="https://model.invalid/v1",
                api_key="secret-model-key",
                model="safe-model",
                health_store=store,
            )
            with patch("dongjiang_agent.llm.gateway.urlopen", side_effect=TimeoutError("timeout")):
                with self.assertRaises(TimeoutError):
                    gateway.analyze_redacted(
                        "⟦CUSTOMER_001⟧敏感合同正文", "私密提示词"
                    )

            encoded = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-model-key", encoded)
            self.assertNotIn("敏感合同正文", encoded)
            self.assertNotIn("私密提示词", encoded)
            latest = store.summary()["latest"]
            self.assertEqual(latest["status"], "unhealthy")
            self.assertEqual(latest["error_type"], "TimeoutError")

    def test_structured_credit_candidates_require_allowlist_and_evidence(self):
        response = json.dumps(
            {
                "summary": "识别到信用字段",
                "candidates": [
                    {
                        "field": "asset_liability_ratio",
                        "value": 0.45,
                        "evidence_query": "资产负债率为45%",
                        "confidence": 0.95,
                    },
                    {
                        "field": "customer_name",
                        "value": "不得回传",
                        "evidence_query": "客户名称",
                        "confidence": 0.9,
                    },
                    {
                        "field": "current_ratio",
                        "value": 1.8,
                        "evidence_query": "原文不存在",
                        "confidence": 0.8,
                    },
                ],
            },
            ensure_ascii=False,
        )
        result = StructuredFieldExtractor(FakeGateway(response)).extract(
            "credit",
            redacted_text="⟦CUSTOMER_001⟧资产负债率为45%",
            documents=[
                {
                    "document_id": "DOC-CREDIT",
                    "fragments": [
                        {
                            "fragment_id": "line-1",
                            "text": "资产负债率为45%",
                            "location": {"line": 1},
                        }
                    ],
                }
            ],
        )
        self.assertEqual(result["verification"]["status"], "passed")
        self.assertEqual(len(result["candidates"]), 1)
        candidate = result["candidates"][0]
        self.assertEqual(candidate["field"], "asset_liability_ratio")
        self.assertEqual(candidate["fragment_id"], "line-1")

    def test_structured_contract_candidate_normalizes_boolean(self):
        response = json.dumps(
            {
                "candidates": [
                    {
                        "field": "has_confidentiality",
                        "value": "是",
                        "evidence_query": "不得披露商业秘密",
                        "confidence": 0.88,
                    }
                ]
            },
            ensure_ascii=False,
        )
        result = StructuredFieldExtractor(FakeGateway(response)).extract(
            "contract",
            redacted_text="⟦CUSTOMER_001⟧不得披露商业秘密",
            documents=[
                {
                    "document_id": "DOC-CONTRACT",
                    "fragments": [
                        {
                            "fragment_id": "p-2",
                            "text": "不得披露商业秘密",
                            "location": {"paragraph": 2},
                        }
                    ],
                }
            ],
        )
        self.assertIs(result["candidates"][0]["value"], True)


if __name__ == "__main__":
    unittest.main()
