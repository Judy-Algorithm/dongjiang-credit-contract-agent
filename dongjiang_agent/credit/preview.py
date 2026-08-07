"""Safe, pre-submission extraction for the credit application form."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from ..domain.models import CreditProfile
from ..ingestion.extractors import DocumentExtractor, ExtractedDocument
from .extractor import CreditFactExtractor


PREVIEW_FIELDS = (
    "registered_capital",
    "years_in_business",
    "asset_liability_ratio",
    "net_margin",
    "current_ratio",
    "revenue_growth",
    "monthly_order_amount",
    "cooperation_years",
    "overdue_count_12m",
    "max_overdue_days_12m",
    "on_time_payment_rate",
    "outstanding_receivables_amount",
    "open_order_amount",
    "current_overdue_days",
    "last_order_date",
)
PERCENT_FIELDS = {
    "asset_liability_ratio",
    "net_margin",
    "revenue_growth",
    "on_time_payment_rate",
}


def _looks_like_contract(document: ExtractedDocument) -> bool:
    name = Path(document.path).name.lower()
    if any(
        token in name
        for token in (
            "合同",
            "协议",
            "订单",
            "采购单",
            "contract",
            "agreement",
            "purchase order",
            "sales order",
        )
    ):
        return True
    signals = (
        "甲方",
        "乙方",
        "买方",
        "卖方",
        "采购方",
        "供应商",
        "订单",
        "付款",
        "违约责任",
        "争议解决",
        "payment terms",
        "buyer",
        "seller",
        "supplier",
    )
    lowered = document.text.lower()
    return sum(signal.lower() in lowered for signal in signals) >= 2


class CreditDocumentPreviewService:
    """Extract form-safe facts without persisting uploaded source documents."""

    def __init__(self) -> None:
        self.document_extractor = DocumentExtractor()
        self.fact_extractor = CreditFactExtractor()

    def preview(self, paths: Iterable[str | Path]) -> dict[str, Any]:
        profile = CreditProfile(customer_name="资料预解析")
        documents: list[dict[str, Any]] = []
        for path in paths:
            source = Path(path).name
            try:
                document = self.document_extractor.extract(path)
                if _looks_like_contract(document):
                    documents.append(
                        {
                            "name": source,
                            "status": "rejected",
                            "message": "检测为合同或订单，请在信审通过后从合同入口上传。",
                        }
                    )
                    continue
                profile = self.fact_extractor.enrich(profile, document.text, source)
                documents.append(
                    {
                        "name": source,
                        "status": "parsed",
                        "extractor": document.extractor,
                        "warnings": list(document.warnings),
                    }
                )
            except (FileNotFoundError, OSError, ValueError) as exc:
                documents.append(
                    {"name": source, "status": "failed", "message": str(exc)}
                )

        fields: dict[str, Any] = {}
        for field in PREVIEW_FIELDS:
            value = getattr(profile, field)
            if value is None or value == "":
                continue
            fields[field] = value * 100 if field in PERCENT_FIELDS else value
        ratings = [asdict(item) for item in profile.external_ratings]
        return {
            "fields": fields,
            "external_ratings": ratings,
            "documents": documents,
            "parsed_document_count": sum(
                item["status"] == "parsed" for item in documents
            ),
            "extracted_field_count": len(fields) + len(ratings),
        }
