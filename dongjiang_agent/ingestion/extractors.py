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
from typing import Any
from xml.etree import ElementTree


@dataclass(slots=True)
class DocumentFragment:
    fragment_id: str
    text: str
    location: dict[str, Any]


@dataclass(slots=True)
class ExtractedDocument:
    path: str
    media_type: str
    text: str
    extractor: str
    warnings: list[str]
    fragments: list[DocumentFragment]


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
            return ExtractedDocument(
                str(target),
                suffix.lstrip("."),
                text,
                "native-text",
                [],
                self._line_fragments(text),
            )
        if suffix == ".docx":
            text, fragments = self._docx_content(target)
            return ExtractedDocument(str(target), "docx", text, "ooxml", [], fragments)
        if suffix == ".xlsx":
            text, fragments = self._xlsx_content(target)
            return ExtractedDocument(str(target), "xlsx", text, "ooxml", [], fragments)
        if suffix == ".pdf":
            text, extractor, warnings, fragments = self._pdf_content(target)
            return ExtractedDocument(str(target), "pdf", text, extractor, warnings, fragments)
        if suffix in self.IMAGE_EXTENSIONS:
            text, warnings = self._image_text(target)
            fragments = [
                DocumentFragment("image-1", text, {"kind": "image", "image": 1})
            ] if text.strip() else []
            return ExtractedDocument(
                str(target), suffix.lstrip("."), text, "tesseract", warnings, fragments
            )
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
    def _docx_content(path: Path) -> tuple[str, list[DocumentFragment]]:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        paragraphs: list[str] = []
        fragments: list[DocumentFragment] = []
        for paragraph in root.findall(".//w:p", namespace):
            chunks = [node.text or "" for node in paragraph.findall(".//w:t", namespace)]
            text = "".join(chunks).strip()
            if text:
                paragraphs.append(text)
                number = len(paragraphs)
                fragments.append(
                    DocumentFragment(
                        f"paragraph-{number}",
                        text,
                        {"kind": "paragraph", "paragraph": number},
                    )
                )
        return "\n".join(paragraphs), fragments

    @staticmethod
    def _xlsx_content(path: Path) -> tuple[str, list[DocumentFragment]]:
        try:
            import openpyxl  # type: ignore
        except ImportError:
            return DocumentExtractor._xlsx_content_ooxml(path)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        lines: list[str] = []
        fragments: list[DocumentFragment] = []
        for sheet in workbook.worksheets:
            lines.append(f"[工作表] {sheet.title}")
            for row in sheet.iter_rows():
                values = [str(cell.value).strip() if cell.value is not None else "" for cell in row]
                if any(values):
                    lines.append(" | ".join(values))
                for cell, value in zip(row, values):
                    if not value:
                        continue
                    fragments.append(
                        DocumentFragment(
                            f"cell-{sheet.title}-{cell.coordinate}",
                            value,
                            {
                                "kind": "cell",
                                "sheet": sheet.title,
                                "cell": cell.coordinate,
                            },
                        )
                    )
        workbook.close()
        return "\n".join(lines), fragments

    @staticmethod
    def _xlsx_content_ooxml(path: Path) -> tuple[str, list[DocumentFragment]]:
        with zipfile.ZipFile(path) as archive:
            shared: list[str] = []
            if "xl/sharedStrings.xml" in archive.namelist():
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = ["".join(node.itertext()) for node in root]
            lines: list[str] = []
            fragments: list[DocumentFragment] = []
            for name in sorted(item for item in archive.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml", item)):
                sheet_name = Path(name).stem
                lines.append(f"[工作表] {sheet_name}")
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
                        if raw:
                            coordinate = str(cell.attrib.get("r") or "")
                            fragments.append(
                                DocumentFragment(
                                    f"cell-{sheet_name}-{coordinate}",
                                    raw,
                                    {
                                        "kind": "cell",
                                        "sheet": sheet_name,
                                        "cell": coordinate,
                                    },
                                )
                            )
                    if any(values):
                        lines.append(" | ".join(values))
            return "\n".join(lines), fragments

    @staticmethod
    def _pdf_content(
        path: Path,
    ) -> tuple[str, str, list[str], list[DocumentFragment]]:
        try:
            from pypdf import PdfReader  # type: ignore
            reader = PdfReader(str(path))
            pages = [page.extract_text() or "" for page in reader.pages]
            text = "\n".join(pages)
            if text.strip():
                fragments = [
                    DocumentFragment(
                        f"page-{number}",
                        content,
                        {"kind": "page", "page": number},
                    )
                    for number, content in enumerate(pages, start=1)
                    if content.strip()
                ]
                return text, "pypdf", [], fragments
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
                pages = result.stdout.split("\f")
                fragments = [
                    DocumentFragment(
                        f"page-{number}",
                        content,
                        {"kind": "page", "page": number},
                    )
                    for number, content in enumerate(pages, start=1)
                    if content.strip()
                ]
                return result.stdout, "pdftotext", [], fragments
        return "", "none", ["PDF 无可提取文本，请安装 pypdf 或 OCR 依赖。"], []

    @staticmethod
    def _line_fragments(text: str) -> list[DocumentFragment]:
        fragments: list[DocumentFragment] = []
        for number, line in enumerate(text.splitlines(), start=1):
            content = line.strip()
            if content:
                fragments.append(
                    DocumentFragment(
                        f"line-{number}",
                        content,
                        {"kind": "line", "line": number},
                    )
                )
        if not fragments and text.strip():
            fragments.append(
                DocumentFragment("line-1", text.strip(), {"kind": "line", "line": 1})
            )
        return fragments

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
