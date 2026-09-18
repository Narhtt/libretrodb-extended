# libretrodb-extended

A modern rebuild of the [Libretro database](https://github.com/libretro/libretro-database)
as a single normalized SQLite file, covering all 146 systems, with CRC32 /
MD5 / SHA1 checksums, filesizes, serials, release dates, ratings and
normalized developer / publisher / genre / region tables.

Intended as an up-to-date, richer replacement for
[avojak/libretrodb-sqlite](https://github.com/avojak/libretrodb-sqlite).

## Contents

- `libretro2sqlite.py` — the converter (MIT)
- `database/` — the source `.rdb` files from libretro-database (MIT)
- `libretrodb.sqlite` — generated output (MIT); **not committed** — 
  download from the [Releases](../../releases) page or regenerate locally.
- `LICENSES/` — third-party license texts for the optional `libretrodb_tool.exe` binary.
- `libretrodb_tool.exe` — optional, compiled RetroArch helper (GPLv3)

## Requirements

- Python 3.8+
- The RetroArch `libretrodb_tool` binary (see "Building the tool" below)
- `database/` containing the `.rdb` files

On Windows, the tool needs three DLLs from MSYS2 in the same folder:
`libwinpthread-1.dll`, `libgcc_s_seh-1.dll`, `libstdc++-6.dll`.

## Building the tool (one-time)

You only need to build `libretrodb_tool` once. It converts the binary `.rdb`
format into JSON, which the Python script then consumes.

### Windows (MSYS2)

1. Install [MSYS2](https://www.msys2.org/).
2. Open the **MSYS2 MINGW64** shell (not UCRT64, not MSYS).
3. Install the toolchain:
   ```
   pacman -S --noconfirm --needed git make \
       mingw-w64-x86_64-toolchain mingw-w64-x86_64-python3
   ```
4. Clone and build:
   ```
   git clone --depth=1 https://github.com/libretro/RetroArch
   cd RetroArch/libretro-db
   make
   ```
5. Copy the tool and its DLLs into the project folder:
   ```
   cp libretrodb_tool.exe  /path/to/retrodb/data/
   cp /mingw64/bin/libwinpthread-1.dll \
      /mingw64/bin/libgcc_s_seh-1.dll \
      /mingw64/bin/libstdc++-6.dll    /path/to/retrodb/data/
   ```
6. Delete the cloned `RetroArch/` folder — it is no longer needed.

### Linux / macOS

```
git clone --depth=1 https://github.com/libretro/RetroArch
cd RetroArch/libretro-db
make
cp libretrodb_tool /path/to/retrodb/data/
```

Then in `libretro2sqlite.py`, set:

```console
TOOL = "./libretrodb_tool"
```

## Usage

From the project folder:

```console
python3 libretro2sqlite.py
```

You will see one line per system, then a total. The output file
`libretrodb.sqlite` is regenerated from scratch on every run.

Runtime: a few minutes on a modern SSD (up to ~10 minutes on slow storage 
or with `--thumbnails --vacuum`).

### Reproducible builds

Pass `SOURCE_DATE_EPOCH` to produce a byte-identical database across runs.

POSIX shell / bash:

```bash
SOURCE_DATE_EPOCH=0 python3 libretro2sqlite.py
```

PowerShell:

```powershell
$env:SOURCE_DATE_EPOCH = "0"
python3 libretro2sqlite.py
Remove-Item Env:\SOURCE_DATE_EPOCH   # optional cleanup
```

The timestamp recorded in the `meta` table is fixed to that epoch. This is
required if you want to verify a downloaded `libretrodb.sqlite` against a
published SHA-256 checksum.

### Post-build verification

Pass `--verify` to run sanity checks after the build:

```console
python3 libretro2sqlite.py --verify
```

The script asserts that games, ROMs, systems and names are non-zero, and
reports how many rows carry each optional field (developer, year, CRC, etc.).
Exits with code 1 if a critical check fails — useful in CI.

### Reclaiming space

`--dedup` and `--thumbnails` leave freed pages inside the SQLite file —
the file size does not shrink after a `DELETE` or an `ALTER TABLE`. Pass
`--vacuum` to compact the database at the end of the build:

```console
python3 libretro2sqlite.py --dedup --vacuum
```

This runs SQLite's `VACUUM` command, which rewrites the file with only the
live pages and rebuilds the indexes. Expect a 10–20% size reduction and
slightly faster lookups. Cost: a few extra seconds on a 300 MB database.

## Schema

```
games
  id, system, platform, manufacturer_id,
  developer_id, publisher_id, genre_id, franchise_id,
  region_id, enhancement_hw_id,
  name, description, serial, releaseyear, releasemonth, releaseday,
  users, tgdb_rating, esrb_rating, pegi_rating, cero_rating,
  bbfc_rating, elspa_rating, rumble, analog, coop,
  boxart_url, snap_url, title_url, logo_url   -- only with --thumbnails
  -- any extra RDB key becomes an additional TEXT column

roms
  id, game_id, rom_name, size, crc, md5, sha1

developers, publishers, genres, franchises,
regions, enhancement_hardware, manufacturers
  id, name
```

- `system` is a slug suitable for joins: `nintendo_game_boy_advance`
- `platform` is the official RetroArch name: `Nintendo - Game Boy Advance`
- A single game may have multiple rows in `roms` (multi-disc, multi-region)

## Command-line flags

| Flag | Effect |
|---|---|
| `--output PATH` | Write to a different file (default `./libretrodb.sqlite`) |
| `--thumbnails`  | Add `boxart_url`, `snap_url`, `title_url`, `logo_url` columns (+~300 MB) |
| `--dedup`       | Remove duplicate games that share a CRC32 (keeps the first) |
| `--vacuum`      | Compact the database at the end of the build. Reclaims the space freed by `--dedup` or `--thumbnails`. Slower, but produces the smallest possible file. |
| `--sample N`    | Process only the first N entries per system (testing) |
| `--verify`      | Run post-build sanity checks; exits non-zero if a critical check fails |

> [!IMPORTANT]
> `--dedup` runs before `--thumbnails` when both are set.

## Deduplication

The default build keeps every entry, because different systems legitimately
share ROMs (a Neo Geo game appears in both `fbneo_arcade_games` and
`snk_neo_geo`). If you want a one-row-per-ROM catalog, either pass `--dedup`
at build time, or dedup on the fly:

```sql
SELECT g.* FROM games g
JOIN roms r ON r.game_id = g.id
WHERE g.id IN (
    SELECT MIN(g2.id) FROM games g2
    JOIN roms r2 ON r2.game_id = g2.id
    WHERE r2.crc IS NOT NULL
    GROUP BY r2.crc
);
```
> Note: deduping by CRC32 is not collision-proof. For stricter uniqueness,
> use MD5 or SHA1 instead of CRC in the queries above.

## Thumbnail URLs

Each game row carries four pre-computed thumbnail URLs pointing at the
public libretro thumbnail server:

- `boxart_url` — cover art
- `snap_url`   — in-game screenshot
- `title_url`  — title screen
- `logo_url`   — game logo

These are generated using RetroArch's filename convention: illegal
characters (`&*/:<>?\|"`) are replaced with underscores. Many games have
no thumbnail on the server, so a 404 is normal — front-ends should fall
back to a placeholder image.

## Output size

The generated `libretrodb.sqlite` contains approximately **738,000 games**
and an equal number of ROM rows. File size:

| Build | Size |
|---|---|
| default | ~300 MB |
| `--dedup` + `--vacuum` | ~270 MB |
| `--thumbnails` | ~700 MB |
| `--thumbnails --dedup --vacuum` | ~630 MB |

> The `--dedup` flag removes ~74,000 duplicate rows (~10%).

## Example queries

Find a game by CRC32:

```sql
SELECT g.name, g.platform, g.releaseyear
FROM games g
JOIN roms r ON r.game_id = g.id
WHERE r.crc = 'FC6F3BC2';
```

Full metadata for a game, by serial:

```sql
SELECT
  g.name, g.platform, g.releaseyear,
  d.name AS developer, p.name AS publisher,
  ge.name AS genre, fr.name AS franchise,
  re.name AS region, m.name AS manufacturer,
  r.rom_name, r.size, r.crc, r.md5, r.sha1
FROM games g
LEFT JOIN developers d  ON d.id  = g.developer_id
LEFT JOIN publishers p  ON p.id  = g.publisher_id
LEFT JOIN genres     ge ON ge.id = g.genre_id
LEFT JOIN franchises fr ON fr.id = g.franchise_id
LEFT JOIN regions    re ON re.id = g.region_id
LEFT JOIN manufacturers m ON m.id = g.manufacturer_id
JOIN roms r ON r.game_id = g.id
WHERE g.serial = 'SLUS-00594';
```

## Updating the database

The `.rdb` files are updated upstream in
[libretro-database](https://github.com/libretro/libretro-database). To refresh:

```
git clone --depth=1 https://github.com/libretro/libretro-database
cp libretro-database/dat/rdb/*.rdb database/
python3 libretro2sqlite.py
```

## License

The converter script and the generated SQLite file are released under the
MIT License (see `LICENSE`).

The `.rdb` source data is MIT-licensed by the libretro-database project.

`libretrodb_tool.exe` is part of RetroArch and is GPLv3; see `LICENSES/`
if you redistribute the compiled binary.

## Binary distribution

If you downloaded a release archive, it may include `libretrodb_tool.exe`
and three MinGW runtime DLLs. These are compiled from the RetroArch and
MinGW-w64 projects and are redistributed under their respective licenses;
see the `LICENSES/` folder.

- `libretrodb_tool.exe` — GPLv3 (part of RetroArch).
  Source: https://github.com/libretro/RetroArch
- `libgcc_s_seh-1.dll`, `libstdc++-6.dll` — GPLv3 + GCC Runtime Library Exception.
- `libwinpthread-1.dll` — MinGW-w64 runtime license.
