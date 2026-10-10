#!/usr/bin/env python3
"""
METANAS — Database Migration & Merge Tool
==========================================
Merges all METANAS databases (main archive + project-specific DBs) into
one master database.

Usage:
    python3 migrate_databases.py [OPTIONS]

Options:
    --main-db PATH        Path to the main footage_metadata.db to use as base
                          (default: ~/.metanas/footage_metadata.db)
    --scan-dirs DIR [DIR] Extra directories to scan for project .db files
                          (always scans ~/.metanas/project_dbs/ automatically)
    --nas-path PATH       NAS mount path to recursively scan for project DBs
    --output PATH         Where to write the merged database
                          (default: ~/.metanas/footage_metadata.db)
    --dry-run             Show what would be merged without writing anything
    --backup              Create a backup of the output DB before merging

Example — merge MacBook Pro DBs into Mac Mini master:
    # First, copy from MacBook Pro to Mac Mini (see README):
    #   scp macbookpro:~/.metanas/footage_metadata.db /tmp/macbook_main.db
    #   rsync -av macbookpro:~/.metanas/thumbnails/ ~/.metanas/thumbnails/
    #
    # Then merge:
    python3 migrate_databases.py \\
        --main-db /tmp/macbook_main.db \\
        --nas-path /Volumes/YourNAS \\
        --output ~/.metanas/footage_metadata.db
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


METANAS_HOME = Path.home() / ".metanas"

# The canonical table schema (v14.3.4+)
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS media_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE NOT NULL,
    file_type TEXT,
    camera_model TEXT,
    duration REAL,
    fps REAL,
    description TEXT,
    shot_type TEXT,
    subjects TEXT,
    setting TEXT,
    lighting TEXT,
    motion TEXT,
    mood TEXT,
    camera_movement TEXT,
    time_of_day TEXT,
    audio_type TEXT,
    color_palette TEXT,
    mood_tags TEXT,
    tags TEXT,
    persons TEXT,
    transcription TEXT,
    vision_provider TEXT,
    phash TEXT,
    processed_at TEXT DEFAULT (datetime('now'))
)
"""

CREATE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
    description, tags, persons, shot_type, setting,
    lighting, mood, camera_movement, time_of_day,
    audio_type, color_palette, mood_tags, transcription,
    content='media_files', content_rowid='id'
)
"""

# Columns to copy (everything except auto-generated 'id')
DATA_COLS = [
    "file_path", "file_type", "camera_model", "duration", "fps",
    "description", "shot_type", "subjects", "setting", "lighting",
    "motion", "mood", "camera_movement", "time_of_day", "audio_type",
    "color_palette", "mood_tags", "tags", "persons", "transcription",
    "vision_provider", "phash", "processed_at"
]


def find_project_dbs(scan_dirs, nas_path=None):
    """Find all .db files that look like METANAS project databases."""
    db_files = []
    all_dirs = list(scan_dirs)

    if nas_path:
        nas = Path(nas_path)
        if nas.exists():
            all_dirs.append(nas)
        else:
            print(f"  ⚠ NAS path not found: {nas_path}")

    for d in all_dirs:
        d = Path(d)
        if not d.exists():
            continue
        # Recursively find .db files
        for db_file in d.rglob("*.db"):
            # Skip the main database itself
            if db_file.name == "footage_metadata.db":
                continue
            # Skip macOS metadata
            if "._" in db_file.name or ".DS_Store" in str(db_file):
                continue
            # Verify it's actually a METANAS database (has media_files table)
            try:
                conn = sqlite3.connect(str(db_file))
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='media_files'"
                )
                if cursor.fetchone():
                    count = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
                    db_files.append((str(db_file), count))
                conn.close()
            except Exception:
                pass  # Not a valid SQLite DB or not a METANAS DB

    return db_files


def get_db_columns(db_path):
    """Get the column names from a database's media_files table."""
    conn = sqlite3.connect(db_path)
    cursor = conn.execute("PRAGMA table_info(media_files)")
    cols = [row[1] for row in cursor.fetchall()]
    conn.close()
    return cols


