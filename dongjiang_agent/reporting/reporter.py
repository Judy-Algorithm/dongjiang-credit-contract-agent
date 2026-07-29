from __future__ import annotations

import html
import json
from pathlib import Path

from ..domain.models import AuditCase


class AuditReporter:
    def export(self, case: AuditCase, output_dir: str | Path = "output") -> dict[str, str]:
        root = Path(output_dir) / case.case_id
        root.mkdir(parents=True, exist_ok=True)
        json_path = root / "audit-result.json"
        html_path = root / "audit-report.html"
        json_path.write_text(json.dumps(case.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(self._html(case), encoding="utf-8")
        return {"json": str(json_path), "html": str(html_path)}

    @staticmethod
    def _html(case: AuditCase) -> str:
        credit = case.credit_assessment
        review_blocks: list[str] = []
        for index, review in enumerate(case.contract_reviews, start=1):
            findings = "".join(
                f"<tr><td>{html.escape(item.rule_id)}</td><td>{html.escape(item.level.value)}</td>"
                f"<td>{html.escape(item.message)}</td><td>{html.escape(item.suggestion)}</td></tr>"
                for item in review.findings
            ) or "<tr><td colspan='4'>未发现风险</td></tr>"
            review_blocks.append(
                f"<h2>合同 {index}：{html.escape(review.decision.value)}</h2>"
                f"<p>{html.escape(review.summary)}</p>"
                f"<table><thead><tr><th>规则</th><th>等级</th><th>发现</th><th>建议</th></tr></thead>"
                f"<tbody>{findings}</tbody></table>"
            )
        score = f"{credit.score:.1f}" if credit else "-"
        level = credit.risk_level.value if credit else "-"
        limit = f"{credit.approved_credit_limit:,.2f}" if credit else "-"
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>东江一体化信审与合同评审报告</title>
<style>body{{font:15px/1.6 Arial,\"PingFang SC\",sans-serif;max-width:1080px;margin:36px auto;color:#15243b}}
h1{{color:#123d78}}.cards{{display:flex;gap:16px}}.card{{padding:14px 22px;background:#eef5ff;border-radius:10px}}
table{{border-collapse:collapse;width:100%;margin:12px 0 28px}}th,td{{border:1px solid #ccd8e6;padding:9px;text-align:left;vertical-align:top}}
th{{background:#e8f0fa}}small{{color:#64748b}}</style></head><body>
<h1>东江一体化信审与合同评审报告</h1>
<p>案件：{html.escape(case.case_id)}　客户：{html.escape(case.customer.customer_name)}</p>
<div class="cards"><div class="card">信用分<br><b>{score}</b></div>
<div class="card">风险等级<br><b>{html.escape(level)}</b></div>
<div class="card">批准额度<br><b>{limit}</b></div></div>
{''.join(review_blocks)}
<small>政策版本：{html.escape(credit.policy_version if credit else '-')}；本报告为辅助决策，最终结果由授权人员确认。</small>
</body></html>"""
