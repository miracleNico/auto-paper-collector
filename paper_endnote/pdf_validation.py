from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from pypdf import PdfReader

from .inputs import DOI_RE, normalize_doi, normalize_title, title_similarity
from .ocr import ocr_pdf_text


SUPPLEMENT_MARKERS = (
    "supporting information",
    "supplementary information",
    "supplemental material",
    "supporting material",
    "online appendix",
)

TITLE_MATCH_THRESHOLD = 0.68
OcrFunc = Callable[[Path], str]


@dataclass(frozen=True)
class PDFValidation:
    valid_pdf: bool
    identity: str
    role: str
    doi: str | None
    title_similarity: float
    page_count: int
    text_available: bool
    sha256: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_text(reader: PdfReader, page_limit: int = 5) -> str:
    parts: list[str] = []
    for page in reader.pages[:page_limit]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def _title_score(expected_title: str | None, text: str) -> float:
    if not expected_title:
        return 0.0
    expected = normalize_title(expected_title)
    normalized_text = normalize_title(text[:10000])
    if expected and expected in normalized_text:
        return 1.0
    candidates = [line.strip() for line in text.splitlines()[:80] if line.strip()]
    return max((title_similarity(expected_title, line) for line in candidates), default=0.0)


def _supplement_role(path: Path, text: str) -> str:
    haystack = f"{path.name} {text[:3000]}".casefold()
    return "supplement" if any(marker in haystack for marker in SUPPLEMENT_MARKERS) else "main"


def validate_pdf(
    path: Path,
    *,
    expected_doi: str | None,
    expected_title: str | None,
    ocr_enabled: bool = True,
    ocr_languages: str = "eng",
    ocr_max_pages: int = 2,
    ocr_text: str | None = None,
    ocr_func: OcrFunc | None = None,
) -> PDFValidation:
    digest = sha256_file(path)
    try:
        with path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise ValueError("文件头不是 PDF")
        reader = PdfReader(str(path))
        page_count = len(reader.pages)
        text = _extract_text(reader)
    except Exception as exc:
        return PDFValidation(False, "rejected", "unknown", None, 0.0, 0, False, digest, f"PDF 无法解析：{exc}")

    compact_text = " ".join(text.split())
    role = _supplement_role(path, compact_text.casefold())
    expected_doi = normalize_doi(expected_doi)

    if not compact_text:
        scanned = ""
        if ocr_text is not None:
            scanned = ocr_text
        elif ocr_enabled:
            if ocr_func is not None:
                scanned = ocr_func(path)
            else:
                scanned = ocr_pdf_text(path, languages=ocr_languages, max_pages=ocr_max_pages)
        scanned = scanned.strip()
        if not scanned:
            reason = "PDF 可解析但无法提取文字，可能是扫描件"
            if ocr_enabled:
                reason += "；OCR 未返回可用文本"
            return PDFValidation(True, "needs_review", role, None, 0.0, page_count, False, digest, reason)
        title_score = _title_score(expected_title, scanned)
        role = _supplement_role(path, scanned.casefold())
        if role == "supplement":
            return PDFValidation(
                True, "needs_review", role, None, title_score, page_count, True, digest, "OCR 检测到补充材料标记"
            )
        if expected_title and title_score >= TITLE_MATCH_THRESHOLD and role == "main":
            return PDFValidation(
                True, "verified", role, None, title_score, page_count, True, digest, "OCR 题名与目标高度匹配"
            )
        return PDFValidation(
            True, "needs_review", role, None, title_score, page_count, True, digest, "OCR 已提取文字，但无法自动确认 PDF 身份"
        )

    found_dois = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(compact_text)}
    found_dois.discard(None)
    title_score = _title_score(expected_title, text)
    found_doi = sorted(found_dois)[0] if found_dois else None

    if role == "supplement":
        return PDFValidation(True, "needs_review", role, found_doi, title_score, page_count, True, digest, "检测到补充材料标记")
    if expected_doi and expected_doi in found_dois:
        return PDFValidation(True, "verified", role, expected_doi, title_score, page_count, True, digest, "PDF DOI 与目标一致")
    if expected_title and title_score >= TITLE_MATCH_THRESHOLD:
        return PDFValidation(True, "verified", role, found_doi, title_score, page_count, True, digest, "PDF 题名与目标高度匹配")
    if expected_doi and found_dois and expected_doi not in found_dois:
        return PDFValidation(True, "rejected", role, found_doi, title_score, page_count, True, digest, "PDF 中的 DOI 与目标不一致")
    return PDFValidation(True, "needs_review", role, found_doi, title_score, page_count, True, digest, "无法自动确认 PDF 身份")
