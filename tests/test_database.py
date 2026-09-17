from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from paper_endnote.db import Database


class DatabaseTests(unittest.TestCase):
    def test_batch_round_trip_and_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "db.sqlite3")
            batch_id = db.create_batch(
                name="test",
                target_library=str(Path(directory) / "target.enl"),
                library_mode="new",
                reference_manager="zotero",
                institution_config={"id": "school-a", "access_type": "carsi_saml"},
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": None}],
            )
            batch = db.get_batch(batch_id)
            self.assertIsNotNone(batch)
            self.assertEqual(len(batch["papers"]), 1)
            self.assertEqual(batch["reference_manager"], "zotero")
            self.assertEqual(
                batch["institution_config"],
                {"id": "school-a", "access_type": "carsi_saml"},
            )
            paper_id = batch["papers"][0]["id"]
            db.update_paper(
                paper_id,
                authors_json=["Doe, Jane"],
                metadata_json={"doi": "10.1000/a"},
                institution_publisher="ieee",
                institution_state="waiting",
            )
            paper = db.get_paper(paper_id)
            self.assertEqual(paper["authors"], ["Doe, Jane"])
            self.assertEqual(paper["metadata"]["doi"], "10.1000/a")
            self.assertEqual(paper["institution_publisher"], "ieee")
            self.assertEqual(paper["institution_state"], "waiting")

    def test_legacy_batch_snapshots_institution_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "db.sqlite3")
            batch_id = db.create_batch(
                name="legacy",
                target_library="Test",
                library_mode="new",
                items=[{"input_text": "A paper", "title": "A paper"}],
            )
            self.assertIsNone(db.get_batch(batch_id)["institution_config"])
            first = db.ensure_batch_institution_config(
                batch_id, {"id": "first", "access_type": "manual_browser"}
            )
            second = db.ensure_batch_institution_config(
                batch_id, {"id": "second", "access_type": "ezproxy"}
            )
            self.assertEqual(first["id"], "first")
            self.assertEqual(second["id"], "first")
            self.assertEqual(db.get_batch(batch_id)["institution_config"]["id"], "first")

    def test_persisted_errors_events_and_operation_details_are_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "db.sqlite3")
            batch_id = db.create_batch(
                name="redaction",
                target_library="Test",
                library_mode="new",
                items=[{"input_text": "A paper", "title": "A paper"}],
            )
            paper_id = db.get_batch(batch_id)["papers"][0]["id"]
            secret_url = (
                "https://idp.example/sso?entityID=stable&"
                "SAMLResponse=assertion-secret&RelayState=relay-secret"
            )

            db.update_paper(paper_id, error=f"paper: {secret_url}")
            db.update_batch(batch_id, error=f"batch: {secret_url}")
            db.event(batch_id, f"event: {secret_url}", paper_id=paper_id)
            operation_id = db.start_operation(
                paper_id, "institution", {"error": secret_url}
            )
            db.finish_operation(operation_id, "failed", {"error": secret_url})

            paper = db.get_paper(paper_id)
            batch = db.get_batch(batch_id)
            event = db.events_after(batch_id)[-1]
            with db.connect() as connection:
                raw_details = connection.execute(
                    "SELECT details_json FROM operations WHERE id=?", (operation_id,)
                ).fetchone()[0]
            stored = "\n".join(
                [paper["error"], batch["error"], event["message"], raw_details]
            )

            self.assertNotIn("assertion-secret", stored)
            self.assertNotIn("relay-secret", stored)
            self.assertIn("entityID=stable", stored)
            self.assertIn("[REDACTED]", stored)
            self.assertEqual(json.loads(raw_details)["error"].count("[REDACTED]"), 2)


if __name__ == "__main__":
    unittest.main()
