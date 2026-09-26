from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from paper_endnote.browser import LoginTimeoutError
from paper_endnote.publishers import publisher_key
from tests.test_parallel_acquisition import _manager, _success


def _batch(manager, db, mode: str, publishers: list[str]) -> tuple[str, list[dict]]:
    manager.settings.auto_commit = False
    manager.institution_gap_seconds = 0
    manager.race_delay_seconds = 0.05
    snapshot = {
        **manager.settings.institution.as_dict(),
        "_acquisition_sources": ["open_access", "institution"],
        "_auto_institution": True,
        "_acquisition_mode": mode,
    }
    batch_id = db.create_batch(
        name=mode,
        target_library="Test",
        library_mode="new",
        items=[
            {"input_text": f"10.1000/{index}", "doi": f"10.1000/{index}", "title": f"Paper {index}"}
            for index in range(len(publishers))
        ],
        institution_config=snapshot,
    )
    papers = db.get_batch(batch_id)["papers"]
    for paper, publisher in zip(papers, publishers):
        db.update_paper(
            paper["id"],
            doi=paper["input_doi"],
            title=paper["input_title"],
            metadata_status="verified",
            status="ready",
            institution_publisher=publisher,
        )
    return batch_id, db.get_batch(batch_id)["papers"]


class AcquisitionModeTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_batch_snapshot_is_persisted_as_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, old_batch_id, _ = _manager(Path(directory))
            manager.settings.acquisition_mode = "full_parallel"
            manager._institution_profile_for_batch(old_batch_id)
            snapshot = db.get_batch(old_batch_id)["institution_config"]
            self.assertEqual(snapshot["_acquisition_mode"], "legacy")
            self.assertEqual(manager._acquisition_mode_for_batch(old_batch_id), "legacy")

    async def test_institution_wait_does_not_block_later_oa(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, _ = _batch(manager, db, "oa_parallel", ["ieee"] * 6)
            manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
            all_oa_started = asyncio.Event()
            institution_started = asyncio.Event()
            release_institution = asyncio.Event()
            oa_count = 0

            async def oa(_paper):
                nonlocal oa_count
                oa_count += 1
                if oa_count == 6:
                    all_oa_started.set()
                return {"success": False, "source": "open_access", "error": "miss"}

            async def institution(_batch_id, _paper):
                institution_started.set()
                await release_institution.wait()
                return {"success": False, "source": "institution", "error": "miss"}

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            task = asyncio.create_task(manager._run_batch(batch_id))
            try:
                await asyncio.wait_for(institution_started.wait(), 3)
                await asyncio.wait_for(all_oa_started.wait(), 3)
                self.assertEqual(oa_count, 6)
            finally:
                release_institution.set()
                await asyncio.wait_for(task, 3)

    async def test_legacy_auto_commit_does_not_wait_on_its_own_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "legacy", ["ieee"])
            manager.settings.auto_commit = True
            db.update_paper(papers[0]["id"], status="endnote_pending")
            committed = asyncio.Event()

            async def commit(_batch):
                committed.set()

            manager._commit_zotero_batch = commit  # type: ignore[method-assign]
            manager.start(batch_id)
            await asyncio.wait_for(manager.wait_for_batch_acquisition(batch_id), 3)
            self.assertTrue(committed.is_set())

    async def test_uncertain_zotero_commit_is_not_submitted_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "legacy", ["ieee"])
            db.update_paper(papers[0]["id"], status="endnote_pending")

            class UncertainZotero:
                def __init__(self):
                    self.calls = 0

                async def ensure_collection(self, _name, *, create):
                    return "COL1"

                async def commit_paper(self, _collection, _metadata, _pdf):
                    self.calls += 1
                    raise RuntimeError("response lost after write")

            zotero = UncertainZotero()
            manager.zotero = zotero  # type: ignore[assignment]
            await manager._commit_batch(batch_id, wait_for_acquisition=False)
            await manager._commit_batch(batch_id, wait_for_acquisition=False)
            self.assertEqual(zotero.calls, 1)
            self.assertEqual(db.get_paper(papers[0]["id"])["endnote_status"], "uncertain")

    async def test_legacy_batches_share_one_institution_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            first_id, first_papers = _batch(manager, db, "legacy", ["ieee"])
            second_id, second_papers = _batch(manager, db, "legacy", ["ieee"])
            for paper in first_papers + second_papers:
                db.update_paper(
                    paper["id"], status="institution_pending",
                    source_url="https://ieeexplore.ieee.org/document/123",
                )
            profile = manager.settings.institution
            manager._institution_ready_sessions.add((profile.id.casefold(), "ieee"))
            started = asyncio.Event()
            release = asyncio.Event()
            active = 0
            peak = 0

            async def acquire(paper_id, **_kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                started.set()
                await release.wait()
                active -= 1
                db.update_paper(paper_id, status="needs_pdf", needs_action="manual_pdf")
                return {"path": "unused"}

            manager.acquire_institution_pdf = acquire  # type: ignore[method-assign]
            manager.start(first_id)
            manager.start(second_id)
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.sleep(0.05)
            self.assertEqual(peak, 1)
            release.set()
            await asyncio.wait_for(
                asyncio.gather(
                    manager.wait_for_batch_acquisition(first_id),
                    manager.wait_for_batch_acquisition(second_id),
                ),
                3,
            )
            self.assertEqual(peak, 1)

    async def test_proxy_publisher_is_normalized_before_slot_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "publisher_parallel", ["generic"])
            proxy_url = "https://ieeexplore-ieee-org.proxy3.library.mcgill.ca/document/123"
            db.update_paper(
                papers[0]["id"], metadata_json={"url": proxy_url},
                institution_publisher=publisher_key(proxy_url),
            )
            paper = db.get_paper(papers[0]["id"])
            resolved = await manager._resolve_paper_publisher(batch_id, paper)
            self.assertEqual(resolved["institution_publisher"], "ieee")

    async def test_quick_resume_restarts_a_batch_that_is_winding_down(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "oa_parallel", ["ieee"])
            entered = asyncio.Event()
            release = asyncio.Event()
            restarted = asyncio.Event()
            runs = 0

            async def run(_batch_id):
                nonlocal runs
                runs += 1
                if runs == 1:
                    entered.set()
                    await release.wait()
                else:
                    restarted.set()

            manager._run_batch = run  # type: ignore[method-assign]
            manager.start(batch_id)
            await asyncio.wait_for(entered.wait(), 3)
            manager.pause(batch_id)
            manager.resume(batch_id)
            self.assertFalse(db.batch_is_paused(batch_id))
            release.set()
            await asyncio.wait_for(restarted.wait(), 3)
            self.assertEqual(runs, 2)
            self.assertEqual(db.get_paper(papers[0]["id"])["status"], "ready")

    async def test_oa_parallel_runs_four_oa_before_serial_institution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "oa_parallel", ["ieee"] * 4)
            manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
            release_oa = asyncio.Event()
            four_oa = asyncio.Event()
            active_oa = 0
            max_oa = 0
            active_institution = 0
            max_institution = 0

            async def oa(_paper):
                nonlocal active_oa, max_oa
                active_oa += 1
                max_oa = max(max_oa, active_oa)
                if active_oa == 4:
                    four_oa.set()
                await release_oa.wait()
                active_oa -= 1
                return {"success": False, "source": "open_access", "error": "miss"}

            async def institution(_batch_id, _paper):
                nonlocal active_institution, max_institution
                active_institution += 1
                max_institution = max(max_institution, active_institution)
                await asyncio.sleep(0.01)
                active_institution -= 1
                return {"success": False, "source": "institution", "error": "miss"}

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            batch_task = asyncio.create_task(manager._run_batch(batch_id))
            await asyncio.wait_for(four_oa.wait(), 3)
            self.assertEqual(active_institution, 0)
            release_oa.set()
            await asyncio.wait_for(batch_task, 3)
            self.assertEqual(max_oa, 4)
            self.assertEqual(max_institution, 1)
            self.assertTrue(
                all(db.get_paper(paper["id"])["status"] == "needs_pdf" for paper in papers)
            )

    async def test_publisher_parallel_delays_institution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "publisher_parallel", ["ieee"])
            manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
            institution_started = asyncio.Event()
            oa_cancelled = asyncio.Event()

            async def oa(_paper):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    oa_cancelled.set()
                    raise

            async def institution(_batch_id, paper):
                institution_started.set()
                return _success(
                    "institution",
                    manager.settings.download_dir / paper["id"] / "institution-main.pdf",
                )

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            batch_task = asyncio.create_task(manager._run_batch(batch_id))
            await asyncio.sleep(0.02)
            self.assertFalse(institution_started.is_set())
            await asyncio.wait_for(institution_started.wait(), 3)
            await asyncio.wait_for(batch_task, 3)
            self.assertTrue(oa_cancelled.is_set())
            self.assertEqual(db.get_paper(papers[0]["id"])["pdf_status"], "verified")

    async def test_cross_publisher_overlaps_but_same_publisher_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(
                manager, db, "publisher_parallel", ["ieee", "sciencedirect", "ieee"]
            )
            manager.race_delay_seconds = 0
            manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
            two_started = asyncio.Event()
            release = asyncio.Event()
            active: list[str] = []
            max_total = 0
            max_ieee = 0

            async def oa(_paper):
                await asyncio.sleep(60)

            async def institution(_batch_id, paper):
                nonlocal max_total, max_ieee
                publisher = paper["institution_publisher"]
                active.append(publisher)
                max_total = max(max_total, len(active))
                max_ieee = max(max_ieee, active.count("ieee"))
                if len(active) == 2:
                    two_started.set()
                await release.wait()
                active.remove(publisher)
                return _success(
                    "institution",
                    manager.settings.download_dir / paper["id"] / "institution-main.pdf",
                )

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            batch_task = asyncio.create_task(manager._run_batch(batch_id))
            await asyncio.wait_for(two_started.wait(), 3)
            self.assertCountEqual(active, ["ieee", "sciencedirect"])
            release.set()
            await asyncio.wait_for(batch_task, 3)
            self.assertEqual(max_total, 2)
            self.assertEqual(max_ieee, 1)
            self.assertTrue(
                all(db.get_paper(paper["id"])["pdf_status"] == "verified" for paper in papers)
            )

    async def test_full_parallel_overlaps_same_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "full_parallel", ["ieee", "ieee"])
            manager.race_delay_seconds = 0
            manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
            two_started = asyncio.Event()
            release = asyncio.Event()
            active = 0
            max_active = 0

            async def oa(_paper):
                await asyncio.sleep(60)

            async def institution(_batch_id, paper):
                nonlocal active, max_active
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    two_started.set()
                await release.wait()
                active -= 1
                return _success(
                    "institution",
                    manager.settings.download_dir / paper["id"] / "institution-main.pdf",
                )

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            batch_task = asyncio.create_task(manager._run_batch(batch_id))
            await asyncio.wait_for(two_started.wait(), 3)
            release.set()
            await asyncio.wait_for(batch_task, 3)
            self.assertEqual(max_active, 2)
            self.assertTrue(
                all(db.get_paper(paper["id"])["pdf_status"] == "verified" for paper in papers)
            )

    async def test_mixed_batches_use_conservative_shared_institution_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            serial_id, _ = _batch(manager, db, "oa_parallel", ["ieee"])
            parallel_id, _ = _batch(manager, db, "full_parallel", ["ieee", "ieee"])
            manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
            manager.race_delay_seconds = 0
            first_started = asyncio.Event()
            release = asyncio.Event()
            active = 0
            max_during_overlap = 0

            async def oa(_paper):
                return {"success": False, "source": "open_access", "error": "miss"}

            async def institution(batch_id, _paper):
                nonlocal active, max_during_overlap
                active += 1
                if batch_id == serial_id:
                    first_started.set()
                if not release.is_set():
                    max_during_overlap = max(max_during_overlap, active)
                await release.wait()
                active -= 1
                return {"success": False, "source": "institution", "error": "miss"}

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            serial_task = asyncio.create_task(manager._run_batch(serial_id))
            await asyncio.wait_for(first_started.wait(), 3)
            parallel_task = asyncio.create_task(manager._run_batch(parallel_id))
            await asyncio.sleep(0.05)
            self.assertEqual(active, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(serial_task, parallel_task), 3)
            self.assertEqual(max_during_overlap, 1)

    async def test_pause_does_not_start_queued_oa_and_resume_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "oa_parallel", ["ieee"] * 5)
            release = asyncio.Event()
            four_started = asyncio.Event()
            started: list[str] = []

            async def oa(paper):
                started.append(paper["id"])
                if len(started) == 4:
                    four_started.set()
                await release.wait()
                return _success(
                    "open_access",
                    manager.settings.download_dir / paper["id"] / "open-access-main.pdf",
                )

            async def institution(_batch_id, _paper):
                self.fail("Institution must not start when OA verifies")

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager._try_institution_pdf = institution  # type: ignore[method-assign]
            manager.start(batch_id)
            await asyncio.wait_for(four_started.wait(), 3)
            manager.pause(batch_id)
            release.set()
            await asyncio.wait_for(manager.wait_for_batch_acquisition(batch_id), 3)
            self.assertEqual(len(started), 4)
            self.assertNotEqual(db.get_paper(papers[-1]["id"])["pdf_status"], "verified")
            manager.resume(batch_id)
            await asyncio.wait_for(manager.wait_for_batch_acquisition(batch_id), 3)
            self.assertEqual(len(started), 5)
            self.assertEqual(db.get_paper(papers[-1]["id"])["pdf_status"], "verified")

    async def test_delete_cancels_parallel_oa_before_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "oa_parallel", ["ieee"] * 2)
            started = asyncio.Event()
            started_count = 0
            cancelled = 0

            async def oa(_paper):
                nonlocal started_count, cancelled
                started_count += 1
                if started_count == 2:
                    started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled += 1
                    raise

            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            manager.start(batch_id)
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.wait_for(
                manager.prepare_batch_deletion(
                    batch_id, [paper["id"] for paper in papers]
                ),
                3,
            )
            self.assertEqual(cancelled, 2)
            self.assertFalse(manager.is_batch_running(batch_id))

    async def test_login_timeout_preserves_oa_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "publisher_parallel", ["ieee"])
            manager.race_delay_seconds = 0

            async def login(_batch_id, _paper):
                raise LoginTimeoutError("login timed out")

            async def oa(paper):
                await asyncio.sleep(0.02)
                return _success(
                    "open_access",
                    manager.settings.download_dir / paper["id"] / "open-access-main.pdf",
                )

            manager._ensure_institution_login = login  # type: ignore[method-assign]
            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            await asyncio.wait_for(manager._run_batch(batch_id), 3)
            paper = db.get_paper(papers[0]["id"])
            self.assertEqual(paper["pdf_status"], "verified")
            self.assertEqual(paper["needs_action"], "commit_endnote")

    async def test_login_timeout_with_oa_miss_goes_to_institution_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager, db, _, _ = _manager(Path(directory))
            batch_id, papers = _batch(manager, db, "publisher_parallel", ["ieee"])

            async def login(_batch_id, _paper):
                raise LoginTimeoutError("login timed out")

            async def oa(_paper):
                return {"success": False, "source": "open_access", "error": "miss"}

            manager._ensure_institution_login = login  # type: ignore[method-assign]
            manager._try_open_access_pdf = oa  # type: ignore[method-assign]
            await asyncio.wait_for(manager._run_batch(batch_id), 3)
            paper = db.get_paper(papers[0]["id"])
            self.assertEqual(paper["needs_action"], "manual_institution")
            self.assertEqual(paper["institution_state"], "expired")


async def _no_login(_batch_id, _paper) -> None:
    return None


if __name__ == "__main__":
    unittest.main()
