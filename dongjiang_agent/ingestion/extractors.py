"""Multi-format extraction with zero-dependency fallbacks."""

from __future__ import annotations

import csv
import html
import io
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(slots=True)
class ExtractedDocument:
    path: str
    media_type: str
    text: str
    extractor: str
    warnings: list[str]


class DocumentExtractor:
    TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}
    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

    def extract(self, path: str | Path) -> ExtractedDocument:
        target = Path(path).expanduser().resolve()
        if not target.is_file():
            raise FileNotFoundError(f"文件不存在：{target}")
        suffix = target.suffix.lower()
        if suffix in self.TEXT_EXTENSIONS:
            text = self._read_text(target)
            return ExtractedDocument(str(target), suffix.lstrip("."), text, "native-text", [])
        if suffix == ".docx":
            return ExtractedDocument(str(target), "docx", self._docx_text(target), "ooxml", [])
        if suffix == ".xlsx":
            return ExtractedDocument(str(target), "xlsx", self._xlsx_text(target), "ooxml", [])
        if suffix == ".pdf":
            text, extractor, warnings = self._pdf_text(target)
            return ExtractedDocument(str(target), "pdf", text, extractor, warnings)
        if suffix in self.IMAGE_EXTENSIONS:
            text, warnings = self._image_text(target)
            return ExtractedDocument(str(target), suffix.lstrip("."), text, "tesseract", warnings)
        raise ValueError(f"暂不支持的文件格式：{suffix or '无扩展名'}")

    @staticmethod
    def _read_text(path: Path) -> str:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = raw.decode(encoding)
                if path.suffix.lower() == ".csv":
                    rows = csv.reader(io.StringIO(text))
                    return "\n".join(" | ".join(cell.strip() for cell in row) for row in rows)
                if path.suffix.lower() in {".html", ".htm"}:
                    text = re.sub(r"<[^>]+>", " ", text)
                    return html.unescape(re.sub(r"\s+", " ", text))
                return text
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _docx_text(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs: list[str] = []
        for paragraph in root.findall(".//w:p", namespace):
            chunks = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
            if chunks:
                paragraphs.append("".join(chunks))
        return "\n".join(paragraphs)

    @staticmethod
    def _xlsx_text(path: Path) -> str:
        try:
            import openpyxl  # type: ignore
        except ImportError:
            return DocumentExtractor._xlsx_text_ooxml(path)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines: list[str] = []
        for sheet in workbook.worksheets:
            lines.append(f"[工作表] {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                values = [str(value).strip() if value is not None else "" for value in row]
                if any(values):
                    lines.append(" | ".join(values))
        return "\n".join(lines)

    @staticmethod
    def _xlsx_text_ooxml(path: Path) -> str:
        with zipfile.ZipFile(path) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in root]
            lines: list[str] = []
            for name in sorted(item for item in archive.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", item)):
                lines.append(f"[工作表] {name}")
                root = ElementTree.fromstring(archive.read(name))
                for row in root.iter():
                    if not row.tag.endswith("}row"):
                        continue
                    values: list[str] = []
                    for cell in row:
                        if not cell.tag.endswith("}c"):
                            continue
                        raw = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                        if cell.attrib.get("t") == "s" and raw.isdigit() and int(raw) < len(shared):
                            raw = shared[int(raw)]
                        values.append(raw)
                    if any(values):
                        lines.append(" | ".join(values))
            return "\n".join(lines)

    @staticmethod
    def _pdf_text(path: Path) -> tuple[str, str, list[str]]:
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(str(path))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
            if text.strip():
                return text, "pypdf", []
        except ImportError:
            pass
        except Exception:
            pass
        binary = shutil.which("pdftotext")
        if binary:
            result = subprocess.run(
                [binary, "-layout", str(path), "-"],
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.stdout.strip():
                return result.stdout, "pdftotext", []
        return "", "none", ["PDF 无可提取文本，请安装 pypdf 或 OCR 依赖。"]

    @staticmethod
    def _image_text(path: Path) -> tuple[str, list[str]]:
        binary = shutil.which("tesseract")
        if not binary:
            return "", ["未找到 tesseract，图片已登记但未执行 OCR。"]
        result = subprocess.run(
            [binary, str(path), "stdout", "-l", "chi_sim+eng"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        warnings = [] if result.returncode == 0 else [result.stderr.strip() or "OCR 失败"]
        return result.stdout, warnings
