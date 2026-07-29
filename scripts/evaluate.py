#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dongjiang_agent.persistence import CaseRepository  # noqa: E402
from dongjiang_agent.workflow import (  # noqa: E402
    ActorContext,
    DongjiangWorkflowHarness,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="使用用户提供的案件集执行规则回归评测")
    parser.add_argument("--cases", required=True, help="评测案件JSON文件")
    parser.add_argument("--output", default="output/evaluation-report.json")
    args = parser.parse_args()
    source = Path(args.cases).expanduser().resolve()
    cases = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测文件必须是非空案件数组。")
    rows = []
    durations = []
    with tempfile.TemporaryDirectory(prefix="dongjiang-eval-") as temp:
        root = Path(temp)
        with DongjiangWorkflowHarness(
            checkpoint_path=root / "workflow.sqlite",
            repository=CaseRepository(root / "cases"),
            vault_dir=root / "vault",
            inbox_dir=root / "inbox",
            output_dir=root / "output",
        ) as harness:
            for item in cases:
                started = time.perf_counter()
                result = harness.start(
                    item["customer"],
                    use_cached_credit=False,
                    actor=ActorContext("evaluation", ("system",), "evaluation"),
                )
                result = harness.resume(
                    result.case_id,
                    {"action": "approve", "comment": "评测自动批准信审结果"},
                    actor=ActorContext("evaluation", ("system",), "evaluation"),
                )
                result = harness.resume(
                    result.case_id,
                    {"action": "submit_contract", "contract_texts": [item["contract"]]},
                    actor=ActorContext("evaluation", ("system",), "evaluation"),
                )
                elapsed = time.perf_counter() - started
                durations.append(elapsed)
                actual = str(result.state["contract_reviews"][0]["decision"])
                rows.append({
                    "case": item["name"],
                    "expected": item["expected_decision"],
                    "actual": actual,
                    "correct": actual == item["expected_decision"],
                    "latency_ms": round(elapsed * 1000, 2),
                })
    correct = sum(bool(row["correct"]) for row in rows)
    report = {
        "sample_count": len(rows),
        "decision_accuracy": round(correct / len(rows), 4),
        "average_latency_ms": round(statistics.mean(durations) * 1000, 2),
        "p95_latency_ms": round(sorted(durations)[max(0, int(len(durations) * .95) - 1)] * 1000, 2),
        "cases": rows,
        "note": "这是规则回归集准确率，不代表真实业务泛化准确率；需用东江授权案例复核。",
    }
    target = Path(args.output).expanduser()
    if not target.is_absolute():
        target = PROJECT_ROOT / target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if correct == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
