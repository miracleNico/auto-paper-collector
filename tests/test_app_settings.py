from __future__ import annotations

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.user_config import OcrOptions, load_preset


class _InputParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inputs: dict[str, dict[str, str | None]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "input":
            return
        attributes = dict(attrs)
        identifier = attributes.get("id")
        if identifier:
            self.inputs[identifier] = attributes


def _settings(root: Path) -> Settings:
    runtime = root / "runtime"
    paths = {
        "downloads": runtime / "downloads",
        "generated": runtime / "generated",
        "backups": runtime / "backups",
        "profile": runtime / "profile",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return Settings(
        app_root=root,
        runtime_dir=runtime,
        database_path=runtime / "db.sqlite3",
        download_dir=paths["downloads"],
        generated_dir=paths["generated"],
        backup_dir=paths["backups"],
        browser_profile_dir=paths["profile"],
        endnote_exe=root / "EndNote.exe",
        endnote_library=root / "remembered.enl",
        crossref_mailto="crossref@example.org",
        unpaywall_email="unpaywall@example.org",
        config_path=runtime / "config.toml",
        acquisition_sources=("open_access", "institution"),
        institution=load_preset("mcgill"),
        ocr=OcrOptions(enabled=False, languages="eng+fra", max_pages=4),
        auto_institution=True,
        auto_commit=False,
        login_wait_seconds=900,
    )


class SettingsUpdateTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_acquisition_update_preserves_saved_profile_and_database_settings(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            database = Database(settings.database_path)
            database.set_setting("crossref_email", settings.crossref_mailto)
            database.set_setting("unpaywall_email", settings.unpaywall_email)
            database.set_setting("endnote_library", str(settings.endnote_library))
            payload = app_module.SettingsUpdate(acquisition_sources=["open_access"])

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.update_settings(payload)

            self.assertEqual(result, {"status": "saved"})
            self.assertEqual(settings.acquisition_sources, ("open_access",))
            self.assertTrue(settings.auto_institution)
            self.assertFalse(settings.auto_commit)
            self.assertEqual(settings.login_wait_seconds, 900)
            self.assertFalse(settings.ocr.enabled)
            self.assertEqual(settings.institution.id, "mcgill")
            self.assertEqual(settings.institution.name, "McGill University")
            self.assertEqual(database.get_setting("crossref_email"), "crossref@example.org")
            self.assertEqual(database.get_setting("unpaywall_email"), "unpaywall@example.org")
            self.assertEqual(database.get_setting("endnote_library"), str(root / "remembered.enl"))

    async def test_partial_database_update_does_not_apply_model_defaults(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            database = Database(settings.database_path)
            database.set_setting("crossref_email", settings.crossref_mailto)
            database.set_setting("unpaywall_email", settings.unpaywall_email)
            database.set_setting("endnote_library", str(settings.endnote_library))
            payload = app_module.SettingsUpdate(crossref_email="new@example.org")

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.update_settings(payload)

            self.assertEqual(result, {"status": "saved"})
            self.assertEqual(settings.crossref_mailto, "new@example.org")
            self.assertEqual(settings.unpaywall_email, "unpaywall@example.org")
            self.assertEqual(settings.endnote_library, root / "remembered.enl")
            self.assertEqual(settings.acquisition_sources, ("open_access", "institution"))
            self.assertTrue(settings.auto_institution)
            self.assertEqual(settings.institution.id, "mcgill")
            self.assertEqual(database.get_setting("crossref_email"), "new@example.org")
            self.assertEqual(database.get_setting("unpaywall_email"), "unpaywall@example.org")
            self.assertEqual(database.get_setting("endnote_library"), str(root / "remembered.enl"))

    async def test_specifying_institution_defaults_omitted_acquisition_switches_on(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            settings.acquisition_sources = ("open_access",)
            settings.auto_institution = False
            database = Database(settings.database_path)
            payload = app_module.SettingsUpdate(institution_preset="mcgill")

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.update_settings(payload)

            self.assertEqual(result, {"status": "saved"})
            self.assertEqual(settings.acquisition_sources, ("open_access", "institution"))
            self.assertTrue(settings.auto_institution)
            self.assertEqual(settings.institution.id, "mcgill")

    async def test_full_ui_payload_still_updates_every_explicit_setting(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            database = Database(settings.database_path)
            profile = load_preset("mcgill")
            payload = app_module.SettingsUpdate(
                crossref_email="updated-crossref@example.org",
                unpaywall_email="updated-unpaywall@example.org",
                endnote_library="",
                acquisition_sources=["open_access"],
                ocr_enabled=True,
                ocr_languages="eng",
                ocr_max_pages=2,
                institution=app_module.InstitutionUpdate(**profile.as_dict()),
                auto_institution=False,
                auto_commit=True,
                login_wait_seconds=300,
            )

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.update_settings(payload)

            self.assertEqual(result, {"status": "saved"})
            self.assertEqual(settings.crossref_mailto, "updated-crossref@example.org")
            self.assertEqual(settings.unpaywall_email, "updated-unpaywall@example.org")
            self.assertIsNone(settings.endnote_library)
            self.assertEqual(settings.acquisition_sources, ("open_access",))
            self.assertFalse(settings.auto_institution)
            self.assertTrue(settings.auto_commit)
            self.assertEqual(settings.login_wait_seconds, 300)
            self.assertTrue(settings.ocr.enabled)
            self.assertEqual(settings.institution.id, "mcgill")
            self.assertEqual(database.get_setting("crossref_email"), "updated-crossref@example.org")
            self.assertEqual(database.get_setting("unpaywall_email"), "updated-unpaywall@example.org")
            self.assertEqual(database.get_setting("endnote_library"), "")


class StaticSettingsDefaultsTests(unittest.TestCase):
    def test_institution_options_are_unchecked_before_server_state_loads(self) -> None:
        parser = _InputParser()
        parser.feed(
            (Path(__file__).parents[1] / "paper_endnote" / "static" / "index.html").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn("checked", parser.inputs["source-open-access"])
        self.assertNotIn("checked", parser.inputs["source-institution"])
        self.assertNotIn("checked", parser.inputs["auto-institution"])


if __name__ == "__main__":
    unittest.main()
