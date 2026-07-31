import json
import unittest

from dongjiang_agent.domain.models import ContractFacts
from dongjiang_agent.llm.contract_assistant import ContractAIAssistant
from dongjiang_agent.web.presentation import case_view


class FakeGateway:
    model = "fake-model"

    def __init__(self, response=None, error=None, available=True):
        self.response = response
        self.error = error
        self._available = available

    @property
    def available(self):
        return self._available

    def analyze_redacted(self, redacted_text, instruction):
        assert "⟦" in redacted_text
        if self.error:
            raise self.error
        return self.response


class ContractAIAssistantTests(unittest.TestCase):
    def test_unconfigured_model_is_non_blocking(self):
        result = ContractAIAssistant(FakeGateway(available=False), enabled=True).review(
            ContractFacts(), redacted_text="⟦REDACTED_TEXT⟧合同", fragments=[]
        )
        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["findings"], [])

    def test_structured_findings_are_normalized_and_located(self):
        payload = json.dumps({
            "summary": "发现一项付款风险",
            "findings": [{
                "id": "AI-PAYMENT-1",
                "title": "付款条件不清晰",
                "level": "high",
                "message": "付款触发条件需要人工确认。",
                "suggestion": "补充验收和付款节点。",
                "evidence_query": "付款应在验收后",
                "confidence": 0.91,
            }],
        })
        result = ContractAIAssistant(FakeGateway(payload), enabled=True).review(
            ContractFacts(document_id="DOC-AI"),
            redacted_text="⟦REDACTED_TEXT⟧付款应在验收后30日支付",
            fragments=[{
                "fragment_id": "line-2",
                "text": "付款应在验收后30日支付",
                "location": {"line": 2},
            }],
        )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["findings"][0]["finding_id"], "AI-PAYMENT-1")
        self.assertEqual(result["findings"][0]["document_id"], "DOC-AI")
        self.assertEqual(result["findings"][0]["fragment_id"], "line-2")
        self.assertEqual(result["findings"][0]["confidence"], 0.91)

    def test_invalid_model_output_falls_back(self):
        result = ContractAIAssistant(FakeGateway("not-json"), enabled=True).review(
            ContractFacts(), redacted_text="⟦REDACTED_TEXT⟧合同", fragments=[]
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["findings"], [])

    def test_ai_findings_are_exposed_without_changing_rule_decision(self):
        view = case_view({
            "case_id": "DJ-AI-TEST",
            "status": "blocked",
            "credit_status": "effective",
            "customer": {"customer_name": "测试客户"},
            "credit_assessment": {
                "score": 80,
                "risk_level": "low",
                "approved_credit_limit": 1000000,
                "recommended_term_days": 90,
            },
            "contract_reviews": [{
                "decision": "block",
                "findings": [{
                    "rule_id": "RULE-1",
                    "title": "制度风险",
                    "level": "blocker",
                    "message": "必须修改",
                    "suggestion": "修改条款",
                }],
                "ai_assistance": {
                    "status": "succeeded",
                    "model": "fake-model",
                    "summary": "辅助摘要",
                    "findings": [{
                        "finding_id": "AI-1",
                        "title": "辅助发现",
                        "level": "medium",
                        "message": "建议复核",
                        "suggestion": "人工确认",
                        "confidence": 0.8,
                    }],
                },
            }],
        })
        self.assertEqual(view["decision"], "block")
        self.assertEqual(len(view["findings"]), 1)
        self.assertEqual(view["ai_assistance"][0]["findings"][0]["finding_id"], "AI-1")


if __name__ == "__main__":
    unittest.main()
