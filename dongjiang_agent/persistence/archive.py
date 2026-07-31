"""Immutable, case-level archive for original credit and contract files."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path
from typing import Any

from ..domain.models import utc_now


class CaseDocumentArchive:
    def __init__(self, root: str | Path = "data/archive") -> None:
        self.root = Path(root)

    def archive(
        self,
        case_id: str,
        source_path: str | Path,
        *,
        document_kind: str,
        actor_id: str = "",
        source_system: str = "",
    ) -> dict[str, Any]:
        source = Path(source_path).expanduser().resolve()
        if document_kind not in {"credit", "contract"}:
            raise ValueError("document_kind 必须是 credit 或 contract。")
        if not source.is_file():
            raise FileNotFoundError(f"待归档文件不存在：{source.name}")
        content = source.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        target_dir = self.root / case_id / document_kind
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{digest[:12]}-{source.name}"
        if not target.exists():
            shutil.copy2(source, target)
        return {
            "document_id": f"DOC-{digest[:16].upper()}",
            "document_kind": document_kind,
            "name": source.name,
            "sha256": digest,
            "size_bytes": len(content),
            "archived_path": str(target.resolve()),
            "actor_id": actor_id,
            "source_system": source_system,
            "archived_at": utc_now(),
        }
