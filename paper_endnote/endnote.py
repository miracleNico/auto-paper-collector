from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .downloader import safe_filename
from .inputs import normalize_doi, title_similarity
from .pdf_validation import validate_pdf


class EndNoteError(RuntimeError):
    pass


REF_TYPE_JOURNAL = "17"
IMPORT_INSTRUCTIONS = """Zotero → EndNote 导入说明

本目录由本机工具从 Zotero collection 导出，不要用 EndNote 桌面控件自动写入。

方法 A（推荐，本机导入）
1. 打开 EndNote，打开或新建目标库。
2. File → Import → File。
3. Import File 选择本目录的 records.xml。
4. Import Option 选择 EndNote Generated XML。
5. 点击 Import。XML 使用本机绝对路径指向 PDF 子目录中的文件。

方法 B（与 Clarivate 文档一致）
1. 按方法 A 导入 records-internal.xml。
2. 把本目录 PDF 文件夹中的全部子文件夹复制到：
   <库名>.Data\\PDF\\
3. 重新打开该库，让 EndNote 索引附件。

不要重复导入同一批次，否则会生成重复题录。更新时请导入到新库，或只导入新增条目。
"""


def _plain(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[\r\n]+", " ", str(value)).strip()


def write_enw(path: Path, metadata: dict[str, Any]) -> Path:
    reference_type = "Journal Article"
    if metadata.get("type") in {"book", "monograph"}:
        reference_type = "Book"
    lines = [f"%0 {reference_type}"]
    if metadata.get("title"):
        lines.append(f"%T {_plain(metadata['title'])}")
    for author in metadata.get("authors") or []:
        lines.append(f"%A {_plain(author)}")
    if metadata.get("year"):
        lines.append(f"%D {_plain(metadata['year'])}")
    if metadata.get("journal"):
        lines.append(f"%J {_plain(metadata['journal'])}")
    if metadata.get("volume"):
        lines.append(f"%V {_plain(metadata['volume'])}")
    if metadata.get("issue"):
        lines.append(f"%N {_plain(metadata['issue'])}")
    if metadata.get("pages"):
        lines.append(f"%P {_plain(metadata['pages'])}")
    if metadata.get("doi"):
        lines.append(f"%R {_plain(metadata['doi'])}")
    if metadata.get("url"):
        lines.append(f"%U {_plain(metadata['url'])}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig", newline="\n")
    return path


def write_ris(path: Path, records: Iterable["EndNoteExportRecord"]) -> Path:
    blocks: list[str] = []
    for record in records:
        metadata = record.metadata
        lines = ["TY  - JOUR"]
        for author in metadata.get("authors") or []:
            lines.append(f"AU  - {_plain(author)}")
        if metadata.get("title"):
            lines.append(f"TI  - {_plain(metadata['title'])}")
        if metadata.get("journal"):
            lines.append(f"JO  - {_plain(metadata['journal'])}")
        if metadata.get("year"):
            lines.append(f"PY  - {_plain(metadata['year'])}")
        if metadata.get("volume"):
            lines.append(f"VL  - {_plain(metadata['volume'])}")
        if metadata.get("issue"):
            lines.append(f"IS  - {_plain(metadata['issue'])}")
        if metadata.get("pages"):
            lines.append(f"SP  - {_plain(metadata['pages'])}")
        doi = normalize_doi(metadata.get("doi"))
        if doi:
            lines.append(f"DO  - {doi}")
        if metadata.get("url"):
            lines.append(f"UR  - {_plain(metadata['url'])}")
        elif doi:
            lines.append(f"UR  - https://doi.org/{doi}")
        if record.copied_pdf and record.copied_pdf.is_file():
            lines.append(f"L1  - {record.copied_pdf.resolve().as_uri()}")
        lines.append("ER  - ")
        blocks.append("\n".join(lines))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(blocks) + ("\n" if blocks else ""), encoding="utf-8-sig", newline="\n")
    return path


@dataclass(frozen=True)
class EndNoteRecord:
    record_number: str | None
    doi: str | None
    title: str
    year: int | None
    authors: list[str]
    attachments: list[str]


@dataclass
class EndNoteExportRecord:
    rec_number: int
    metadata: dict[str, Any]
    source_pdf: Path | None
    folder_id: str
    filename: str
    copied_pdf: Path | None = None
    zotero_key: str | None = None


def _text(element: ET.Element | None) -> str:
    return "" if element is None else "".join(element.itertext()).strip()


