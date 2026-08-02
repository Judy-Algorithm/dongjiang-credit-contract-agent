#!/usr/bin/env python3
"""Check HKGAI environment configuration without printing any credential."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from dongjiang_agent.config import load_local_env  # noqa: E402


CORE_REQUIRED = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
)
OPTIONAL = (
    "HKGAI_SPEECH_BASE_URL",
    "HKGAI_SPEECH_API_KEY",
    "HKGAI_TOOLHUB_BASE_URL",
    "HKGAI_AGENTHUB_BASE_URL",
    "HKGAI_APP_NAME",
    "HKGAI_APP_KEY",
)
KNOWN_KEYS = CORE_REQUIRED + OPTIONAL


def _env_file_keys(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    keys: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def main() -> int:
    process_keys = {name for name in KNOWN_KEYS if os.getenv(name)}
    loaded = load_local_env()
    file_keys = _env_file_keys(loaded) if loaded else set()
    missing_required = [name for name in CORE_REQUIRED if not os.getenv(name)]
    missing_optional = [name for name in OPTIONAL if not os.getenv(name)]
    result: dict[str, object] = {
        "env_file_loaded": bool(loaded),
        "configured": {name: bool(os.getenv(name)) for name in KNOWN_KEYS},
        "source": {
            name: "process_environment"
            if name in process_keys
            else "env_file"
            if name in file_keys
            else "missing"
            for name in KNOWN_KEYS
        },
        "text_model": os.getenv("OPENAI_MODEL") or "",
        "optional_capabilities_complete": not missing_optional,
    }
    if missing_required:
        result["ok"] = False
        result["missing_required"] = missing_required
        result["missing_optional"] = missing_optional
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    if missing_optional:
        result["missing_optional"] = missing_optional

    request = Request(
        f"{os.environ['OPENAI_BASE_URL'].rstrip('/')}/models",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        model_ids = {
            str(item.get("id"))
            for item in payload.get("data", [])
            if item.get("id")
        }
        result["text_api_reachable"] = True
        result["selected_model_available"] = os.environ["OPENAI_MODEL"] in model_ids
        result["ok"] = bool(result["selected_model_available"])
    except HTTPError as exc:
        result.update(ok=False, text_api_reachable=False, http_status=exc.code)
    except URLError as exc:
        result.update(ok=False, text_api_reachable=False, error=str(exc.reason))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
