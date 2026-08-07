import unittest

from dongjiang_agent.web.presentation import case_view


class ContractPackagePresentationTests(unittest.TestCase):
    def test_contract_package_groups_language_redaction_and_risk_by_document(self):
        view = case_view({
            "case_id": "DJ-PACKAGE",
            "customer": {"customer_name": "资料包测试", "business_type": "TKP"},
            "credit_status": "effective",
            "effective_credit_assessment": {
                "score": 80,
                "risk_level": "low",
                "approved_credit_limit": 1_000_000,
                "recommended_term_days": 60,
            },
            "contract_facts": [{
                "contract_name": "english.xlsx",
                "document_id": "DOC-EN",
                "language": "en",
            }],
            "contract_reviews": [{
                "decision": "special_approval",
                "findings": [{
                    "rule_id": "TKP-SPECIAL-TERM",
                    "title": "超账期",
                    "level": "high",
                    "message": "账期超过90天",
                    "suggestion": "提交例外授权",
                    "document_id": "DOC-EN",
                    "fragment_id": "cell-C6",
                    "location": {"kind": "cell", "sheet": "Contract", "cell": "C6"},
                }],
                "ai_assistance": {
                    "status": "succeeded",
                    "model": "demo-model",
                    "summary": "辅助审查完成",
                    "findings": [{
                        "finding_id": "AI-1",
                        "title": "付款风险",
                        "level": "medium",
                        "message": "需要确认付款条件",
                        "suggestion": "法务复核",
                        "document_id": "DOC-EN",
                        "fragment_id": "cell-C6",
                        "location": {"kind": "cell", "sheet": "Contract", "cell": "C6"},
                    }],
                },
            }],
            "source_documents": [{
                "document_kind": "contract",
                "document_id": "DOC-EN",
                "name": "english.xlsx",
                "media_type": "xlsx",
                "extractor": "ooxml",
                "parse_status": "parsed",
                "redaction": {
                    "status": "redacted",
                    "reversible": True,
                    "masked_occurrence_count": 2,
                    "masked_fragment_count": 1,
                    "categories": {"PARTY": 1, "MONEY": 1},
                },
                "fragments": [{
                    "fragment_id": "cell-C6",
                    "text": "⟦PARTY_1234567890⟧ pays ⟦MONEY_1234567890⟧",
                    "location": {"kind": "cell", "sheet": "Contract", "cell": "C6"},
                }],
            }],
        })

        package = view["contract_package"][0]
        self.assertEqual(package["language_label"], "英文")
        self.assertEqual(package["rule_finding_count"], 1)
        self.assertEqual(package["ai_finding_count"], 1)
        self.assertEqual(package["highest_risk"], "high")
        self.assertEqual(package["redaction"]["masked_occurrence_count"], 2)
        self.assertTrue(package["safe_fragments"][0]["contains_redaction"])
        self.assertEqual(view["findings"][0]["document_id"], "DOC-EN")


if __name__ == "__main__":
    unittest.main()
