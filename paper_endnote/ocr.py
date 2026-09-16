from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


OcrFunc = Callable[[Path, str, int], str]


def ocr_status() -> dict[str, Any]:
    pypdfium2_ok = False
    pytesseract_ok = False
    tesseract_version = None
    try:
        import pypdfium2  # noqa: F401

        pypdfium2_ok = True
    except Exception:
        pass
    try:
        import pytesseract

        pytesseract_ok = True
        tesseract_version = str(pytesseract.get_tesseract_version())
    except Exception:
        tesseract_version = None
    ready = pypdfium2_ok and pytesseract_ok and bool(tesseract_version)
    return {
        "pypdfium2": pypdfium2_ok,
        "pytesseract": pytesseract_ok,
        "tesseract_version": tesseract_version,
        "ready": ready,
    }


def ocr_pdf_text(path: Path, *, languages: str = "eng", max_pages: int = 2) -> str:
    if not path.is_file():
        return ""
    try:
        import pypdfium2
        import pytesseract
    except Exception:
        return ""
    try:
        pytesseract.get_tesseract_version()
    except Exception:
        return ""
    pages = max(1, min(int(max_pages), 5))
    lang = languages.strip() or "eng"
    try:
        document = pypdfium2.PdfDocument(str(path))
    except Exception:
        return ""
    parts: list[str] = []
    try:
        for index, page in enumerate(document):
            if index >= pages:
                break
            try:
                bitmap = page.render(scale=2)
                image = bitmap.to_pil()
                parts.append(pytesseract.image_to_string(image, lang=lang) or "")
            except Exception:
                continue
            finally:
                page.close()
    finally:
        document.close()
    return "\n".join(parts).strip()
