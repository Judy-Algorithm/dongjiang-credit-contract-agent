"""Run the two contract demonstration flows against the generated files."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.web.presentation import case_view
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness


FILES_ROOT = Path(r"E:\信用评审项目\系统完整测试资料")


def harness(root: Path) -> DongjiangWorkflowHarness:
    return DongjiangWorkflowHarness(
        checkpoint_path=root / "workflow.sqlite",
        repository=CaseRepository(root / "cases"),
        vault_dir=root / "vault",
        inbox_dir=root / "inbox",
        output_dir=root / "output",
        evidence_dir=root / "evidence",
    )


def run_four_role_case() -> dict[str, object]:
    main_files = [
        FILES_ROOT / "12-四角色主案例-中文合同.docx",
        FILES_ROOT / "13-四角色主案例-英文合同.xlsx",
        FILES_ROOT / "14-四角色主案例-越南语合同.pdf",
    ]
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        credit_evidence = root / "credit-release.txt"
        credit_evidence.write_text(
            "OA特别放行批准：仅限本案，2026-12-31前有效。", encoding="utf-8"
        )
        contract_evidence = root / "contract-exception.txt"
        contract_evidence.write_text(
            "OA合同例外批准：超额度及超账期仅限本案。", encoding="utf-8"
        )
        with harness(root) as workflow:
            run = workflow.start(
                {
                    "customer_name": "星越汽车科技（四角色演示）有限公司",
                    "unified_social_credit_code": "91440300DEMO260801",
                    "crm_customer_id": "CRM-DEMO-4ROLE-001",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "project_name": "新能源汽车精密组件",
                    "monthly_order_amount": 1_000_000,
                    "registered_capital": 80_000_000,
                    "years_in_business": 15,
                    "asset_liability_ratio": 0.45,
                    "net_margin": 0.10,
                    "current_ratio": 1.8,
                    "revenue_growth": 0.12,
                    "external_rating": "AA",
                    "overdue_count_12m": 0,
                    "max_overdue_days_12m": 0,
                    "on_time_payment_rate": 1.0,
                    "outstanding_receivables_amount": 1_500_000,
                    "open_order_amount": 1_000_000,
                    "current_overdue_days": 45,
                },
                use_cached_credit=False,
                actor=ActorContext("business.demo", ("sales",), "web"),
            )
            assert run.waiting_for == "credit_approval"
            run = workflow.resume(
                run.case_id,
                {"action": "approve", "comment": "信用审批员确认模型建议"},
                actor=ActorContext("credit.demo", ("credit",), "web"),
            )
            assert run.waiting_for == "special_release"
            run = workflow.resume(
                run.case_id,
                {
                    "action": "approve",
                    "comment": "授权本案信用控制特别放行",
                    "approval_scope": "仅限本案订单",
                    "validity_days": 30,
                    "oa_evidence_id": "OA-RELEASE-260807",
                    "file_paths": [str(credit_evidence)],
                },
                actor=ActorContext("approval.demo", ("director",), "web"),
            )
            assert run.waiting_for == "contract_upload"
            run = workflow.resume(
                run.case_id,
                {
                    "action": "submit_contract",
                    "file_paths": [str(item) for item in main_files],
                },
                actor=ActorContext("business.demo", ("sales",), "web"),
            )
            assert run.waiting_for == "manager_approval", run.state.get("errors")
            run = workflow.resume(
                run.case_id,
                {
                    "action": "approve",
                    "comment": "批准合同例外并转法务",
                    "approval_scope": "仅限本案三份合同附件",
                    "validity_days": 30,
                    "oa_evidence_id": "OA-CONTRACT-260807",
                    "file_paths": [str(contract_evidence)],
                },
                actor=ActorContext("approval.demo", ("director",), "web"),
            )
            assert run.waiting_for == "contract_approval"
            run = workflow.resume(
                run.case_id,
                {
                    "action": "approve",
                    "comment": "法务结合制度风险与AI辅助发现最终批准",
                },
                actor=ActorContext("legal.demo", ("legal",), "web"),
            )
            view = case_view(run.state)
            assert run.status == "approved_by_exception"
            assert {item["language"] for item in view["contract_package"]} == {
                "zh", "en", "vi"
            }
            assert all(
                item["redaction"]["masked_occurrence_count"] > 0
                for item in view["contract_package"]
            )
            return {
                "case_id": run.case_id,
                "status": run.status,
                "languages": [item["language"] for item in view["contract_package"]],
                "media_types": [item["media_type"] for item in view["contract_package"]],
                "masked_counts": [
                    item["redaction"]["masked_occurrence_count"]
                    for item in view["contract_package"]
                ],
                "risk_counts": [
                    item["rule_finding_count"] for item in view["contract_package"]
                ],
                "credit_release": run.state["special_release"]["purpose"],
                "contract_exception": run.state["contract_exception_approval"][
                    "purpose"
                ],
                "legal_exception_applied": run.state["contract_approval"][
                    "exception_authorization_applied"
                ],
            }


def run_bottom_line_case() -> dict[str, object]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with harness(root) as workflow:
            run = workflow.start(
                {
                    "customer_name": "底线条款演示客户",
                    "customer_type": "new",
                    "business_type": "TKP",
                    "monthly_order_amount": 1_000_000,
                    "external_rating": "AA",
                    "asset_liability_ratio": 0.45,
                    "current_ratio": 1.5,
                },
                use_cached_credit=False,
                actor=ActorContext("business.demo", ("sales",), "web"),
            )
            run = workflow.resume(
                run.case_id,
                {"action": "approve"},
                actor=ActorContext("credit.demo", ("credit",), "web"),
            )
            run = workflow.resume(
                run.case_id,
                {
                    "action": "submit_contract",
                    "file_paths": [
                        str(FILES_ROOT / "15-底线条款退回案例-中文合同.docx")
                    ],
                },
                actor=ActorContext("business.demo", ("sales",), "web"),
            )
            rule_ids = [
                finding["rule_id"]
                for review in run.state.get("contract_reviews") or []
                for finding in review.get("findings") or []
            ]
            assert run.waiting_for == "sales_revision"
            assert "DJ-CANCEL-WITHOUT-LIABILITY" in rule_ids
            return {
                "case_id": run.case_id,
                "status": run.status,
                "waiting_for": run.waiting_for,
                "decision": run.state.get("decision"),
                "rule_ids": rule_ids,
            }


if __name__ == "__main__":
    print(json.dumps({
        "four_role_case": run_four_role_case(),
        "bottom_line_case": run_bottom_line_case(),
    }, ensure_ascii=False, indent=2))
