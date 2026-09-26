# Reusable acquisition sample

Created on 2026-09-26 using `auto-paper-find-skill` from 50 saved fuzzy clues across 10 domains. Crossref identities, authors and versions were reviewed before sampling. This is a convenience smoke-test corpus, not a systematic literature review or a representative estimate of retrieval success.

- `fuzzy-clues.tsv`: domain, initial query and Chinese clue.
- `corpus-50.csv`: 50 verified bibliographic records, five per domain.
- `sample-20.csv`: frozen random sample, in draw order.
- `sample-manifest.json`: seed, selected IDs/DOIs, domain counts and input hashes.
- `*.sources.md`: identity sources, version choices and abstract-based notes where available.

Sampling used `random.Random(20260926).sample(range(50), 20)`, without replacement or post-download substitutions. Both CSV files passed round-trip validation through the project's actual input parser. The manifest's `corpus_records_sha256` identifies the original intermediate JSON; that JSON and raw Crossref responses remain in the originating local run directory, not this fixture. Source notes referring to raw search JSON likewise refer to the local evidence archive.

To reuse, import either CSV through the application. Downloading requires an appropriate source/session; this fixture includes no PDFs, credentials or institutional access rights. Use a new batch and disable automatic Zotero submission for acquisition-only tests.

## Recorded live run

On 2026-09-26, the 20-paper sample ran in `full_parallel` mode with OA and McGill institution paths enabled. Login wait was 90 seconds and automatic submission was disabled. Observed elapsed time was 163 seconds:

- Metadata verified: 20/20.
- PDFs verified: 17/20 (15 institution, 2 OA).
- No verified PDF: 2/20 (ATLAS paper `10.1016/j.physletb.2012.08.020`, Prospect Theory `10.2307/1914185`).
- Manual identity review: 1/20 (`10.1126/science.275.5306.1593`).
- Sampled peak workers: metadata 3, OA 4, institution 4, validation 1. Sampling was every two seconds and can miss brief activity.
- All phase counters were zero after completion, PDF hashes and paths were distinct, and no Zotero submission was made.

An independent text check found the expected DOI on the first three pages of 11 verified PDFs and all checked title keywords in all 17. This is not a complete manual content review. Original application settings were restored after the run.

The test exposed a CSV issue where escaped quotes after the five-line dialect sample were parsed incorrectly. The parser now explicitly supports doubled CSV quotes; a regression test covers this case. No download-acceptance rules were relaxed.

These observations do not establish speedup versus legacy mode or exhaustive lifecycle correctness. The local run's detailed events, timing samples, file hashes and outcomes remain under `outputs/paper-input/20260926-040241/` and are intentionally not committed.
