"""Load local development secrets without adding a runtime dependency."""

from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def load_local_env(path: str | Path | None = None) -> Path | None:
    """Load KEY=VALUE pairs from .env without overwriting real environment values."""
    target = Path(path).expanduser() if path else DEFAULT_ENV_PATH
    if not target.is_file():
        return None
    for line_number, raw_line in enumerate(
        target.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ValueError(f"{target}:{line_number} 不是有效的 KEY=VALUE 配置。")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"{target}:{line_number} 缺少环境变量名称。")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return target
