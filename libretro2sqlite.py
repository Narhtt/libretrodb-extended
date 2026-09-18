#!/usr/bin/env python3
"""
libretro2sqlite.py

Convert Libretro `.rdb` database files into a single normalized SQLite
database.  Requires `libretrodb_tool` (built from the RetroArch source)
to be present in the same directory.

See README.md for build and usage instructions.

SPDX-License-Identifier: MIT
"""

import argparse
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RDB_DIR          = Path("database")
TOOL             = "./libretrodb_tool.exe"   # "./libretrodb_tool" on Linux/macOS
DEFAULT_OUTPUT   = "./libretrodb.sqlite"
SCHEMA_VERSION   = 1
LIBRETRO_DB_URL  = "https://github.com/libretro/libretro-database"

LOOKUP_FIELDS = {
    "developer":      "developers",
    "publisher":      "publishers",
    "genre":          "genres",
    "franchise":      "franchises",
    "region":         "regions",
    "enhancement_hw": "enhancement_hardware",
}

ROM_KEYS = {"rom_name", "size", "crc", "md5", "sha1"}

GAME_COLUMNS = [
    ("name",         "TEXT"),
    ("description",  "TEXT"),
    ("thumbnail_name", "TEXT"),
    ("serial",       "TEXT"),
    ("releaseyear",  "INTEGER"),
    ("releasemonth", "INTEGER"),
    ("releaseday",   "INTEGER"),
    ("users",        "INTEGER"),
    ("tgdb_rating",  "INTEGER"),
    ("esrb_rating",  "TEXT"),
    ("pegi_rating",  "TEXT"),
    ("cero_rating",  "TEXT"),
    ("bbfc_rating",  "TEXT"),
    ("elspa_rating", "TEXT"),
    ("rumble",       "TEXT"),
    ("analog",       "TEXT"),
    ("coop",         "TEXT"),
]

MANUFACTURER_BY_PREFIX = {
    "Nintendo":     "Nintendo",
    "Sega":         "Sega",
    "Sony":         "Sony",
    "Atari":        "Atari",
    "Commodore":    "Commodore",
    "SNK":          "SNK",
    "NEC":          "NEC",
    "Bandai":       "Bandai",
    "Mattel":       "Mattel",
    "Coleco":       "Coleco",
    "Microsoft":    "Microsoft",
    "Philips":      "Philips",
    "Sinclair":     "Sinclair",
    "Sharp":        "Sharp",
    "Casio":        "Casio",
    "Emerson":      "Emerson",
    "Fairchild":    "Fairchild",
    "GCE":          "GCE",
    "Magnavox":     "Magnavox",
    "Interton":     "Interton",
    "Entex":        "Entex",
    "Epoch":        "Epoch",
    "Funtech":      "Funtech",
    "Hartung":      "Hartung",
    "LeapFrog":     "LeapFrog",
    "RCA":          "RCA",
    "Spectravideo": "Spectravideo",
    "Tiger":        "Tiger",
    "VTech":        "VTech",
    "Watara":       "Watara",
}

THUMBNAIL_BASE = "https://thumbnails.libretro.com"
THUMBNAIL_TYPES = {
    "boxart_url": "Named_Boxarts",
    "snap_url":   "Named_Snaps",
    "title_url":  "Named_Titles",
    "logo_url":   "Named_Logos",
}
THUMBNAIL_ILLEGAL_CHARS = '&*/:<>?\\|"'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalise(value):
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return value


