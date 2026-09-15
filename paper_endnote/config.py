from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    crossref_mailto: str
    unpaywall_email: str
    max_pdf_bytes: int = 100 * 1024 * 1024
    request_timeout_seconds: float = 30.0
    crossref_min_interval_seconds: float = 0.25

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
        settings = cls(
            app_root=app_root,
            runtime_dir=base,
            database_path=base / "paper-endnote.sqlite3",
            download_dir=base / "downloads",
            generated_dir=base / "generated",
            backup_dir=base / "backups",
            browser_profile_dir=base / "playwright-profile",
            endnote_exe=endnote,
            crossref_mailto=os.environ.get("PAPER_ENDNOTE_CROSSREF_EMAIL", "").strip(),
            unpaywall_email=os.environ.get("PAPER_ENDNOTE_UNPAYWALL_EMAIL", "").strip(),
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
