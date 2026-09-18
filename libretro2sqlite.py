#!/usr/bin/env python3
"""
libretro2sqlite.py

Convert Libretro `.rdb` database files into a single normalized SQLite
database.  Requires `libretrodb_tool` (built from the RetroArch source)
to be present in the same directory.

See README.md for build and usage instructions.

SPDX-License-Identifier: MIT
"""

import contextlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

RDB_DIR   = Path("database")
TOOL      = "./libretrodb_tool.exe"   # use "./libretrodb_tool" on Linux/macOS
OUTPUT_DB = "./libretrodb.sqlite"

# RDB keys that become normalized lookup tables.  The corresponding column
# on `games` is `<key>_id`.
LOOKUP_FIELDS = {
    "developer":      "developers",
    "publisher":      "publishers",
    "genre":          "genres",
    "franchise":      "franchises",
    "region":         "regions",             # comma-split before lookup
    "enhancement_hw": "enhancement_hardware",
}

# RDB keys that belong to the `roms` table, linked via game_id.
ROM_KEYS = {"rom_name", "size", "crc", "md5", "sha1"}

# Game-level columns with explicit SQLite types.  Any other RDB key not
# listed here (and not in LOOKUP_FIELDS/ROM_KEYS) is added as a TEXT column
# on the fly.
GAME_COLUMNS = [
    ("name",         "TEXT"),
    ("description",  "TEXT"),
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

# Manufacturer inferred from the platform name (RDB filename stem).
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


def manufacturer_for(platform_name):
    for prefix, manufacturer in MANUFACTURER_BY_PREFIX.items():
        if platform_name.startswith(prefix):
            return manufacturer
    return None


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
    """Flatten a list-valued RDB field into a comma-joined string."""
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

    cur.execute("CREATE INDEX idx_games_system ON games(system)")
    cur.execute("CREATE INDEX idx_roms_game    ON roms(game_id)")
    cur.execute("CREATE INDEX idx_roms_crc     ON roms(crc)")
    cur.execute("CREATE INDEX idx_roms_md5     ON roms(md5)")


# ---------------------------------------------------------------------------
# Per-RDB processing
# ---------------------------------------------------------------------------

def process_rdb(conn, rdb_path, system_slug, platform_name):
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

    if not entries:
        print(f"  ! No usable entries in {rdb_path.name}")
        return 0

    # Discover extra keys and add them as TEXT columns on `games`.
    known_keys = {n for n, _ in GAME_COLUMNS} | set(LOOKUP_FIELDS) | ROM_KEYS | {"name"}
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
# Main
# ---------------------------------------------------------------------------

def main():
    if not RDB_DIR.is_dir():
        sys.exit(f"Error: {RDB_DIR}/ not found. Run from the project root.")

    if os.path.exists(OUTPUT_DB):
        os.remove(OUTPUT_DB)

    conn = sqlite3.connect(OUTPUT_DB)
    cur = conn.cursor()
    create_schema(cur)
    conn.commit()

    rdb_files = sorted(RDB_DIR.glob("*.rdb"))
    print(f"Found {len(rdb_files)} .rdb files in {RDB_DIR}/")

    total = 0
    for rdb in rdb_files:
        platform_name = rdb.stem
        slug = rdb.stem.lower()
        for ch in (" - ", " ", "-", "."):
            slug = slug.replace(ch, "_")
        while "__" in slug:
            slug = slug.replace("__", "_")
        slug = slug.strip("_")

        n = process_rdb(conn, rdb, slug, platform_name)
        print(f"{slug}: {n} entries")
        total += n

    conn.commit()
    conn.close()
    print(f"\nTotal: {total} games")
    print(f"Done -> {OUTPUT_DB}")


if __name__ == "__main__":
    main()