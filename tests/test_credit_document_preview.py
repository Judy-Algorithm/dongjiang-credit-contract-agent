from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.credit.preview import CreditDocumentPreviewService


CREDIT_SUPPLEMENT = """华南精密科技（功能测试）有限公司信用补件说明
注册资本：人民币80,000,000元。
成立年限：15年。
资产负债率：45%。
净利率：10%。
流动比率：1.8。
营收增长率：12%。
第三方主体评级：中诚信国际 AA，展望稳定。
当前未收款金额：0元。
在手已入单金额：0元。
当前未收款最长逾期：0天。
"""


class CreditDocumentPreviewTests(unittest.TestCase):
    def test_extracts_credit_supplement_and_preserves_zero_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "05-信用补件说明.txt"
            target.write_text(CREDIT_SUPPLEMENT, encoding="utf-8")
            preview = CreditDocumentPreviewService().preview([target])

        fields = preview["fields"]
        self.assertEqual(fields["registered_capital"], 80_000_000)
        self.assertEqual(fields["years_in_business"], 15)
        self.assertEqual(fields["asset_liability_ratio"], 45)
        self.assertEqual(fields["net_margin"], 10)
        self.assertEqual(fields["current_ratio"], 1.8)
        self.assertEqual(fields["revenue_growth"], 12)
        self.assertEqual(fields["outstanding_receivables_amount"], 0)
        self.assertEqual(fields["open_order_amount"], 0)
        self.assertEqual(fields["current_overdue_days"], 0)
        self.assertEqual(preview["external_ratings"][0]["agency"], "中诚信国际")
        self.assertEqual(preview["external_ratings"][0]["rating"], "AA")
        self.assertEqual(preview["external_ratings"][0]["outlook"], "稳定")

    def test_rejects_contract_in_credit_preview(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "销售合同.txt"
            target.write_text("甲方与乙方约定付款条款和违约责任。", encoding="utf-8")
            preview = CreditDocumentPreviewService().preview([target])

        self.assertEqual(preview["parsed_document_count"], 0)
        self.assertEqual(preview["documents"][0]["status"], "rejected")
        self.assertEqual(preview["fields"], {})


if __name__ == "__main__":
    unittest.main()
