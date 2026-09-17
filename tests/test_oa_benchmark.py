from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

import httpx

from paper_endnote.oa_benchmark import (
    ALLOWED_METADATA_HOSTS,
    CandidateDiscoveryError,
    CandidateOnlyHTTP,
    discover_openalex,
    run_benchmark,
)


EMPTY_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom"></feed>"""


def _json_response(request: httpx.Request, value: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, request=request, json=value)


class OACandidateBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_only_report_uses_cache_and_never_requests_pdf_urls(self) -> None:
        requested: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requested.append(str(request.url))
            host = request.url.host
            url = unquote(str(request.url)).casefold()
            missing = "10.1000/missing" in url
            if host == "api.unpaywall.org":
                return _json_response(
                    request,
                    {
                        "doi": "10.1000/missing" if missing else "10.1000/existing",
                        "title": "Missing paper" if missing else "Existing paper",
                        "best_oa_location": {"url_for_pdf": "https://repo.example/missing.pdf"}
                        if missing
                        else None,
                        "oa_locations": [
                            {
                                "url_for_pdf": "https://repo.example/missing.pdf",
                                "url_for_landing_page": "https://repo.example/item/1",
                                "version": "acceptedVersion",
                                "license": "cc-by",
                                "host_type": "repository",
                            }
                        ]
                        if missing
                        else [],
                    },
                )
            if host == "api.openalex.org":
                doi = "10.1000/missing" if missing else "10.1000/existing"
                return _json_response(
                    request,
                    {
                        "id": "https://openalex.org/W1",
                        "doi": f"https://doi.org/{doi}",
                        "title": "Missing paper" if missing else "Existing paper",
                        "open_access": {"oa_status": "green" if missing else "closed"},
                        "locations": [
                            {
                                "is_oa": True,
                                "pdf_url": "https://repo.example/missing.pdf",
                                "landing_page_url": "https://repo.example/item/1",
                                "version": "acceptedVersion",
                                "license": "cc-by",
                                "source": {"type": "repository", "display_name": "Example repository"},
                            }
                        ]
                        if missing
                        else [],
                    },
                )
            if host == "api.core.ac.uk":
                return _json_response(
                    request,
                    {
                        "results": [
                            {
                                "id": 7,
                                "doi": "10.1000/missing",
                                "title": "Missing paper",
                                "downloadUrl": "https://core.ac.uk/download/7.pdf",
                                "sourceFulltextUrls": [],
                                "dataProviders": [{"name": "Example repository"}],
                                "links": [{"type": "display", "url": "https://core.ac.uk/works/7"}],
                            }
                        ]
                        if missing
                        else []
                    },
                )
            if host == "www.ebi.ac.uk":
                return _json_response(
                    request,
                    {
                        "resultList": {
                            "result": [
                                {
                                    "id": "PMC7",
                                    "source": "PMC",
                                    "doi": "10.1000/missing",
                                    "title": "Missing paper",
                                    "isOpenAccess": "Y",
                                    "license": "cc by",
                                    "fullTextUrlList": {
                                        "fullTextUrl": [
                                            {
                                                "availability": "Open access",
                                                "availabilityCode": "OA",
                                                "documentStyle": "pdf",
                                                "site": "Europe_PMC",
                                                "url": "https://europepmc.org/articles/PMC7?pdf=render",
                                            }
                                        ]
                                    },
                                }
                            ]
                            if missing
                            else []
                        }
                    },
                )
            if host == "export.arxiv.org":
                atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2601.00001</id>
    <title>Missing paper</title>
    <arxiv:doi>10.1000/MISSING</arxiv:doi>
    <link title="pdf" href="http://arxiv.org/pdf/2601.00001" rel="related" type="application/pdf"/>
  </entry>
