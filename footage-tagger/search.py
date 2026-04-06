#!/usr/bin/env python3
"""
Sheneller Ventures — Footage Search Tool
Searches the metadata database built by footage_tagger.py.

Usage:
  python3 search.py "Shenelle outdoor"
  python3 search.py "golden hour beach"
  python3 search.py "drone aerial lake"
  python3 search.py "Shenelle" --person
  python3 search.py "Sony A7S III" --camera
  python3 search.py --recent 20
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path


DB_PATH = "/Users/Shehaan/Desktop/VideoTagger/footage_metadata.db"

DIVIDER = "─" * 80


def search_metadata(db_path: str, query: str, limit: int = 20) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT
                mf.file_path,
                mf.description,
                mf.camera_model,
                mf.shot_type,
                mf.mood,
                mf.persons,
                mf.tags,
                mf.transcription,
                mf.vision_provider,
                mf.processed_at,
                snippet(media_fts, 1, '[', ']', '...', 20) AS snippet
            FROM media_fts
            JOIN media_files mf ON mf.id = media_fts.rowid
            WHERE media_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"Search error: {e}")
        return []
    finally:
        conn.close()


def search_by_person(db_path: str, name: str, limit: int = 50) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT file_path, description, camera_model, shot_type, persons, processed_at
            FROM media_files
            WHERE LOWER(persons) LIKE ?
            ORDER BY processed_at DESC
            LIMIT ?
        """, (f"%{name.lower()}%", limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_by_camera(db_path: str, camera: str, limit: int = 50) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT file_path, description, camera_model, shot_type, processed_at
            FROM media_files
            WHERE LOWER(camera_model) LIKE ?
            ORDER BY processed_at DESC
            LIMIT ?
        """, (f"%{camera.lower()}%", limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_recent(db_path: str, limit: int = 20) -> list:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT file_path, description, camera_model, shot_type, persons, processed_at
            FROM media_files
            ORDER BY processed_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
        videos = conn.execute("SELECT COUNT(*) FROM media_files WHERE file_type='video'").fetchone()[0]
        images = conn.execute("SELECT COUNT(*) FROM media_files WHERE file_type='image'").fetchone()[0]
        cameras = conn.execute(
            "SELECT camera_model, COUNT(*) as n FROM media_files GROUP BY camera_model ORDER BY n DESC"
        ).fetchall()
        persons_rows = conn.execute(
            "SELECT persons FROM media_files WHERE persons != '[]' AND persons != '' AND persons IS NOT NULL"
        ).fetchall()
        all_persons = []
        for row in persons_rows:
            try:
                all_persons.extend(json.loads(row[0]))
            except Exception:
                pass
        person_counts = {}
        for p in all_persons:
            person_counts[p] = person_counts.get(p, 0) + 1
        return {
            "total": total, "videos": videos, "images": images,
            "cameras": [(r[0], r[1]) for r in cameras],
            "persons": sorted(person_counts.items(), key=lambda x: -x[1]),
        }
    finally:
        conn.close()


def print_result(r: dict, index: int):
    path = Path(r["file_path"])
    print(f"\n  {index}. {path.name}")
    print(f"     📁 {path.parent}")
    if r.get("camera_model") and r["camera_model"] != "Unknown":
        print(f"     📷 {r['camera_model']}  |  Shot: {r.get('shot_type','?')}  |  Mood: {r.get('mood','?')}")
    persons = r.get("persons", "[]")
    try:
        persons_list = json.loads(persons) if isinstance(persons, str) else persons
        if persons_list:
            print(f"     👤 {', '.join(persons_list)}")
    except Exception:
        pass
    desc = r.get("description") or r.get("snippet", "")
    if desc:
        # Show first 200 chars of description
        short = desc[:200].replace("\n", " ")
        if len(desc) > 200:
            short += "…"
        print(f"     💬 {short}")


def main():
    parser = argparse.ArgumentParser(
        description="Search Sheneller Ventures footage metadata database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 search.py "Shenelle outdoor golden hour"
  python3 search.py "drone aerial lake mountain"
  python3 search.py "interview close-up"
  python3 search.py "Shenelle" --person
  python3 search.py "Sony A7S III" --camera
  python3 search.py --recent 20
  python3 search.py --stats
        """
    )
    parser.add_argument("query", nargs="?", default=None, help="Search terms")
    parser.add_argument("--person", action="store_true", help="Search by person name")
    parser.add_argument("--camera", action="store_true", help="Search by camera model")
    parser.add_argument("--recent", type=int, metavar="N", help="Show N most recently tagged files")
    parser.add_argument("--stats", action="store_true", help="Show database statistics")
    parser.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    parser.add_argument("--db", default=DB_PATH, help="Path to database file")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"❌  Database not found: {args.db}")
        print("    Run footage_tagger.py first to build the database.")
        sys.exit(1)

    # ── Stats ──────────────────────────────────────────────────────────────────
    if args.stats:
        stats = get_stats(args.db)
        print(f"\n{'═'*50}")
        print(f"  SHENELLER VENTURES — FOOTAGE DATABASE")
        print(f"{'═'*50}")
        print(f"  Total files indexed : {stats['total']:,}")
        print(f"  Videos              : {stats['videos']:,}")
        print(f"  Images              : {stats['images']:,}")
        if stats["cameras"]:
            print(f"\n  Cameras:")
            for cam, count in stats["cameras"]:
                print(f"    {cam or 'Unknown':25s}  {count:,} files")
        if stats["persons"]:
            print(f"\n  Identified persons:")
            for name, count in stats["persons"]:
                print(f"    {name:25s}  {count:,} clips")
        print(f"{'═'*50}\n")
        return

    # ── Recent ────────────────────────────────────────────────────────────────
    if args.recent:
        results = get_recent(args.db, args.recent)
        print(f"\n{DIVIDER}")
        print(f"  {len(results)} most recently tagged files")
        print(DIVIDER)
        for i, r in enumerate(results, 1):
            print_result(r, i)
        print(f"\n{DIVIDER}\n")
        return

    # ── Require query for other modes ─────────────────────────────────────────
    if not args.query:
        parser.print_help()
        sys.exit(0)

    # ── Person search ─────────────────────────────────────────────────────────
    if args.person:
        results = search_by_person(args.db, args.query, args.limit)
        print(f"\n{DIVIDER}")
        print(f"  Person search: '{args.query}'  —  {len(results)} result(s)")
        print(DIVIDER)
        for i, r in enumerate(results, 1):
            print_result(r, i)
        print(f"\n{DIVIDER}\n")
        return

    # ── Camera search ─────────────────────────────────────────────────────────
    if args.camera:
        results = search_by_camera(args.db, args.query, args.limit)
        print(f"\n{DIVIDER}")
        print(f"  Camera search: '{args.query}'  —  {len(results)} result(s)")
        print(DIVIDER)
        for i, r in enumerate(results, 1):
            print_result(r, i)
        print(f"\n{DIVIDER}\n")
        return

    # ── Full-text search ──────────────────────────────────────────────────────
    results = search_metadata(args.db, args.query, args.limit)
    print(f"\n{DIVIDER}")
    print(f"  Search: '{args.query}'  —  {len(results)} result(s)")
    print(DIVIDER)
    if not results:
        print("\n  No results found. Try different keywords.")
    else:
        for i, r in enumerate(results, 1):
            print_result(r, i)
    print(f"\n{DIVIDER}\n")


if __name__ == "__main__":
    main()
