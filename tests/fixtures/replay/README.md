# PNM replay fixtures

This directory is the future home of pixel-exact replay fixtures: recorded PNM frames with pinned measurement expectations that the test suite replays bit-exactly against the production code. The format (manifest schema, checksums over decompressed bytes, deterministic zlib compression, strict size budgets) is defined and enforced by `tests/support/pnm_replay.py`; the schema documentation lives in that module's docstring.

No fixtures are committed yet. The current tests generate synthetic fixtures at runtime; a small field corpus may follow later, captured from purpose-built, repository-owned test sheets after privacy and provenance review.

Content policy for anything committed here: synthetic or repository-owned material only. Never add personal or customer documents, personal names or usernames, home-directory paths, device serial numbers, network addresses, embedded document metadata, PDFs or PDF-derived rasters, or third-party copyrighted pages. Every payload must fit the documented size budget and be reproducible from repository-owned source.
