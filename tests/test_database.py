from __future__ import annotations

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
                items=[{"input_text": "10.1000/a", "doi": "10.1000/a", "title": None}],
            )
            batch = db.get_batch(batch_id)
            self.assertIsNotNone(batch)
            self.assertEqual(len(batch["papers"]), 1)
            self.assertEqual(batch["reference_manager"], "zotero")
            paper_id = batch["papers"][0]["id"]
            db.update_paper(paper_id, authors_json=["Doe, Jane"], metadata_json={"doi": "10.1000/a"})
            paper = db.get_paper(paper_id)
            self.assertEqual(paper["authors"], ["Doe, Jane"])
            self.assertEqual(paper["metadata"]["doi"], "10.1000/a")


if __name__ == "__main__":
    unittest.main()
