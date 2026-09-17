from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .user_config import (
    AcquisitionConfig,
    DEFAULT_SOURCES,
    InstitutionProfile,
    OcrOptions,
    default_config_path,
    default_institution_profile,
    load_acquisition_config,
    save_acquisition_config,
)


@dataclass
class Settings:
    app_root: Path
    runtime_dir: Path
    database_path: Path
    download_dir: Path
    generated_dir: Path
    backup_dir: Path
    browser_profile_dir: Path
    endnote_exe: Path
    endnote_library: Path | None
    crossref_mailto: str
    unpaywall_email: str
    config_path: Path
    acquisition_sources: tuple[str, ...] = DEFAULT_SOURCES
    ocr: OcrOptions = field(default_factory=OcrOptions)
    institution: InstitutionProfile = field(default_factory=default_institution_profile)
    max_pdf_bytes: int = 100 * 1024 * 1024
    request_timeout_seconds: float = 30.0
    crossref_min_interval_seconds: float = 0.25
    auto_institution: bool = False
    auto_commit: bool = True
    login_wait_seconds: int = 600

    @classmethod
    def load(cls) -> "Settings":
        app_root = Path(__file__).resolve().parent.parent
        base = Path(os.environ.get("PAPER_ENDNOTE_DATA", app_root / "runtime")).resolve()
        endnote = Path(
            os.environ.get(
                "PAPER_ENDNOTE_ENDNOTE_EXE",
                r"C:\Program Files (x86)\EndNote 21\EndNote.exe",
            )
        )
        library_value = os.environ.get("PAPER_ENDNOTE_LIBRARY", "").strip()
        config_path = default_config_path(base)
        acquisition = load_acquisition_config(config_path, create=True)
        settings = cls(
            app_root=app_root,
            runtime_dir=base,
            database_path=base / "paper-endnote.sqlite3",
            download_dir=base / "downloads",
            generated_dir=base / "generated",
            backup_dir=base / "backups",
            browser_profile_dir=base / "playwright-profile",
            endnote_exe=endnote,
            endnote_library=Path(library_value).expanduser() if library_value else None,
            crossref_mailto=os.environ.get("PAPER_ENDNOTE_CROSSREF_EMAIL", "").strip(),
            unpaywall_email=os.environ.get("PAPER_ENDNOTE_UNPAYWALL_EMAIL", "").strip(),
            config_path=config_path,
            acquisition_sources=acquisition.sources,
            ocr=acquisition.ocr,
            institution=acquisition.institution,
            auto_institution=acquisition.auto_institution,
            auto_commit=acquisition.auto_commit,
            login_wait_seconds=acquisition.login_wait_seconds,
        )
        for directory in (
            settings.runtime_dir,
            settings.download_dir,
            settings.generated_dir,
            settings.backup_dir,
            settings.browser_profile_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return settings

    def acquisition_config(self) -> AcquisitionConfig:
        return AcquisitionConfig(
            sources=self.acquisition_sources,
            ocr=self.ocr,
            institution=self.institution,
            auto_institution=self.auto_institution,
            auto_commit=self.auto_commit,
            login_wait_seconds=self.login_wait_seconds,
        )

    def save_acquisition_config(self) -> None:
        save_acquisition_config(self.config_path, self.acquisition_config())

    def pdf_ocr_kwargs(self) -> dict[str, bool | str | int]:
        return {
            "ocr_enabled": self.ocr.enabled,
            "ocr_languages": self.ocr.languages,
            "ocr_max_pages": self.ocr.max_pages,
        }
