from __future__ import annotations

import asyncio
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import AsyncMock, patch

from paper_endnote.config import Settings
from paper_endnote.db import Database
from paper_endnote.pipeline import PipelineManager
from paper_endnote.user_config import (
    InstitutionProfile,
    OcrOptions,
    load_acquisition_config,
    load_preset,
)


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
    async def test_retry_waits_for_in_flight_auto_commit(self) -> None:
        from paper_endnote import app as app_module

        class HoldingZotero:
            def __init__(self) -> None:
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def ensure_collection(self, name: str, *, create: bool) -> str:
                return "COL1"

            async def commit_paper(self, collection_key, metadata, pdf_path):
                self.started.set()
                await self.release.wait()
                return {"record_number": "ITEM1", "existing_full_text": False}

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.acquisition_mode = "oa_parallel"
            settings.auto_commit = True
            database = Database(settings.database_path)
            manager = PipelineManager(settings, database)
            zotero = HoldingZotero()
            manager.zotero = zotero
            batch_id = database.create_batch(
                name="auto commit",
                target_library="Test",
                library_mode="new",
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a"}],
                institution_config={
                    **settings.institution.as_dict(),
                    "_acquisition_mode": "oa_parallel",
                    "_acquisition_sources": ["open_access"],
                    "_auto_institution": False,
                },
            )
            paper_id = database.get_batch(batch_id)["papers"][0]["id"]
            database.update_paper(
                paper_id,
                doi="10.1000/a",
                title="Example Paper",
                metadata_status="verified",
                pdf_status="not_found",
                endnote_status="pending",
                status="endnote_pending",
            )
            manager.start(batch_id)
            await asyncio.wait_for(zotero.started.wait(), timeout=2)
            with (
                patch.object(app_module, "pipeline", manager),
                patch.object(app_module, "database", database),
                patch.object(manager, "start"),
            ):
                retry = asyncio.create_task(app_module.retry_paper(paper_id))
                try:
                    await asyncio.sleep(0.05)
                    self.assertFalse(retry.done(), "重试必须等待正在提交的 Zotero 写入")
                finally:
                    zotero.release.set()
                    await asyncio.wait_for(manager.wait_for_batch_acquisition(batch_id), timeout=2)
                    await asyncio.wait_for(retry, timeout=2)
            updated = database.get_paper(paper_id)
            self.assertEqual(updated["status"], "ready")
            self.assertEqual(updated["endnote_status"], "verified")

    async def test_clearing_institution_returns_to_oa_only(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            database = Database(settings.database_path)
            payload = app_module.SettingsUpdate(
                institution=app_module.InstitutionUpdate(
                    id="",
                    name="",
                    access_type="ezproxy",
                    login_url="",
                    login_url_markers=[],
                    openurl="",
                    ezproxy_login="",
                    ezproxy_hosts=[],
                    school_aliases=[],
                    entity_id="",
                    publisher_login_urls={},
                    preset="",
                )
            )
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                await app_module.update_settings(payload)

            self.assertEqual(settings.institution.id, "")
            self.assertEqual(settings.acquisition_sources, ("open_access",))
            self.assertFalse(settings.auto_institution)

    async def test_new_batch_keeps_credential_free_institution_snapshot(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.acquisition_mode = "publisher_parallel"
            settings.institution = InstitutionProfile(
                id="example-u",
                name="Example University",
                access_type="carsi_saml",
                login_url="https://idp.example.edu/login",
                school_aliases=("Example University",),
            )
            database = Database(settings.database_path)
            payload = app_module.BatchCreate(
                name="snapshot",
                items_text="10.1000/example",
                target_library="Test",
                library_mode="new",
                start_immediately=False,
            )
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.create_batch(payload)

            snapshot = database.get_batch(result["id"])["institution_config"]
            self.assertEqual(snapshot["id"], "example-u")
            self.assertEqual(snapshot["access_type"], "carsi_saml")
            self.assertEqual(
                snapshot["_acquisition_sources"], ["open_access", "institution"]
            )
            self.assertTrue(snapshot["_auto_institution"])
            self.assertEqual(snapshot["_acquisition_mode"], "publisher_parallel")
            self.assertNotIn("password", snapshot)
            self.assertNotIn("cookie", snapshot)

    async def test_mode_setting_applies_only_to_new_batches(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            database = Database(settings.database_path)
            payload = app_module.BatchCreate(
                name="snapshot",
                items_text="10.1000/example",
                target_library="Test",
                library_mode="new",
                start_immediately=False,
            )
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                first = await app_module.create_batch(payload)
                await app_module.update_settings(
                    app_module.SettingsUpdate(acquisition_mode="full_parallel")
                )
                second = await app_module.create_batch(payload)

            self.assertEqual(settings.acquisition_mode, "full_parallel")
            self.assertEqual(
                load_acquisition_config(settings.config_path, create=False).acquisition_mode,
                "full_parallel",
            )
            self.assertEqual(
                database.get_batch(first["id"])["institution_config"]["_acquisition_mode"],
                "legacy",
            )
            self.assertEqual(
                database.get_batch(second["id"])["institution_config"]["_acquisition_mode"],
                "full_parallel",
            )

    async def test_old_batch_api_defaults_to_legacy_and_exposes_progress(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            database = Database(_settings(Path(directory)).database_path)
            batch_id = database.create_batch(
                name="old batch", target_library="Test", library_mode="new", items=[]
            )
            progress = {
                stage: {"running": 0, "waiting": 0}
                for stage in ("metadata", "oa", "institution", "validation")
            }
            with (
                patch.object(app_module, "database", database),
                patch.object(app_module.pipeline, "acquisition_progress", return_value=progress),
            ):
                detail = await app_module.get_batch(batch_id)
                listing = await app_module.list_batches()

            self.assertEqual(detail["acquisition_mode"], "legacy")
            self.assertEqual(detail["acquisition_progress"], progress)
            self.assertEqual(listing[0]["acquisition_mode"], "legacy")
            self.assertEqual(listing[0]["acquisition_progress"], progress)

    async def test_switching_from_carsi_to_manual_clears_hidden_carsi_links(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.institution = InstitutionProfile(
                id="example-u",
                name="Example University",
                access_type="carsi_saml",
                login_url="https://idp.example.edu/login",
                school_aliases=("Example University",),
                entity_id="https://idp.example.edu/entity",
                publisher_login_urls={"ieee": "https://idp.example.edu/ieee"},
            )
            database = Database(settings.database_path)
            payload = app_module.SettingsUpdate(
                institution=app_module.InstitutionUpdate(
                    access_type="manual_browser",
                    id="example-u",
                    name="Example University",
                    login_url="https://library.example.edu/manual",
                )
            )
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                await app_module.update_settings(payload)

            self.assertEqual(settings.institution.access_type, "manual_browser")
            self.assertEqual(settings.institution.school_aliases, ())
            self.assertEqual(settings.institution.entity_id, "")
            self.assertEqual(settings.institution.publisher_login_urls, {})

    async def test_cannot_enable_institution_without_school_id(self) -> None:
        from fastapi import HTTPException
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            settings.institution = InstitutionProfile()
            settings.acquisition_sources = ("open_access",)
            settings.auto_institution = False
            database = Database(settings.database_path)
            payload = app_module.SettingsUpdate(
                acquisition_sources=["open_access", "institution"],
                auto_institution=True,
            )
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
                self.assertRaises(HTTPException),
            ):
                await app_module.update_settings(payload)

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

    async def test_carsi_profile_saves_without_ezproxy_fields(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            settings.acquisition_sources = ("open_access",)
            settings.auto_institution = False
            database = Database(settings.database_path)
            payload = app_module.SettingsUpdate(
                institution=app_module.InstitutionUpdate(
                    id="example-university",
                    name="Example University",
                    access_type="carsi_saml",
                    login_url="https://library.example.edu/carsi",
                    login_url_markers=["idp.example.edu"],
                    school_aliases=["Example U", "示例大学"],
                    entity_id="https://idp.example.edu/idp/shibboleth",
                    publisher_login_urls={
                        "IEEE": "https://library.example.edu/ieee",
                        "sciencedirect": "https://library.example.edu/elsevier",
                    },
                    preset="",
                )
            )

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                result = await app_module.update_settings(payload)

            self.assertEqual(result, {"status": "saved"})
            self.assertEqual(settings.institution.access_type, "carsi_saml")
            self.assertEqual(settings.institution.ezproxy_login, "")
            self.assertEqual(settings.institution.school_aliases, ("Example U", "示例大学"))
            self.assertEqual(
                settings.institution.publisher_login_urls["ieee"],
                "https://library.example.edu/ieee",
            )
            self.assertEqual(settings.acquisition_sources, ("open_access", "institution"))
            self.assertTrue(settings.auto_institution)
            persisted = load_acquisition_config(settings.config_path, create=False)
            self.assertEqual(persisted.institution.as_dict(), settings.institution.as_dict())

    async def test_explicit_empty_optional_fields_are_not_restored_from_preset(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = _settings(root)
            database = Database(settings.database_path)
            payload = app_module.SettingsUpdate(
                institution=app_module.InstitutionUpdate(
                    login_url="", openurl="", login_url_markers=[], ezproxy_hosts=[]
                )
            )

            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module, "database", database),
            ):
                await app_module.update_settings(payload)

            self.assertEqual(settings.institution.login_url, "")
            self.assertEqual(settings.institution.openurl, "")
            self.assertEqual(settings.institution.login_url_markers, ())
            self.assertEqual(settings.institution.ezproxy_hosts, ())
            persisted = load_acquisition_config(settings.config_path, create=False)
            self.assertEqual(persisted.institution.login_url, "")
            self.assertEqual(persisted.institution.openurl, "")
            self.assertEqual(persisted.institution.login_url_markers, ())
            self.assertEqual(persisted.institution.ezproxy_hosts, ())

    async def test_state_exposes_access_types_and_complete_institution_profile(self) -> None:
        from paper_endnote import app as app_module

        with tempfile.TemporaryDirectory() as directory:
            settings = _settings(Path(directory))
            with (
                patch.object(app_module, "settings", settings),
                patch.object(app_module.pipeline.zotero, "probe", AsyncMock(return_value={})),
                patch.object(app_module, "probe_endnote", return_value={}),
                patch.object(app_module, "ocr_status", return_value={}),
                patch.object(
                    app_module,
                    "credential_status",
                    return_value={"username": "", "password_saved": False},
                ),
            ):
                result = await app_module.state()

            from paper_endnote import __version__

            self.assertEqual(result["version"], __version__)
            self.assertEqual(
                result["institution_access_types"],
                ["ezproxy", "carsi_saml", "manual_browser"],
            )
            institution = result["settings"]["institution"]
            self.assertEqual(institution["access_type"], "ezproxy")
            self.assertEqual(institution["login_url"], "https://proxy.library.mcgill.ca/login")
            self.assertIn("publisher_login_urls", institution)
            self.assertIn("school_aliases", institution)


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
