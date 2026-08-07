import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dongjiang_agent.ingestion import (
    DocumentFragment,
    DocumentQualityGate,
    ExtractedDocument,
)
from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.workflow import DongjiangWorkflowHarness


class DocumentQualityGateTests(unittest.TestCase):
    def test_empty_scanned_document_requires_human_and_blocks_text_model(self):
        result = DocumentQualityGate().evaluate(
            ExtractedDocument(
                path="scan.pdf",
                media_type="pdf",
                text="",
                extractor="none",
                warnings=["扫描版 PDF 未识别出可用文本。"],
                fragments=[],
            ),
            document_kind="credit",
        )

        self.assertEqual(result["status"], "manual_required")
        self.assertFalse(result["can_use_text_model"])
        self.assertEqual(result["fallback"], "human")

    def test_low_confidence_ocr_text_can_use_text_model_enhancement(self):
        text = "资产负债率45%，流动比率1.5，主体评级AA。" * 6
        result = DocumentQualityGate().evaluate(
            ExtractedDocument(
                path="scan.pdf",
                media_type="pdf",
                text=text,
                extractor="pypdf+tesseract",
                warnings=[],
                fragments=[
                    DocumentFragment(
                        "page-1",
                        text,
                        {
                            "kind": "page",
                            "page": 1,
                            "ocr": True,
                            "ocr_confidence": 0.62,
                        },
                    )
                ],
            ),
            document_kind="credit",
        )

        self.assertEqual(result["status"], "needs_text_enhancement")
        self.assertTrue(result["can_use_text_model"])
        self.assertEqual(result["fallback"], "text_model")


class StubDocumentEnhancer:
    available = True
    model = "text-only-test-model"

    def enhance(self, document_kind, *, document):
        fragment = document["fragments"][0]
        return {
            "status": "succeeded",
            "mode": "text_only",
            "model": self.model,
            "summary": "从低置信OCR文字中恢复字段。",
            "candidates": [
                {
                    "field": "asset_liability_ratio",
                    "value": 0.45,
                    "confidence": 0.91,
                    "evidence_query": "资产负债率45%",
                    "document_id": document["document_id"],
                    "fragment_id": fragment["fragment_id"],
                    "location": fragment["location"],
                }
            ],
            "verification": {"status": "passed"},
            "limitation": "只能处理本地已提取文字。",
        }


class DocumentTextEnhancementWorkflowTests(unittest.TestCase):
    def test_low_quality_text_is_enhanced_without_claiming_multimodal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "低质量信用资料.txt"
            source.write_text(
                "资产负债率45%，流动比率1.5，主体评级AA。" * 8,
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DONGJIANG_DOCUMENT_TEXT_ENHANCEMENT_ENABLED": "true"},
            ):
                with DongjiangWorkflowHarness(
                    checkpoint_path=root / "workflow.sqlite",
                    repository=CaseRepository(root / "cases"),
                    vault_dir=root / "vault",
                    inbox_dir=root / "inbox",
                    output_dir=root / "output",
                ) as harness:
                    harness.nodes.document_enhancer = StubDocumentEnhancer()
                    harness.nodes.tool_registry._handlers[
                        "document.text_enhance"
                    ] = lambda payload: harness.nodes.document_enhancer.enhance(
                        payload["document_kind"], document=payload["document"]
                    )
                    original = harness.nodes.document_quality.evaluate
                    harness.nodes.document_quality.evaluate = lambda document, document_kind: {
                        **original(document, document_kind=document_kind),
                        "status": "needs_text_enhancement",
                        "can_use_text_model": True,
                        "fallback": "text_model",
                    }
                    harness.nodes.tool_registry._handlers[
                        "document.quality_gate"
                    ] = lambda payload: harness.nodes.document_quality.evaluate(
                        harness.nodes._document_from_tool(payload["document"]),
                        document_kind=payload["document_kind"],
                    )
                    run = harness.start(
                        {
                            "customer_name": "文本增强测试客户",
                            "customer_type": "new",
                            "business_type": "TKP",
                            "monthly_order_amount": 1_000_000,
                            "current_ratio": 1.5,
                            "external_rating": "AA",
                        },
                        file_paths=[str(source)],
                        use_cached_credit=False,
                    )

            document = run.state["source_documents"][0]
            self.assertEqual(document["text_enhancement"]["mode"], "text_only")
            self.assertEqual(document["text_enhancement"]["status"], "succeeded")
            self.assertEqual(run.state["customer"]["asset_liability_ratio"], 0.45)
            self.assertTrue(any(
                item.get("tool_name") == "document.text_enhance"
                for item in run.state["tool_calls"]
            ))
            self.assertNotIn("multimodal", str(document).lower())

    def test_semantic_failure_keeps_successful_local_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "可读信用资料.txt"
            source.write_text(
                "资产负债率45%，流动比率1.5，主体评级AA。" * 8,
                encoding="utf-8",
            )
            with DongjiangWorkflowHarness(
                checkpoint_path=root / "workflow.sqlite",
                repository=CaseRepository(root / "cases"),
                vault_dir=root / "vault",
                inbox_dir=root / "inbox",
                output_dir=root / "output",
            ) as harness:
                harness.nodes.credit_facts.enrich = lambda *args, **kwargs: (
                    (_ for _ in ()).throw(RuntimeError("semantic extraction failed"))
                )
                run = harness.start(
                    {
                        "customer_name": "后处理失败测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "current_ratio": 1.5,
                        "external_rating": "AA",
                    },
                    file_paths=[str(source)],
                    use_cached_credit=False,
                )

            document = run.state["source_documents"][0]
            self.assertEqual(document["parse_status"], "parsed")
            self.assertEqual(document["semantic_status"], "failed")
            self.assertEqual(document["processing_error"], "semantic extraction failed")
            self.assertTrue(document["fragments"])


if __name__ == "__main__":
    unittest.main()
