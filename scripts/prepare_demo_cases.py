#!/usr/bin/env python3
"""Create three idempotent synthetic cases for the competition demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dongjiang_agent.persistence import CaseRepository  # noqa: E402
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness  # noqa: E402


SAFE_CONTRACT = """销售合同
甲方：东江集团；乙方：{customer}。
合同标的：精密组件。合同金额：80万元。信用额度：80万元。
付款及账期：月结{term}天。知识产权：双方背景知识产权各自所有。保密：不得披露。
违约责任：违约方赔偿可证明的直接损失，累计不超过合同金额。
解除与终止：重大违约经书面催告30日未改正后可以解除。
争议解决：适用中华人民共和国法律，由深圳市人民法院管辖。
"""


def harness(root: Path) -> DongjiangWorkflowHarness:
    return DongjiangWorkflowHarness(
        checkpoint_path=root / "workflow" / "checkpoints.sqlite",
        repository=CaseRepository(root / "cases"),
        vault_dir=root / "vault",
        inbox_dir=root / "workflow" / "inbox",
        output_dir=root.parent / "output",
        evidence_dir=root / "evidence",
        archive_dir=root / "archive",
    )


def base_customer(name: str) -> dict[str, object]:
    return {
        "customer_name": name,
        "customer_type": "new",
        "business_type": "TKP",
        "project_name": "比赛演示项目",
        "monthly_order_amount": 1_000_000,
        "external_rating": "AA",
        "asset_liability_ratio": 0.45,
        "current_ratio": 1.5,
    }


def create_cases(root: Path) -> list[dict[str, str]]:
    actor = ActorContext("demo-system", ("system",), "demo", "演示数据生成器")
    rows: list[dict[str, str]] = []
    with harness(root) as workflow:
        supplement = workflow.start(
            {
                "customer_name": "演示-资料不足客户",
                "customer_type": "new",
                "business_type": "TKP",
                "project_name": "资料补件演示",
                "monthly_order_amount": 1_000_000,
                "external_rating": "AAA",
            },
            use_cached_credit=False,
            actor=actor,
        )
        rows.append({"scenario": "资料不足进入补件", "case_id": supplement.case_id, "status": supplement.status})

        special = workflow.start(base_customer("演示-TKP120天客户"), use_cached_credit=False, actor=actor)
        special = workflow.resume(special.case_id, {"action": "approve"}, actor=actor)
        special = workflow.resume(
            special.case_id,
            {
                "action": "submit_contract",
                "contract_texts": [SAFE_CONTRACT.format(customer="演示-TKP120天客户", term=120)],
            },
            actor=actor,
        )
        rows.append({"scenario": "TKP 120天进入特批", "case_id": special.case_id, "status": special.status})

        blocked = workflow.start(base_customer("演示-无责取消客户"), use_cached_credit=False, actor=actor)
        blocked = workflow.resume(blocked.case_id, {"action": "approve"}, actor=actor)
        risky = SAFE_CONTRACT.format(customer="演示-无责取消客户", term=60).replace(
            "违约责任：违约方赔偿可证明的直接损失，累计不超过合同金额。",
            "买方可随时取消订单且不承担任何责任。",
        )
        blocked = workflow.resume(
            blocked.case_id,
            {"action": "submit_contract", "contract_texts": [risky]},
            actor=actor,
        )
        rows.append({"scenario": "无责取消进入合同修订", "case_id": blocked.case_id, "status": blocked.status})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="生成比赛用合成演示案件")
    parser.add_argument("--root", default="data", help="数据根目录，默认 data")
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    if not root.is_absolute():
        root = PROJECT_ROOT / root
    manifest = root / "demo" / "cases.json"
    if manifest.is_file():
        existing = json.loads(manifest.read_text(encoding="utf-8"))
        case_root = root / "cases"
        if all((case_root / f"{item['case_id']}.json").is_file() for item in existing):
            print(json.dumps({"created": False, "cases": existing}, ensure_ascii=False, indent=2))
            return 0
    rows = create_cases(root)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"created": True, "cases": rows}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
