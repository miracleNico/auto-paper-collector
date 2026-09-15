from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader

from .inputs import DOI_RE, normalize_doi, normalize_title, title_similarity


SUPPLEMENT_MARKERS = (
    "supporting information",
    "supplementary information",
    "supplemental material",
    "supporting material",
    "online appendix",
)


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


def validate_pdf(path: Path, *, expected_doi: str | None, expected_title: str | None) -> PDFValidation:
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
    lower_text = compact_text.casefold()
    found_dois = {normalize_doi(match.group(0)) for match in DOI_RE.finditer(compact_text)}
    found_dois.discard(None)
    expected_doi = normalize_doi(expected_doi)
    title_score = _title_score(expected_title, text)

    filename_and_text = f"{path.name} {lower_text[:3000]}".casefold()
    role = "supplement" if any(marker in filename_and_text for marker in SUPPLEMENT_MARKERS) else "main"
    found_doi = sorted(found_dois)[0] if found_dois else None

    if not compact_text:
        return PDFValidation(True, "needs_review", role, found_doi, 0.0, page_count, False, digest, "PDF 可解析但无法提取文字，可能是扫描件")
    if role == "supplement":
        return PDFValidation(True, "needs_review", role, found_doi, title_score, page_count, True, digest, "检测到补充材料标记")
    if expected_doi and expected_doi in found_dois:
        return PDFValidation(True, "verified", role, expected_doi, title_score, page_count, True, digest, "PDF DOI 与目标一致")
    # Journal PDFs commonly contain many unrelated DOIs in their references.
    # A strong title match is therefore more reliable than treating the mere
    # presence of a different DOI anywhere in the first pages as a conflict.
    if expected_title and title_score >= 0.68:
        return PDFValidation(True, "verified", role, found_doi, title_score, page_count, True, digest, "PDF 题名与目标高度匹配")
    if expected_doi and found_dois and expected_doi not in found_dois:
        return PDFValidation(True, "rejected", role, found_doi, title_score, page_count, True, digest, "PDF 中的 DOI 与目标不一致")
    return PDFValidation(True, "needs_review", role, found_doi, title_score, page_count, True, digest, "无法自动确认 PDF 身份")