def parse_endnote_xml(path: Path) -> list[EndNoteRecord]:
    if not path.exists():
        return []
    root = ET.parse(path).getroot()
    records: list[EndNoteRecord] = []
    for record in root.findall(".//record"):
        record_number = _text(record.find("./rec-number")) or None
        title = _text(record.find("./titles/title")) or _text(record.find("./title"))
        doi_text = _text(record.find("./electronic-resource-num"))
        doi = normalize_doi(doi_text)
        year_text = _text(record.find("./dates/year")) or _text(record.find("./year"))
        year_match = re.search(r"\b(?:18|19|20|21)\d{2}\b", year_text)
        authors = [_text(item) for item in record.findall("./contributors/authors/author") if _text(item)]
        attachments: list[str] = []
        for item in record.findall("./urls/pdf-urls/url") + record.findall("./urls/related-urls/url"):
            value = _text(item)
            if value:
                attachments.append(value)
        records.append(
            EndNoteRecord(
                record_number=record_number,
                doi=doi,
                title=title,
                year=int(year_match.group(0)) if year_match else None,
                authors=authors,
                attachments=attachments,
            )
        )
    return records


def match_records(records: list[EndNoteRecord], metadata: dict[str, Any]) -> list[EndNoteRecord]:
    doi = normalize_doi(metadata.get("doi"))
    if doi:
        exact = [record for record in records if record.doi == doi]
        if exact:
            return exact
    title = metadata.get("title") or ""
    year = metadata.get("year")
    candidates = []
    for record in records:
        score = title_similarity(title, record.title)
        if score >= 0.88 and (not year or not record.year or int(year) == record.year):
            candidates.append(record)
    return candidates


def resolve_internal_attachment(library_path: Path, attachment: str) -> Path | None:
    attachment = unquote(attachment)
    if not attachment.casefold().startswith("internal-pdf://"):
        value = attachment.replace("file:///", "")
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
        candidate = Path(value)
        return candidate if candidate.is_absolute() else None
    relative = attachment[len("internal-pdf://") :].lstrip("/\\").replace("/", os.sep)
    return library_path.with_suffix(".Data") / "PDF" / relative


def resolve_export_attachment(export_dir: Path, attachment: str) -> Path | None:
    attachment = unquote(attachment)
    if attachment.casefold().startswith("internal-pdf://"):
        relative = attachment[len("internal-pdf://") :].lstrip("/\\").replace("/", os.sep)
        candidate = export_dir / "PDF" / relative
        return candidate if candidate.is_file() else None
    if attachment.casefold().startswith("file:"):
        value = attachment.replace("file:///", "")
        if re.match(r"^/[A-Za-z]:/", value):
            value = value[1:]
        candidate = Path(value)
        return candidate if candidate.is_file() else None
    return None


def verified_main_attachments(
    library_path: Path, record: EndNoteRecord, metadata: dict[str, Any]
) -> list[Path]:
    """Return existing attachments that can safely satisfy the full-text requirement."""
    result: list[Path] = []
    for attachment in record.attachments:
        resolved = resolve_internal_attachment(library_path, attachment)
        if not resolved or not resolved.is_file() or resolved.suffix.casefold() != ".pdf":
            continue
        validation = validate_pdf(
            resolved,
            expected_doi=metadata.get("doi"),
            expected_title=metadata.get("title"),
        )
        if validation.valid_pdf and validation.identity == "verified" and validation.role == "main":
            result.append(resolved)
    return result


def _xml_text(parent: ET.Element, tag: str, value: Any) -> ET.Element | None:
    text = _plain(value)
    if not text:
        return None
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def _attachment_url(record: EndNoteExportRecord, *, internal: bool) -> str | None:
    if not record.copied_pdf or not record.copied_pdf.is_file():
        return None
    if internal:
        return f"internal-pdf://{record.folder_id}/{record.filename}"
    return record.copied_pdf.resolve().as_uri()


def write_endnote_xml(
    path: Path,
    records: Iterable[EndNoteExportRecord],
    *,
    internal_pdf: bool,
) -> Path:
    root = ET.Element("xml")
    records_el = ET.SubElement(root, "records")
    for record in records:
        metadata = record.metadata
        item = ET.SubElement(records_el, "record")
        _xml_text(item, "rec-number", record.rec_number)
        ref_type = ET.SubElement(item, "ref-type")
        ref_type.set("name", "Journal Article")
        ref_type.text = REF_TYPE_JOURNAL
        contributors = ET.SubElement(item, "contributors")
        authors_el = ET.SubElement(contributors, "authors")
        authors = list(metadata.get("authors") or [])
        if authors:
            for author in authors:
                _xml_text(authors_el, "author", author)
        else:
            ET.SubElement(authors_el, "author")
        titles = ET.SubElement(item, "titles")
        _xml_text(titles, "title", metadata.get("title"))
        if metadata.get("journal"):
            _xml_text(titles, "secondary-title", metadata.get("journal"))
            periodical = ET.SubElement(item, "periodical")
            _xml_text(periodical, "full-title", metadata.get("journal"))
        dates = ET.SubElement(item, "dates")
        _xml_text(dates, "year", metadata.get("year"))
        _xml_text(item, "volume", metadata.get("volume"))
        _xml_text(item, "number", metadata.get("issue"))
        _xml_text(item, "pages", metadata.get("pages"))
        doi = normalize_doi(metadata.get("doi"))
        _xml_text(item, "electronic-resource-num", doi)
        urls = ET.SubElement(item, "urls")
        pdf_url = _attachment_url(record, internal=internal_pdf)
        if pdf_url:
            pdf_urls = ET.SubElement(urls, "pdf-urls")
            _xml_text(pdf_urls, "url", pdf_url)
        related = metadata.get("url") or (f"https://doi.org/{doi}" if doi else "")
        if related:
            related_urls = ET.SubElement(urls, "related-urls")
            _xml_text(related_urls, "url", related)
        source_app = ET.SubElement(item, "source-app")
        source_app.set("name", "Zotero")
        source_app.text = "Zotero"
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode"),
        encoding="utf-8",
        newline="\n",
    )
    return path


