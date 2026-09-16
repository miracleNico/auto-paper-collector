from __future__ import annotations

from enum import StrEnum


class BatchStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class PaperStatus(StrEnum):
    QUEUED = "queued"
    MATCHING = "matching"
    NEEDS_MATCH = "needs_match"
    READY = "ready"
    LOOKING_FOR_PDF = "looking_for_pdf"
    INSTITUTION_PENDING = "institution_pending"
    NEEDS_PDF = "needs_pdf"
    NEEDS_PDF_REVIEW = "needs_pdf_review"
    PDF_READY = "pdf_ready"
    ENDNOTE_PENDING = "endnote_pending"
    COMPLETE = "complete"
    SKIPPED = "skipped"
    FAILED = "failed"


TERMINAL_PAPER_STATUSES = {
    PaperStatus.COMPLETE.value,
    PaperStatus.SKIPPED.value,
    PaperStatus.NEEDS_MATCH.value,
    PaperStatus.NEEDS_PDF.value,
    PaperStatus.NEEDS_PDF_REVIEW.value,
    PaperStatus.FAILED.value,
}
