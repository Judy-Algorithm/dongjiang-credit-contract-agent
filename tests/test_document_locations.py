import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from dongjiang_agent.ingestion import DocumentExtractor, locate_excerpt


class DocumentLocationTests(unittest.TestCase):
    def test_text_fragments_keep_line_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.txt"
            path.write_text("第一行\n\n买方可取消订单且不承担任何责任。\n第四行", encoding="utf-8")
            document = DocumentExtractor().extract(path)
            self.assertEqual(document.fragments[1].location, {"kind": "line", "line": 3})
            matched = locate_excerpt("取消订单且不承担任何责任", [
                {"fragment_id": item.fragment_id, "text": item.text, "location": item.location}
                for item in document.fragments
            ])
            self.assertEqual(matched["fragment_id"], "line-3")

    def test_docx_fragments_keep_paragraph_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract.docx"
            document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:r><w:t>合同标题</w:t></w:r></w:p>
<w:p><w:r><w:t>账期为120天。</w:t></w:r></w:p>
</w:body></w:document>"""
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", document_xml)
            document = DocumentExtractor().extract(path)
            self.assertEqual(document.fragments[1].location, {"kind": "paragraph", "paragraph": 2})

    def test_docx_table_cells_keep_structure_and_merge_spans(self):
        from docx import Document

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "contract-table.docx"
            document = Document()
            document.add_paragraph("合同标题")
            table = document.add_table(rows=3, cols=3)
            table.cell(0, 0).merge(table.cell(0, 1)).text = "付款条款"
            table.cell(0, 2).text = "账期"
            table.cell(1, 0).merge(table.cell(2, 0)).text = "商务条件"
            table.cell(1, 1).text = "月结"
            table.cell(1, 2).text = "120天"
            table.cell(2, 1).text = "币种"
            table.cell(2, 2).text = "人民币"
            document.save(path)

            extracted = DocumentExtractor().extract(path)
            heading = next(item for item in extracted.fragments if item.text == "付款条款")
            vertical = next(item for item in extracted.fragments if item.text == "商务条件")
            term = next(item for item in extracted.fragments if item.text == "120天")
            self.assertEqual(
                heading.location,
                {
                    "kind": "word_table_cell",
                    "table": 1,
                    "row": 1,
                    "column": 1,
                    "row_span": 1,
                    "column_span": 2,
                },
            )
            self.assertEqual(vertical.location["row_span"], 2)
            self.assertEqual(term.fragment_id, "table-1-row-2-column-3")
            matched = locate_excerpt(
                "账期120天",
                [
                    {
                        "fragment_id": item.fragment_id,
                        "text": item.text,
                        "location": item.location,
                    }
                    for item in extracted.fragments
                ],
            )
            self.assertEqual(matched["fragment_id"], term.fragment_id)

    def test_xlsx_fragments_keep_sheet_and_cell(self):
        import openpyxl

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credit.xlsx"
            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.title = "财务数据"
            sheet["B3"] = "资产负债率45%"
            workbook.save(path)
            workbook.close()
            document = DocumentExtractor().extract(path)
            fragment = next(item for item in document.fragments if item.text == "资产负债率45%")
            self.assertEqual(fragment.location, {"kind": "cell", "sheet": "财务数据", "cell": "B3"})

    def test_pdf_fragment_keeps_page_number(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            with path.open("wb") as handle:
                writer.write(handle)
            document = DocumentExtractor().extract(path)
            self.assertEqual(document.media_type, "pdf")
            self.assertEqual(document.fragments, [])
            self.assertTrue(document.warnings)

    def test_scanned_pdf_ocr_keeps_page_number_and_confidence(self):
        from pypdf import PdfWriter

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scanned.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=200, height=200)
            writer.add_blank_page(width=200, height=200)
            with path.open("wb") as handle:
                writer.write(handle)
            with patch.object(
                DocumentExtractor,
                "_ocr_pdf_pages",
                return_value=({2: ("付款账期120天", 0.913)}, []),
            ) as ocr:
                document = DocumentExtractor().extract(path)
            ocr.assert_called_once_with(path.resolve(), [1, 2])
            self.assertEqual(document.extractor, "pypdf+tesseract")
            self.assertEqual(document.fragments[0].fragment_id, "page-2")
            self.assertEqual(
                document.fragments[0].location,
                {
                    "kind": "page",
                    "page": 2,
                    "ocr": True,
                    "ocr_confidence": 0.913,
                },
            )

    def test_ocr_normalization_keeps_latin_spaces_and_joins_cjk(self):
        text = DocumentExtractor._normalize_ocr_text(
            "合 同 金 额 : 人 民 币 100 万 元\nBuyer and Seller agree"
        )
        self.assertEqual(
            text,
            "合同金额:人民币 100 万元\nBuyer and Seller agree",
        )


if __name__ == "__main__":
    unittest.main()
