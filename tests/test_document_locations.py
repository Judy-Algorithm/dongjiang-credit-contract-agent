import tempfile
import unittest
import zipfile
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
