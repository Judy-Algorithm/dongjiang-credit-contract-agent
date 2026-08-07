from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


TEMPLATE_FIELDS = {
    "customer_name",
    "unified_social_credit_code",
    "crm_customer_id",
    "customer_type",
    "business_type",
    "tkm_business_subtype",
    "project_name",
    "contract_amount",
    "requested_credit_limit",
    "requested_term_days",
    "currency",
    "application_reason",
    "purchase_exemption_requested",
    "monthly_order_amount",
    "registered_capital",
    "years_in_business",
    "asset_liability_ratio",
    "net_margin",
    "current_ratio",
    "revenue_growth",
    "cooperation_years",
    "overdue_count_12m",
    "max_overdue_days_12m",
    "on_time_payment_rate",
    "outstanding_receivables_amount",
    "open_order_amount",
    "current_overdue_days",
    "last_order_date",
    "external_ratings",
}
RATING_FIELDS = {"agency", "rating", "outlook", "rating_date", "source"}


def _iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _example(
    *,
    template_id: str,
    name: str,
    description: str,
    customer_name: str,
    business_type: str,
    project_name: str,
    monthly_order_amount: float,
    contract_amount: float,
    requested_credit_limit: float,
    requested_term_days: int,
    tkm_business_subtype: str = "",
    purchase_exemption_requested: bool = False,
) -> dict[str, Any]:
    return {
        "template_id": template_id,
        "name": name,
        "description": description,
        "system": True,
        "deletable": False,
        "created_at": "",
        "updated_at": "",
        "data": {
            "customer_name": customer_name,
            "unified_social_credit_code": "",
            "crm_customer_id": "",
            "customer_type": "new",
            "business_type": business_type,
            "tkm_business_subtype": tkm_business_subtype,
            "project_name": project_name,
            "contract_amount": contract_amount,
            "requested_credit_limit": requested_credit_limit,
            "requested_term_days": requested_term_days,
            "currency": "CNY",
            "application_reason": "用于功能演示的合成申请数据，请按实际客户情况修改后再提交。",
            "purchase_exemption_requested": purchase_exemption_requested,
            "monthly_order_amount": monthly_order_amount,
            "registered_capital": 50_000_000,
            "years_in_business": 12,
            "asset_liability_ratio": 0.42,
            "net_margin": 0.11,
            "current_ratio": 1.8,
            "revenue_growth": 0.12,
            "cooperation_years": None,
            "overdue_count_12m": 0,
            "max_overdue_days_12m": 0,
            "on_time_payment_rate": None,
            "outstanding_receivables_amount": 0,
            "open_order_amount": 0,
            "current_overdue_days": 0,
            "last_order_date": "",
            "external_ratings": [
                {
                    "agency": "中诚信国际",
                    "rating": "AA",
                    "outlook": "稳定",
                    "rating_date": "2026-06-30",
                    "source": "示例模板",
                }
            ],
        },
    }


SYSTEM_TEMPLATES = (
    _example(
        template_id="SYSTEM-TKP-STANDARD",
        name="示例 · TKP 常规合作",
        description="新客户、60天账期、资料较完整的常规信用申请。",
        customer_name="华南精密制造有限公司",
        business_type="TKP",
        project_name="汽车精密零部件年度供货",
        monthly_order_amount=1_000_000,
        contract_amount=1_500_000,
        requested_credit_limit=2_000_000,
        requested_term_days=60,
    ),
    _example(
        template_id="SYSTEM-TKM-AUTOMOTIVE",
        name="示例 · TKM 汽车及标准业务",
        description="汽车及标准模具业务的常规申请数据。",
        customer_name="湾区汽车科技有限公司",
        business_type="TKM",
        tkm_business_subtype="automotive_standard",
        project_name="汽车标准模具采购项目",
        monthly_order_amount=800_000,
        contract_amount=2_400_000,
        requested_credit_limit=3_000_000,
        requested_term_days=180,
    ),
    _example(
        template_id="SYSTEM-TKM-PRECISION",
        name="示例 · TKM 精密模具特批",
        description="精密模具并申请首期采购款豁免的授权审批场景。",
        customer_name="东南精密工业有限公司",
        business_type="TKM",
        tkm_business_subtype="precision",
        project_name="高精度注塑模具定制项目",
        monthly_order_amount=1_200_000,
        contract_amount=3_600_000,
        requested_credit_limit=4_000_000,
        requested_term_days=180,
        purchase_exemption_requested=True,
    ),
)