def merge_db(source_path, dest_conn, stats):
    """Merge rows from source database into destination."""
    try:
        src_conn = sqlite3.connect(source_path)
        src_conn.row_factory = sqlite3.Row
    except Exception as e:
        print(f"  ✗ Could not open {source_path}: {e}")
        return

    try:
        # Get columns that exist in BOTH source and destination
        src_cols = get_db_columns(source_path)
        common_cols = [c for c in DATA_COLS if c in src_cols]

        if "file_path" not in common_cols:
            print(f"  ✗ {source_path}: no file_path column — skipping")
            src_conn.close()
            return

        rows = src_conn.execute(f"SELECT * FROM media_files").fetchall()
        src_col_names = [desc[0] for desc in src_conn.execute("SELECT * FROM media_files LIMIT 1").description]

        col_list = ", ".join(common_cols)
        placeholders = ", ".join("?" for _ in common_cols)

        inserted = 0
        updated = 0
        skipped = 0

        for row in rows:
            # Build values from available columns
            vals = []
            for c in common_cols:
                if c in src_col_names:
                    vals.append(row[c])
                else:
                    vals.append(None)

            file_path = vals[common_cols.index("file_path")]

            # Check if this file_path already exists in dest
            existing = dest_conn.execute(
                "SELECT id, processed_at FROM media_files WHERE file_path = ?",
                (file_path,)
            ).fetchone()

            if existing:
                # Update if source has newer processed_at
                src_time = vals[common_cols.index("processed_at")] if "processed_at" in common_cols else None
                dest_time = existing[1]

                if src_time and dest_time and src_time > dest_time:
                    # Source is newer — update
                    update_cols = [c for c in common_cols if c != "file_path"]
                    update_vals = [vals[common_cols.index(c)] for c in update_cols]
                    set_clause = ", ".join(f"{c} = ?" for c in update_cols)
                    dest_conn.execute(
                        f"UPDATE media_files SET {set_clause} WHERE file_path = ?",
                        update_vals + [file_path]
                    )
                    updated += 1
                else:
                    skipped += 1
            else:
                # New row — insert
                try:
                    dest_conn.execute(
                        f"INSERT INTO media_files ({col_list}) VALUES ({placeholders})",
                        vals
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    skipped += 1
                except Exception as e:
                    print(f"    ⚠ Row error: {e}")
                    skipped += 1

        dest_conn.commit()
        stats["inserted"] += inserted
        stats["updated"] += updated
        stats["skipped"] += skipped
        print(f"    ✓ {inserted} new, {updated} updated, {skipped} unchanged")

    except Exception as e:
        print(f"  ✗ Error merging {source_path}: {e}")
    finally:
        src_conn.close()


def rebuild_fts(conn):
    """Rebuild the FTS index from scratch."""
    print("  🔍 Rebuilding full-text search index…")
    try:
        # Drop existing FTS table
        conn.execute("DROP TABLE IF EXISTS media_fts")
        conn.execute(CREATE_FTS)

        # Populate FTS from media_files
        conn.execute("""
            INSERT INTO media_fts(rowid, description, tags, persons, shot_type,
                setting, lighting, mood, camera_movement, time_of_day,
                audio_type, color_palette, mood_tags, transcription)
            SELECT id, description, tags, persons, shot_type,
                setting, lighting, mood, camera_movement, time_of_day,
                audio_type, color_palette, mood_tags, transcription
            FROM media_files
        """)
        conn.commit()
        print("  ✓ FTS index rebuilt")
    except Exception as e:
        print(f"  ⚠ FTS rebuild warning: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="METANAS — Merge all databases into one master DB"
    )
    parser.add_argument("--main-db", type=str, default=None,
                        help="Path to the main footage_metadata.db (source)")
    parser.add_argument("--scan-dirs", nargs="*", default=[],
                        help="Extra directories to scan for project .db files")
    parser.add_argument("--nas-path", type=str, default=None,
                        help="NAS mount path to recursively scan for project DBs")
    parser.add_argument("--output", type=str,
                        default=str(METANAS_HOME / "footage_metadata.db"),
                        help="Where to write the merged database")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be merged without writing")
    parser.add_argument("--backup", action="store_true",
                        help="Backup the output DB before merging")
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   METANAS — Database Merge Tool          ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    output_path = Path(args.output).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # ── 1. Discover all source databases ─────────────────────────────
    print("  📂 Scanning for databases…")
    sources = []

    # Main DB (from MacBook Pro or wherever)
    if args.main_db:
        main_db = Path(args.main_db).expanduser()
        if main_db.exists():
            try:
                conn = sqlite3.connect(str(main_db))
                count = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
                conn.close()
                sources.append((str(main_db), count))
                print(f"  ✓ Main DB: {main_db} ({count:,} files)")
            except Exception as e:
                print(f"  ✗ Main DB error: {e}")
        else:
            print(f"  ✗ Main DB not found: {main_db}")

    # Default project_dbs folder
    scan_dirs = [METANAS_HOME / "project_dbs"]
    for extra in args.scan_dirs:
        scan_dirs.append(Path(extra).expanduser())

    # Also check config for known project DB folders
    config_path = METANAS_HOME / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                cfg = json.load(f)
            for folder in cfg.get("project_db_folders", []):
                scan_dirs.append(Path(folder))
        except Exception:
            pass

    project_dbs = find_project_dbs(scan_dirs, args.nas_path)
    for db_path, count in project_dbs:
        print(f"  ✓ Project DB: {db_path} ({count:,} files)")

    sources.extend(project_dbs)

    # Also check if the output DB already exists (merging INTO it)
    existing_count = 0
    if output_path.exists() and str(output_path) not in [s[0] for s in sources]:
        try:
            conn = sqlite3.connect(str(output_path))
            existing_count = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
            conn.close()
            print(f"  ✓ Existing output DB: {output_path} ({existing_count:,} files)")
        except Exception:
            pass

    if not sources:
        print()
        print("  ✗ No source databases found. Use --main-db or --nas-path.")
        print()
        sys.exit(1)

    total_source = sum(count for _, count in sources) + existing_count
    print(f"\n  Total source files across all databases: {total_source:,}")

    if args.dry_run:
        print("\n  🏁 Dry run complete — no changes made.")
        print()
        sys.exit(0)

    # ── 2. Backup if requested ───────────────────────────────────────
    if args.backup and output_path.exists():
        backup = output_path.with_suffix(f".backup_{int(time.time())}.db")
        shutil.copy2(str(output_path), str(backup))
        print(f"\n  💾 Backup saved: {backup}")

    # ── 3. Create/open the output database ───────────────────────────
    print(f"\n  📝 Output database: {output_path}")
    dest_conn = sqlite3.connect(str(output_path), timeout=30)
    dest_conn.execute("PRAGMA journal_mode=WAL")
    dest_conn.execute("PRAGMA busy_timeout=10000")
    dest_conn.execute(CREATE_TABLE)
    dest_conn.commit()

    # ── 4. Merge each source ─────────────────────────────────────────
    stats = {"inserted": 0, "updated": 0, "skipped": 0}

    for db_path, count in sources:
        # Skip if source IS the output
        if Path(db_path).resolve() == output_path.resolve():
            print(f"\n  ⏭ Skipping (same as output): {db_path}")
            continue
        print(f"\n  📥 Merging: {Path(db_path).name} ({count:,} files)")
        merge_db(db_path, dest_conn, stats)

    # ── 5. Rebuild FTS index ─────────────────────────────────────────
    rebuild_fts(dest_conn)

    # ── 6. Final count ───────────────────────────────────────────────
    final_count = dest_conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
    dest_conn.close()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   ✓  Merge complete!                     ║")
    print("  ╠══════════════════════════════════════════╣")
    print(f"  ║   New rows:      {stats['inserted']:>6,}                 ║")
    print(f"  ║   Updated:       {stats['updated']:>6,}                 ║")
    print(f"  ║   Unchanged:     {stats['skipped']:>6,}                 ║")
    print(f"  ║   Total in DB:   {final_count:>6,}                 ║")
    print("  ╚══════════════════════════════════════════╝")
    print(f"\n  Master DB: {output_path}")
    print()


if __name__ == "__main__":
    main()
