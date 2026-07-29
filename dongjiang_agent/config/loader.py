from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_POLICY_PATH = Path(__file__).with_name("credit_policy.json")


def load_policy(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path).expanduser() if path else DEFAULT_POLICY_PATH
    with target.open("r", encoding="utf-8") as handle:
        policy = json.load(handle)
    if not isinstance(policy, dict) or "version" not in policy:
        raise ValueError("信用政策配置无效：缺少 version。")
    return policy
