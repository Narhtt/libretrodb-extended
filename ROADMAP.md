# Roadmap

## v1.0.0 — initial release [x]

- [x] Convert all 146 Libretro `.rdb` files into a normalized SQLite DB
- [x] 738k games, full CRC32 / MD5 / SHA1 / size / serial coverage
- [x] Normalized developers / publishers / genres / franchises / regions /
      enhancement_hardware / manufacturers
- [x] `--thumbnails` flag to add boxart / snap / title / logo URLs
- [x] `--dedup` flag to collapse duplicate CRCs
- [x] `--vacuum` flag to compact the DB after build
- [x] `--sample N` for fast test runs
- [x] `--verify` post-build sanity checks (exit code 1 on failure)
- [x] `SOURCE_DATE_EPOCH` support for byte-reproducible builds
- [x] `meta` table with schema version, generation timestamp, source URLs
- [x] MIT-licensed script and DB

## v1.1 — quality of life []

- [ ] `--jobs N`: parallelize `libretrodb_tool` calls with
      `concurrent.futures.ThreadPoolExecutor`. Reduces wall time to ~2–3 min.
- [ ] `--dedup-by md5|sha1|crc` with automatic fallback when the preferred
      hash is missing.

## v1.2 — companion tooling []

- [ ] `lpl-from-sqlite.py`: reads the DB and emits RetroArch `.lpl` files
      per system. Bridges `libretrodb-extended` to
      [retroarch-playlist-generator](link-to-your-other-repo).
- [ ] `enrich-csv.py`: takes a collection CSV (as produced by the playlist
      generator project) and adds `crc`, `md5`, `sha1`, `release_year`,
      `developer`, `publisher`, `genre` from the DB. The two projects then
      form a pipeline:
      `libretrodb-extended → enrich-csv → playlist generator`.

## v2.0 — speculative []

- [ ] FTS5 index on `games.name` and `games.description` for fast
      fuzzy search.
- [ ] Optional XML / CSV export formats.
- [ ] GitHub Actions weekly rebuild + release publish.
- [ ] A small Rust or Go CLI that reads the DB and answers
      "what game is this file?" from CRC/MD5/SHA1 without a front-end.

## Not planned

- Game metadata scraping (IGDB, MobyGames) — out of scope.
- ROM downloading or management — out of scope.
- GUI — out of scope; the DB is meant to be consumed by other tools.