</feed>"""
                return httpx.Response(
                    200,
                    request=request,
                    text=atom,
                    headers={"content-type": "application/atom+xml"},
                )
            raise AssertionError(f"Unexpected request: {request.url}")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = root / "papers.csv"
                fixture.write_text(
                    "doi,title\n10.1000/existing,Existing paper\n10.1000/missing,Missing paper\n",
                    encoding="utf-8",
                )
                baseline = root / "baseline.json"
                baseline.write_text(
                    json.dumps(
                        {
                            "network_pdf_count": 1,
                            "papers": [
                                {
                                    "doi": "10.1000/existing",
                                    "title": "Existing paper",
                                    "pdf_status": "verified",
                                    "pdf_attachments": [{"key": "A"}],
                                },
                                {
                                    "doi": "10.1000/missing",
                                    "title": "Missing paper",
                                    "pdf_status": "not_found",
                                    "pdf_attachments": [],
                                },
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                transport = CandidateOnlyHTTP(
                    root / "cache",
                    user_agent="test",
                    intervals={provider: 0.0 for provider in ("unpaywall", "openalex", "core", "europe_pmc", "arxiv")},
                    client=http_client,
                )
                report = await run_benchmark(
                    input_path=fixture,
                    baseline_path=baseline,
                    output_path=root / "report.json",
                    cache_dir=root / "cache",
                    email="researcher@example.org",
                    transport=transport,
                )

                self.assertEqual(report["summary"]["baseline_pdf_count"], 1)
                self.assertEqual(report["summary"]["baseline_missing_count"], 1)
                self.assertEqual(report["summary"]["unique_incremental_hit_count"], 1)
                self.assertEqual(report["summary"]["unique_incremental_dois"], ["10.1000/missing"])
                self.assertEqual(report["safety"]["pdf_downloads_attempted"], 0)
                self.assertEqual(report["safety"]["candidate_files_verified"], 0)
                self.assertEqual(report["transport"]["candidate_url_requests"], 0)
                self.assertEqual(report["provider_status"]["core"]["access_mode"], "anonymous_rate_limited")
                missing_paper = report["papers"][1]
                self.assertEqual(missing_paper["artifact_status"], "candidate_not_verified")
                self.assertTrue(missing_paper["incremental_hit"])
                self.assertTrue(
                    all(
                        candidate["candidate_status"] == "candidate_not_verified"
                        for candidate in missing_paper["candidates"]
                    )
                )
                self.assertTrue(all(httpx.URL(url).host in ALLOWED_METADATA_HOSTS for url in requested))
                self.assertFalse(any("repo.example" in url for url in requested))
                self.assertFalse(any("europepmc.org/articles" in url for url in requested))
                core_requests = [unquote(url) for url in requested if "api.core.ac.uk" in url]
                self.assertTrue(core_requests)
                self.assertTrue(all("exclude=fullText,abstract" in url for url in core_requests))

                first_request_count = len(requested)
                cached_report = await run_benchmark(
                    input_path=fixture,
                    baseline_path=baseline,
                    output_path=root / "cached-report.json",
                    cache_dir=root / "cache",
                    email="researcher@example.org",
                    transport=transport,
                )
                self.assertEqual(len(requested), first_request_count)
                self.assertGreater(cached_report["transport"]["metadata_cache_hits"], 0)

    async def test_provider_failure_is_isolated(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.core.ac.uk":
                return httpx.Response(500, request=request, text="error")
            if request.url.host == "export.arxiv.org":
                return httpx.Response(200, request=request, text=EMPTY_ATOM)
            if request.url.host == "api.unpaywall.org":
                return _json_response(
                    request,
                    {"doi": "10.1000/test", "title": "Test paper", "oa_locations": []},
                )
            if request.url.host == "api.openalex.org":
                return _json_response(
                    request,
                    {
                        "id": "https://openalex.org/W1",
                        "doi": "https://doi.org/10.1000/test",
                        "title": "Test paper",
                        "locations": [],
                    },
                )
            if request.url.host == "www.ebi.ac.uk":
                return _json_response(request, {"resultList": {"result": []}})
            raise AssertionError(request.url)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = root / "papers.csv"
                fixture.write_text("doi,title\n10.1000/test,Test paper\n", encoding="utf-8")
                baseline = root / "baseline.json"
                baseline.write_text(
                    json.dumps(
                        {
                            "network_pdf_count": 0,
                            "papers": [
                                {
                                    "doi": "10.1000/test",
                                    "title": "Test paper",
                                    "pdf_status": "not_found",
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                transport = CandidateOnlyHTTP(
                    root / "cache",
                    user_agent="test",
                    intervals={provider: 0.0 for provider in ("unpaywall", "openalex", "core", "europe_pmc", "arxiv")},
                    client=http_client,
                )
                report = await run_benchmark(
                    input_path=fixture,
                    baseline_path=baseline,
                    output_path=root / "report.json",
                    cache_dir=root / "cache",
                    email="researcher@example.org",
                    transport=transport,
                )
                self.assertEqual(report["papers"][0]["errors"], {"core": "core returned HTTP 500"})
                self.assertEqual(report["provider_status"]["core"]["paper_errors"], 1)
                self.assertEqual(report["summary"]["unique_incremental_hit_count"], 0)

    async def test_transport_refuses_candidate_hosts(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
            with tempfile.TemporaryDirectory() as directory:
                transport = CandidateOnlyHTTP(Path(directory), user_agent="test", client=client)
                with self.assertRaises(CandidateDiscoveryError):
                    await transport.get(
                        "test",
                        "https://repository.example/paper.pdf",
                        accept="application/pdf",
                    )

    async def test_openalex_closed_pdf_location_is_not_a_lawful_candidate(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return _json_response(
                request,
                {
                    "id": "https://openalex.org/W1",
                    "doi": "https://doi.org/10.1000/closed",
                    "title": "Closed paper",
                    "open_access": {"oa_status": "closed"},
                    "locations": [
                        {
                            "is_oa": False,
                            "pdf_url": "https://publisher.example/closed.pdf",
                            "landing_page_url": "https://publisher.example/article",
                            "version": "publishedVersion",
                        }
                    ],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with tempfile.TemporaryDirectory() as directory:
                transport = CandidateOnlyHTTP(
                    Path(directory),
                    user_agent="test",
                    intervals={"openalex": 0.0},
                    client=client,
                )
                candidates = await discover_openalex(
                    transport,
                    "10.1000/closed",
                    "Closed paper",
                    email="researcher@example.org",
                    api_key="",
                )
                self.assertEqual(candidates, [])

    async def test_provider_circuit_stops_repeated_failures(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "api.core.ac.uk":
                return httpx.Response(500, request=request, text="error")
            return httpx.Response(404, request=request, text="not found")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fixture = root / "papers.csv"
                fixture.write_text(
                    "doi,title\n"
                    + "\n".join(f"10.1000/test{index},Test paper {index}" for index in range(4))
                    + "\n",
                    encoding="utf-8",
                )
                baseline = root / "baseline.json"
                baseline.write_text(
                    json.dumps(
                        {
                            "network_pdf_count": 0,
                            "papers": [
                                {
                                    "doi": f"10.1000/test{index}",
                                    "title": f"Test paper {index}",
                                    "pdf_status": "not_found",
                                }
                                for index in range(4)
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                transport = CandidateOnlyHTTP(
                    root / "cache",
                    user_agent="test",
                    intervals={provider: 0.0 for provider in ("unpaywall", "openalex", "core", "europe_pmc", "arxiv")},
                    client=client,
                )
                report = await run_benchmark(
                    input_path=fixture,
                    baseline_path=baseline,
                    output_path=root / "report.json",
                    cache_dir=root / "cache",
                    email="researcher@example.org",
                    transport=transport,
                )
                self.assertEqual(report["transport"]["network_requests_by_provider"]["core"], 3)
                self.assertTrue(report["provider_status"]["core"]["circuit_open"])
                self.assertIn("provider circuit open", report["papers"][3]["errors"]["core"])


if __name__ == "__main__":
    unittest.main()
