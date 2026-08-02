import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.persistence import CaseRepository
from dongjiang_agent.runtime import (
    AgentDescriptor,
    InvocationContext,
    LocalAgentRegistry,
    LocalToolRegistry,
    ToolDescriptor,
)
from dongjiang_agent.workflow import DongjiangWorkflowHarness


class RegistryContractTests(unittest.TestCase):
    def test_agent_registry_discovers_and_invokes_without_logging_payload(self):
        registry = LocalAgentRegistry()
        registry.register(
            AgentDescriptor(
                agent_id="credit_review",
                name="信用评审子 Agent",
                description="test",
                capabilities=("credit_review",),
            ),
            lambda payload: {
                "stage": "done",
                "active_workflow_plan": {
                    "plan_id": "CREDIT-1",
                    "status": "completed",
                    "tasks": [],
                },
            },
        )
        result = registry.invoke(
            "credit_review",
            {"secret": "never-log-this"},
            context=InvocationContext(
                case_id="DJ-REGISTRY",
                agent_id="credit_review",
            ),
        )

        self.assertEqual(registry.discover("credit_review")[0]["agent_id"], "credit_review")
        self.assertEqual(result.record["provider"], "local")
        self.assertEqual(result.record["plan_id"], "CREDIT-1")
        self.assertNotIn("never-log-this", str(result.record))

    def test_tool_registry_enforces_agent_allowlist(self):
        registry = LocalToolRegistry()
        registry.register(
            ToolDescriptor(
                tool_name="credit.assess",
                name="信用评分",
                description="test",
                allowed_agents=("credit_review",),
            ),
            lambda payload: {"score": 80},
        )

        allowed = registry.invoke(
            "credit.assess",
            {"customer": "redacted"},
            context=InvocationContext(agent_id="credit_review"),
        )
        self.assertEqual(allowed.output["score"], 80)
        with self.assertRaises(PermissionError):
            registry.invoke(
                "credit.assess",
                {},
                context=InvocationContext(agent_id="contract_review"),
            )


class RegistryWorkflowTests(unittest.TestCase):
    def test_credit_subagent_and_tools_are_invoked_through_registries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with DongjiangWorkflowHarness(
                checkpoint_path=root / "workflow.sqlite",
                repository=CaseRepository(root / "cases"),
                vault_dir=root / "vault",
                inbox_dir=root / "inbox",
                output_dir=root / "output",
            ) as harness:
                run = harness.start(
                    {
                        "customer_name": "注册中心测试客户",
                        "customer_type": "new",
                        "business_type": "TKP",
                        "monthly_order_amount": 1_000_000,
                        "external_rating": "AA",
                        "asset_liability_ratio": 0.45,
                        "current_ratio": 1.5,
                    },
                    use_cached_credit=False,
                )

                self.assertEqual(
                    {item["agent_id"] for item in harness.agent_registry.discover()},
                    {"credit_review", "contract_review"},
                )
                self.assertTrue(
                    any(
                        item.get("agent_id") == "credit_review"
                        for item in run.state.get("agent_invocations") or []
                    )
                )
                self.assertTrue(
                    any(
                        item.get("tool_name") == "credit.analyze_dimension"
                        for item in run.state.get("tool_calls") or []
                    )
                )
                self.assertTrue(harness.tool_registry.discover(agent_id="credit_review"))


if __name__ == "__main__":
    unittest.main()