def existing_columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def get_or_create(cur, table, value):
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    cur.execute(f"SELECT id FROM {table} WHERE name = ?", (value,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(f"INSERT INTO {table} (name) VALUES (?)", (value,))
    return cur.lastrowid


def manufacturer_for(platform_name):
    for prefix, manufacturer in MANUFACTURER_BY_PREFIX.items():
        if platform_name.startswith(prefix):
            return manufacturer
    return None


def scrub_thumbnail_name(name):
    if not name:
        return None
    for ch in THUMBNAIL_ILLEGAL_CHARS:
        name = name.replace(ch, "_")
    return name


def build_thumbnail_url(platform, name, thumb_type):
    safe = scrub_thumbnail_name(name)
    if not safe:
        return None
    return (
        f"{THUMBNAIL_BASE}/"
        f"{quote(platform, safe='')}/"
        f"{thumb_type}/"
        f"{quote(safe, safe='')}.png"
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_schema(cur):
    base_cols = [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "system TEXT NOT NULL",
        "platform TEXT NOT NULL",
        "manufacturer_id INTEGER",
    ]
    base_cols += [f"{k}_id INTEGER" for k in LOOKUP_FIELDS]
    cur.execute(f"CREATE TABLE games ({', '.join(base_cols)})")
    for name, sqltype in GAME_COLUMNS:
        cur.execute(f'ALTER TABLE games ADD COLUMN "{name}" {sqltype}')

    cur.execute("""
        CREATE TABLE roms (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id   INTEGER NOT NULL,
            rom_name  TEXT,
            size      INTEGER,
            crc       TEXT,
            md5       TEXT,
            sha1      TEXT,
            FOREIGN KEY (game_id) REFERENCES games(id)
        )
    """)

    for table in set(LOOKUP_FIELDS.values()):
        cur.execute(f"CREATE TABLE {table} "
                    f"(id INTEGER PRIMARY KEY, name TEXT UNIQUE)")
    cur.execute("CREATE TABLE manufacturers "
                "(id INTEGER PRIMARY KEY, name TEXT UNIQUE)")

    cur.execute("""
        CREATE TABLE meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    cur.execute("CREATE INDEX idx_games_system ON games(system)")
    cur.execute("CREATE INDEX idx_roms_game    ON roms(game_id)")
    cur.execute("CREATE INDEX idx_roms_crc     ON roms(crc)")
    cur.execute("CREATE INDEX idx_roms_md5     ON roms(md5)")


# ---------------------------------------------------------------------------
# Per-RDB processing
# ---------------------------------------------------------------------------

def process_rdb(conn, rdb_path, system_slug, platform_name, sample=None):
    cur = conn.cursor()

    proc = subprocess.run(
        [TOOL, rdb_path.as_posix(), "list"],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    stdout = proc.stdout or ""
    if not stdout.strip():
        print(f"  ! No output for {rdb_path.name}")
        return 0

    entries = []
    for line in stdout.splitlines():
        line = line.strip().replace("\\", "\\\\")
        if not line:
            continue
        with contextlib.suppress(json.JSONDecodeError):
            obj = json.loads(line)
            if obj.get("name"):
                if isinstance(obj.get("serial"), str):
                    with contextlib.suppress(ValueError, UnicodeDecodeError):
                        obj["serial"] = bytes.fromhex(obj["serial"]).decode("utf-8")
                entries.append(obj)

    if sample is not None:
        entries = entries[:sample]

    if not entries:
        print(f"  ! No usable entries in {rdb_path.name}")
        return 0

    known_keys = {n for n, _ in GAME_COLUMNS} | set(LOOKUP_FIELDS) | ROM_KEYS
    all_keys = set()
    for e in entries:
        all_keys.update(e.keys())
    extra_keys = sorted(all_keys - known_keys)

    existing = existing_columns(cur, "games")
    for key in extra_keys:
        if key not in existing:
            cur.execute(f'ALTER TABLE games ADD COLUMN "{key}" TEXT')
            existing.add(key)

    fixed_cols = ["system", "platform", "manufacturer_id"]
    fixed_cols += [f"{k}_id" for k in LOOKUP_FIELDS]
    fixed_cols += [n for n, _ in GAME_COLUMNS]
    fixed_cols += extra_keys
    placeholders = ",".join("?" * len(fixed_cols))
    insert_sql = (f'INSERT INTO games ({",".join(fixed_cols)}) '
                  f'VALUES ({placeholders})')

    rom_sql = ("INSERT INTO roms (game_id, rom_name, size, crc, md5, sha1) "
               "VALUES (?,?,?,?,?,?)")

    manufacturer_id = get_or_create(cur, "manufacturers",
                                    manufacturer_for(platform_name))

    count = 0
    for e in entries:
        region_raw = normalise(e.get("region")) or ""
        region_primary = region_raw.split(",")[0].strip() or None

        fk_ids = {
            "developer":      get_or_create(cur, "developers",
                                            normalise(e.get("developer"))),
            "publisher":      get_or_create(cur, "publishers",
                                            normalise(e.get("publisher"))),
            "genre":          get_or_create(cur, "genres",
                                            normalise(e.get("genre"))),
            "franchise":      get_or_create(cur, "franchises",
                                            normalise(e.get("franchise"))),
            "region":         get_or_create(cur, "regions", region_primary),
            "enhancement_hw": get_or_create(cur, "enhancement_hardware",
                                            normalise(e.get("enhancement_hw"))),
        }

        row = [system_slug, platform_name, manufacturer_id]
        row += [fk_ids[k] for k in LOOKUP_FIELDS]
        for name, sqltype in GAME_COLUMNS:
            if name == "thumbnail_name":
                row.append(scrub_thumbnail_name(e.get("name")))
            else:
                v = e.get(name)
                row.append(to_int(v) if sqltype == "INTEGER" else v)
        for k in extra_keys:
            v = e.get(k)
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            row.append(v)

        cur.execute(insert_sql, row)
        game_id = cur.lastrowid

        cur.execute(rom_sql, (
            game_id,
            e.get("rom_name"),
            to_int(e.get("size")),
            e.get("crc"),
            e.get("md5"),
            e.get("sha1"),
        ))
        count += 1

    conn.commit()
    return count


# ---------------------------------------------------------------------------
# Post-processing
# ---------------------------------------------------------------------------

def populate_thumbnail_urls(conn):
    cur = conn.cursor()
    for col in THUMBNAIL_TYPES:
        cur.execute(f"ALTER TABLE games ADD COLUMN {col} TEXT")

    rows = cur.execute("SELECT id, platform, name FROM games").fetchall()
    for col, thumb_type in THUMBNAIL_TYPES.items():
        payload = [
            (build_thumbnail_url(platform, name, thumb_type), gid)
            for gid, platform, name in rows
            if platform and name
        ]
        cur.executemany(f"UPDATE games SET {col} = ? WHERE id = ?", payload)
        print(f"  {col}: populated")

    conn.commit()
    print(f"Thumbnail URLs populated for {len(rows)} games")


def dedup_by_crc(conn):
    cur = conn.cursor()
    before = cur.execute("SELECT COUNT(*) FROM games").fetchone()[0]

    cur.execute("""
        DELETE FROM games WHERE id IN (
            SELECT g.id FROM games g
            JOIN roms r ON r.game_id = g.id
            WHERE r.crc IS NOT NULL
              AND g.id NOT IN (
                  SELECT MIN(g2.id) FROM games g2
                  JOIN roms r2 ON r2.game_id = g2.id
                  WHERE r2.crc IS NOT NULL
                  GROUP BY r2.crc
              )
        )
    """)
    cur.execute("DELETE FROM roms WHERE game_id NOT IN (SELECT id FROM games)")
    conn.commit()

    after = cur.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    print(f"Dedup by CRC32: removed {before - after} duplicate games")

def verify(conn) -> int:
    """Run post-build sanity checks.  Returns 0 on success, 1 on failure."""
    cur = conn.cursor()
    failures = []

    def q1(sql, *args):
        row = cur.execute(sql, args).fetchone()
        return row[0] if row else 0

    checks = [
        # (label, value, minimum, critical?)
        ("games total",           q1("SELECT COUNT(*) FROM games"),         1,     True),
        ("roms total",            q1("SELECT COUNT(*) FROM roms"),          1,     True),
        ("systems",               q1("SELECT COUNT(DISTINCT system) FROM games"), 100, True),
        ("games with a name",     q1("SELECT COUNT(*) FROM games WHERE name IS NOT NULL AND name != ''"),  1, True),
        ("games with a developer", q1("SELECT COUNT(*) FROM games WHERE developer_id IS NOT NULL"),  1, False),
        ("games with a release year", q1("SELECT COUNT(*) FROM games WHERE releaseyear IS NOT NULL"), 1, False),
        ("roms with a CRC32",     q1("SELECT COUNT(*) FROM roms WHERE crc IS NOT NULL"),   1, False),
        ("roms with an MD5",      q1("SELECT COUNT(*) FROM roms WHERE md5 IS NOT NULL"),   1, False),
        ("roms with a SHA1",      q1("SELECT COUNT(*) FROM roms WHERE sha1 IS NOT NULL"),  1, False),
        ("roms with a size",      q1("SELECT COUNT(*) FROM roms WHERE size IS NOT NULL"),  1, False),
        ("regions lookup",        q1("SELECT COUNT(*) FROM regions"),       1,     False),
        ("developers lookup",     q1("SELECT COUNT(*) FROM developers"),    1,     False),
    ]

    print("\n--- Verification ---")
    for label, value, minimum, critical in checks:
        ok = value >= minimum
        marker = "OK " if ok else "!! "
        print(f"  {marker}{label:30s} {value}")
        if not ok and critical:
            failures.append(label)

    # Orphan check: games with no ROMs, or ROMs pointing at a missing game
    orphans_g = q1("SELECT COUNT(*) FROM games WHERE id NOT IN (SELECT DISTINCT game_id FROM roms)")
    orphans_r = q1("SELECT COUNT(*) FROM roms WHERE game_id NOT IN (SELECT id FROM games)")
    print(f"  {'OK ' if orphans_g == 0 else '!! '}orphan games (no ROM row)    {orphans_g}")
    print(f"  {'OK ' if orphans_r == 0 else '!! '}orphan roms (no game row)     {orphans_r}")
    if orphans_g or orphans_r:
        failures.append("orphans")

    if failures:
        print(f"\nVerification FAILED: {', '.join(failures)}")
        return 1
    print("\nVerification OK")
    return 0

def get_build_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp.

    Honors SOURCE_DATE_EPOCH for reproducible builds:
    https://reproducible-builds.org/specs/source-date-epoch/
    """
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.isdigit():
        dt = datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    else:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

def write_meta(conn, total_games):
    cur = conn.cursor()
    rom_count = cur.execute("SELECT COUNT(*) FROM roms").fetchone()[0]
    meta = {
        "schema_version":     str(SCHEMA_VERSION),
        "generated_at":       get_build_timestamp(),
        "libretro_database":  LIBRETRO_DB_URL,
        "game_count":         str(total_games),
        "rom_count":          str(rom_count),
    }
    cur.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                    meta.items())
    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="Convert Libretro .rdb files into a SQLite database.",
    )
    p.add_argument("--output", default=DEFAULT_OUTPUT,
                   help=f"Output SQLite file (default: {DEFAULT_OUTPUT})")
    p.add_argument("--thumbnails", action="store_true",
                   help="Add boxart/snap/title/logo URL columns "
                        "(adds ~300 MB to the DB)")
    p.add_argument("--dedup", action="store_true",
                   help="Remove duplicate games that share a CRC32 "
                        "(keeps the first occurrence)")
    p.add_argument("--sample", type=int, default=None, metavar="N",
                   help="Only process the first N entries per system "
                        "(for testing; the resulting DB is incomplete)")
    p.add_argument("--verify", action="store_true",
                   help="Run post-build sanity checks and exit non-zero on failure")
    p.add_argument("--vacuum", action="store_true",
                   help="Compact the database after building "
                        "(slower, but reclaims space freed by --dedup or --thumbnails)")
    return p.parse_args()


def main():
    args = parse_args()

    if not RDB_DIR.is_dir():
        sys.exit(f"Error: {RDB_DIR}/ not found. Run from the project root.")

    if os.path.exists(args.output):
        os.remove(args.output)

    conn = sqlite3.connect(args.output)
    cur = conn.cursor()
    create_schema(cur)
    conn.commit()

    rdb_files = sorted(RDB_DIR.glob("*.rdb"))
    print(f"Found {len(rdb_files)} .rdb files in {RDB_DIR}/")
    if args.sample:
        print(f"Sample mode: max {args.sample} entries per system")

    total = 0
    for rdb in rdb_files:
        platform_name = rdb.stem
        slug = rdb.stem.lower()
        for ch in (" - ", " ", "-", "."):
            slug = slug.replace(ch, "_")
        while "__" in slug:
            slug = slug.replace("__", "_")
        slug = slug.strip("_")

        n = process_rdb(conn, rdb, slug, platform_name, sample=args.sample)
        print(f"{slug}: {n} entries")
        total += n

    conn.commit()
    print(f"\nTotal: {total} games")

    if args.dedup:
        dedup_by_crc(conn)

    if args.thumbnails:
        print("\nPopulating thumbnail URLs...")
        populate_thumbnail_urls(conn)

    write_meta(conn, total)

    if args.vacuum:
        print("\nVacuuming...")
        conn.execute("VACUUM")
        conn.commit()

    exit_code = 0
    if args.verify:
        exit_code = verify(conn)

    conn.close()
    print(f"\nDone -> {args.output}")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()