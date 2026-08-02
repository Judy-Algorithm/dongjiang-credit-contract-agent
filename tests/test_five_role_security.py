import json
import tempfile
import unittest
from pathlib import Path

from dongjiang_agent.security import AuthStore
from dongjiang_agent.workflow import ActorContext, DongjiangWorkflowHarness


class FiveRoleSecurityTests(unittest.TestCase):
    def test_account_requires_exactly_one_role(self):
        with tempfile.TemporaryDirectory() as temp:
            with AuthStore(Path(temp) / "auth.sqlite") as store:
                store.bootstrap_admin("admin", "管理员", "AdminPass123")
                with self.assertRaisesRegex(ValueError, "单个角色"):
                    store.create_user(
                        username="multi.user",
                        display_name="多角色用户",
                        password="InitialPass123",
                        roles=["credit_approver", "legal_reviewer"],
                    )
                with self.assertRaisesRegex(ValueError, "单个角色"):
                    store.create_user(
                        username="empty.user",
                        display_name="无角色用户",
                        password="InitialPass123",
                        roles=[],
                    )

    def test_legacy_multi_roles_are_migrated_with_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "auth.sqlite"
            with AuthStore(path) as store:
                admin = store.bootstrap_admin("admin", "管理员", "AdminPass123")
                user = store.create_user(
                    username="legacy.user",
                    display_name="历史用户",
                    password="InitialPass123",
                    roles=["case_submitter"],
                )
                store.connection.execute(
                    "UPDATE users SET roles_json = ? WHERE user_id = ?",
                    (json.dumps(["finance", "legal"]), user["user_id"]),
                )
                store.connection.execute(
                    "UPDATE users SET roles_json = ? WHERE user_id = ?",
                    (json.dumps(["admin", "sales"]), admin["user_id"]),
                )
                store.connection.commit()
            with AuthStore(path) as migrated:
                self.assertEqual(
                    migrated.get_user(user["user_id"])["roles"],
                    ["legal_reviewer"],
                )
                self.assertEqual(
                    migrated.get_user(admin["user_id"])["roles"],
                    ["system_admin"],
                )
                events = [
                    item
                    for item in migrated.list_audit(limit=20)
                    if item["event_type"] == "user.role_migrated"
                ]
                self.assertEqual(len(events), 2)

    def test_multi_target_role_notification_lookup_is_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            with AuthStore(Path(temp) / "auth.sqlite") as store:
                store.bootstrap_admin("admin", "管理员", "AdminPass123")
                credit = store.create_user(
                    username="credit.user",
                    display_name="信用审批人",
                    password="InitialPass123",
                    roles=["credit_approver"],
                )
                legal = store.create_user(
                    username="legal.user",
                    display_name="合同法务",
                    password="InitialPass123",
                    roles=["legal_reviewer"],
                )
                users = store.users_for_roles(
                    ["credit_approver", "legal_reviewer"]
                )
                self.assertEqual(
                    {item["user_id"] for item in users},
                    {credit["user_id"], legal["user_id"]},
                )

    def test_system_admin_has_no_business_approval_bypass(self):
        harness = object.__new__(DongjiangWorkflowHarness)
        with self.assertRaises(PermissionError):
            harness._authorize(
                "credit_approval",
                ActorContext("admin", ("system_admin",), "web", "系统管理员"),
            )
        harness._authorize(
            "credit_approval",
            ActorContext(
                "credit", ("credit_approver",), "web", "信用审批人"
            ),
        )
        with self.assertRaises(PermissionError):
            harness._authorize(
                "finance_legal_review",
                ActorContext(
                    "credit", ("credit_approver",), "web", "信用审批人"
                ),
            )
        harness._authorize(
            "finance_legal_review",
            ActorContext(
                "legal", ("legal_reviewer",), "web", "合同法务"
            ),
        )


if __name__ == "__main__":
    unittest.main()
