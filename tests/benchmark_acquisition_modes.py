"""Repeatable local latency comparison; no network or institutional access.

Run: python -m tests.benchmark_acquisition_modes
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
import time
from pathlib import Path

from tests.test_acquisition_modes import _batch, _no_login
from tests.test_parallel_acquisition import _manager, _success


async def measure(mode: str, size: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as directory:
        manager, db, _, _ = _manager(Path(directory))
        publishers = ["ieee" if index % 2 else "sciencedirect" for index in range(size)]
        batch_id, papers = _batch(manager, db, mode, publishers)
        manager.race_delay_seconds = 0.005
        manager._ensure_institution_login = _no_login  # type: ignore[method-assign]
        active = {"oa": 0, "institution": 0}
        peak = dict(active)

        async def oa(paper):
            active["oa"] += 1
            peak["oa"] = max(peak["oa"], active["oa"])
            try:
                await asyncio.sleep(0.02)
                if int(paper["doi"].rsplit("/", 1)[1]) % 3 == 0:
                    return _success(
                        "open_access",
                        manager.settings.download_dir / paper["id"] / "open-access-main.pdf",
                    )
                return {"success": False, "source": "open_access", "error": "miss"}
            finally:
                active["oa"] -= 1

        async def institution(_batch_id, paper):
            active["institution"] += 1
            peak["institution"] = max(peak["institution"], active["institution"])
            try:
                await asyncio.sleep(0.04)
                return _success(
                    "institution",
                    manager.settings.download_dir / paper["id"] / "institution-main.pdf",
                )
            finally:
                active["institution"] -= 1

        manager._try_open_access_pdf = oa  # type: ignore[method-assign]
        manager._try_institution_pdf = institution  # type: ignore[method-assign]
        started = time.perf_counter()
        await manager._run_batch(batch_id)
        elapsed = time.perf_counter() - started
        verified = sum(
            db.get_paper(paper["id"])["pdf_status"] == "verified" for paper in papers
        )
        await manager.close()
        return {
            "mode": mode,
            "papers": size,
            "verified": verified,
            "seconds": round(elapsed, 3),
            "peak_oa": peak["oa"],
            "peak_institution": peak["institution"],
        }


async def main(sizes: list[int]) -> None:
    for size in sizes:
        for mode in ("legacy", "oa_parallel", "publisher_parallel", "full_parallel"):
            print(json.dumps(await measure(mode, size), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=[20, 50, 100])
    args = parser.parse_args()
    asyncio.run(main(args.sizes))
