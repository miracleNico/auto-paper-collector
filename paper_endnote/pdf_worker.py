"""Bounded process isolation for PDF parsing, hashing, and OCR."""

from __future__ import annotations

import asyncio
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from .pdf_validation import PDFValidation, validate_pdf


_pool: ProcessPoolExecutor | None = None
_pool_lock = threading.Lock()


def _workers() -> ProcessPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")
            )
        return _pool


def _validate_in_process(path: str, options: dict[str, Any]) -> dict[str, object]:
    return validate_pdf(Path(path), **options).as_dict()


async def validate_pdf_async(
    path: Path,
    *,
    expected_doi: str | None,
    expected_title: str | None,
    ocr_enabled: bool = True,
    ocr_languages: str = "eng",
    ocr_max_pages: int = 2,
    ocr_text: str | None = None,
) -> PDFValidation:
    """Validate outside the event loop; cancellation waits for file access to end."""
    options = {
        "expected_doi": expected_doi,
        "expected_title": expected_title,
        "ocr_enabled": ocr_enabled,
        "ocr_languages": ocr_languages,
        "ocr_max_pages": ocr_max_pages,
        "ocr_text": ocr_text,
    }
    future = asyncio.get_running_loop().run_in_executor(
        _workers(), _validate_in_process, str(path), options
    )
    try:
        result = await asyncio.shield(future)
    except asyncio.CancelledError:
        # run_in_executor cannot stop an already running child. Draining it here
        # keeps batch deletion from removing a PDF while OCR still reads it.
        await asyncio.shield(future)
        raise
    return PDFValidation(**result)


async def close_pdf_workers() -> None:
    """Wait for running validations and close the process pool on shutdown."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        await asyncio.to_thread(pool.shutdown, wait=True, cancel_futures=False)