class RequestTemplateStore:
    def __init__(self, path: str | Path = "data/templates/request-templates.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS request_templates (
                template_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL COLLATE NOCASE,
                description TEXT NOT NULL DEFAULT '',
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_request_templates_user_name
                ON request_templates(user_id, name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS idx_request_templates_user_updated
                ON request_templates(user_id, updated_at DESC);
            """
        )
        self.connection.commit()

    @staticmethod
    def _normalize_name(value: str) -> str:
        name = re.sub(r"\s+", " ", str(value or "")).strip()
        if not name:
            raise ValueError("请填写模板名称。")
        if len(name) > 60:
            raise ValueError("模板名称不能超过60个字符。")
        return name

    @staticmethod
    def _normalize_description(value: str) -> str:
        description = re.sub(r"\s+", " ", str(value or "")).strip()
        if len(description) > 160:
            raise ValueError("模板说明不能超过160个字符。")
        return description

    @staticmethod
    def _scalar(value: Any) -> str | int | float | bool | None:
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if not math.isfinite(float(value)) or abs(float(value)) > 1e15:
                raise ValueError("模板包含无效数字。")
            return value
        if isinstance(value, str):
            if len(value) > 2000:
                raise ValueError("模板字段内容过长。")
            return value
        raise ValueError("模板字段格式无效。")

    @classmethod
    def sanitize_data(cls, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("模板内容格式无效。")
        data: dict[str, Any] = {}
        for key in TEMPLATE_FIELDS - {"external_ratings"}:
            if key in raw:
                data[key] = cls._scalar(raw[key])
        ratings: list[dict[str, Any]] = []
        raw_ratings = raw.get("external_ratings") or []
        if not isinstance(raw_ratings, list):
            raise ValueError("第三方评级模板格式无效。")
        for item in raw_ratings[:5]:
            if not isinstance(item, dict):
                raise ValueError("第三方评级模板格式无效。")
            rating = {
                key: cls._scalar(item[key])
                for key in RATING_FIELDS
                if key in item
            }
            if rating:
                ratings.append(rating)
        data["external_ratings"] = ratings
        encoded = json.dumps(data, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("模板内容不能超过64KB。")
        return data

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "template_id": row["template_id"],
            "name": row["name"],
            "description": row["description"],
            "system": False,
            "deletable": True,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "data": json.loads(str(row["data_json"] or "{}")),
        }

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT * FROM request_templates
            WHERE user_id = ? ORDER BY updated_at DESC, name COLLATE NOCASE
            """,
            (str(user_id),),
        ).fetchall()
        return [dict(item) for item in SYSTEM_TEMPLATES] + [self._public(row) for row in rows]

    def create(
        self,
        user_id: str,
        *,
        name: str,
        description: str = "",
        data: Any,
    ) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("模板所属用户无效。")
        count = self.connection.execute(
            "SELECT COUNT(*) AS count FROM request_templates WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if count and int(count["count"]) >= 50:
            raise ValueError("每个账号最多保存50个申请模板。")
        template_id = f"TPL-{uuid4().hex[:12].upper()}"
        now = _iso()
        normalized = self.sanitize_data(data)
        try:
            self.connection.execute(
                """
                INSERT INTO request_templates (
                    template_id, user_id, name, description, data_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    user_id,
                    self._normalize_name(name),
                    self._normalize_description(description),
                    json.dumps(normalized, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("当前账号已存在同名模板。") from exc
        row = self.connection.execute(
            "SELECT * FROM request_templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()
        assert row is not None
        return self._public(row)

    def delete(self, user_id: str, template_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM request_templates WHERE template_id = ? AND user_id = ?",
            (str(template_id), str(user_id)),
        ).fetchone()
        if not row:
            raise KeyError("模板不存在或不属于当前账号。")
        result = self._public(row)
        self.connection.execute(
            "DELETE FROM request_templates WHERE template_id = ? AND user_id = ?",
            (str(template_id), str(user_id)),
        )
        self.connection.commit()
        return result

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RequestTemplateStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