def copy_pdf_for_export(record: EndNoteExportRecord, pdf_root: Path) -> Path | None:
    if not record.source_pdf or not Path(record.source_pdf).is_file():
        return None
    destination = pdf_root / record.folder_id / record.filename
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(record.source_pdf, destination)
    record.copied_pdf = destination
    return destination


def copy_pdfs_into_endnote_library(pdf_root: Path, library: Path) -> Path:
    library = Path(library).expanduser().resolve()
    if library.suffix.casefold() != ".enl":
        raise EndNoteError("目标库必须使用 .enl 扩展名")
    if not library.is_file():
        raise EndNoteError(f"EndNote 库不存在：{library}")
    destination = library.with_suffix(".Data") / "PDF"
    destination.mkdir(parents=True, exist_ok=True)
    if not pdf_root.is_dir():
        return destination
    for child in pdf_root.iterdir():
        if not child.is_dir():
            continue
        target = destination / child.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(child, target)
    return destination


def zip_export_package(export_dir: Path, zip_path: Path | None = None) -> Path:
    zip_path = zip_path or export_dir.with_suffix(".zip")
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for child in export_dir.rglob("*"):
            if child.is_file():
                archive.write(child, child.relative_to(export_dir))
    return zip_path


def build_endnote_export_package(
    records: list[EndNoteExportRecord],
    destination: Path,
    *,
    collection_name: str,
    library: Path | None = None,
) -> dict[str, Any]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    pdf_root = destination / "PDF"
    pdf_root.mkdir(parents=True, exist_ok=True)
    copied = 0
    for record in records:
        if copy_pdf_for_export(record, pdf_root):
            copied += 1
    xml_path = write_endnote_xml(destination / "records.xml", records, internal_pdf=False)
    internal_xml = write_endnote_xml(destination / "records-internal.xml", records, internal_pdf=True)
    ris_path = write_ris(destination / "records.ris", records)
    (destination / "IMPORT.txt").write_text(IMPORT_INSTRUCTIONS, encoding="utf-8", newline="\n")
    library_pdf_dir = None
    library_copy_error = None
    if library:
        try:
            library_pdf_dir = str(copy_pdfs_into_endnote_library(pdf_root, library))
        except EndNoteError as exc:
            library_copy_error = str(exc)
    zip_path = zip_export_package(destination)
    manifest = {
        "collection": collection_name,
        "record_count": len(records),
        "pdf_count": copied,
        "xml": str(xml_path),
        "xml_internal": str(internal_xml),
        "ris": str(ris_path),
        "zip": str(zip_path),
        "library_pdf_dir": library_pdf_dir,
        "library_copy_error": library_copy_error,
        "records": [
            {
                "rec_number": record.rec_number,
                "doi": normalize_doi(record.metadata.get("doi")),
                "title": record.metadata.get("title"),
                "zotero_key": record.zotero_key,
                "pdf": str(record.copied_pdf) if record.copied_pdf else None,
            }
            for record in records
        ],
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def export_filename(metadata: dict[str, Any], fallback: str) -> str:
    doi = normalize_doi(metadata.get("doi"))
    stem = doi.replace("/", "_") if doi else _plain(metadata.get("title")) or fallback
    return safe_filename(f"{stem}.pdf")


def probe_endnote(endnote_exe: Path, library: Path | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "platform": os.name,
        "endnote_exe": str(endnote_exe),
        "endnote_exists": Path(endnote_exe).exists(),
        "library": str(library) if library else None,
        "library_exists": bool(library and Path(library).is_file()),
        "ready": True,
        "details": [
            "EndNote 不再通过桌面控件写入。提交到 Zotero 后导出导入包，再由 EndNote 导入 XML。"
        ],
    }
    if library and not Path(library).is_file():
        result["details"].append(f"已配置的 EndNote 库不存在：{library}")
    return result
