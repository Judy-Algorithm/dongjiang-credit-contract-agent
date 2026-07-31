from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load_local_env
from .workflow import ActorContext, DongjiangWorkflowHarness


def _load_case(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _run(payload: dict, output: str, no_cache: bool) -> int:
    with DongjiangWorkflowHarness(output_dir=output) as harness:
        run = harness.start(
            payload.get("customer") or {},
            file_paths=payload.get("files") or [],
            contract_texts=payload.get("contract_texts") or [],
            use_cached_credit=not no_cache,
            actor=_actor(payload),
        )
    print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
    return 0


def _actor(payload: dict) -> ActorContext:
    actor = payload.get("actor") or {}
    return ActorContext(
        actor_id=str(actor.get("actor_id") or "cli-user"),
        roles=tuple(actor.get("roles") or ["system"]),
        source_system=str(actor.get("source_system") or "cli"),
    )


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser(description="东江一体化信审与合同评审 Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    audit = subparsers.add_parser(
        "audit",
        aliases=["workflow-start"],
        help="启动一个 JSON 工作流案件",
    )
    audit.add_argument("--case", required=True, help="案件 JSON 文件")
    audit.add_argument("--output", default="output", help="报告输出目录")
    audit.add_argument("--no-cache", action="store_true", help="忽略历史有效信审")
    serve = subparsers.add_parser("serve", help="启动本地业务服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    workflow_status = subparsers.add_parser("workflow-status", help="读取工作流案件状态")
    workflow_status.add_argument("--case-id", required=True)
    workflow_resume = subparsers.add_parser("workflow-resume", help="使用审批或补充资料恢复工作流")
    workflow_resume.add_argument("--case-id", required=True)
    workflow_resume.add_argument("--decision", required=True, help="恢复指令 JSON 文件")
    lifecycle = subparsers.add_parser(
        "lifecycle-sweep", help="执行一年无订单且无欠款客户的授信清零"
    )
    args = parser.parse_args()
    if args.command in {"audit", "workflow-start"}:
        return _run(_load_case(args.case), args.output, args.no_cache)
    if args.command == "serve":
        from .web.server import serve
        serve(args.host, args.port)
        return 0
    if args.command == "workflow-status":
        with DongjiangWorkflowHarness() as harness:
            print(json.dumps(harness.get(args.case_id).to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "workflow-resume":
        payload = _load_case(args.decision)
        with DongjiangWorkflowHarness() as harness:
            run = harness.resume(args.case_id, payload, actor=_actor(payload))
            print(json.dumps(run.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "lifecycle-sweep":
        from .integrations import IntegrationBundle
        from .persistence import CaseRepository

        changed = CaseRepository().apply_inactivity_policy(
            integrations=IntegrationBundle.from_environment()
        )
        print(json.dumps({"inactivated_case_ids": changed}, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
