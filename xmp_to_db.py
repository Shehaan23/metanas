#!/usr/bin/env python3
"""
METANAS — Reconstruct DB from XMP Sidecars
============================================
Scans a project folder for .xmp sidecar files written by METANAS,
parses the metadata, and creates a project .db file (and optionally
merges into the main database).

Usage:
    python3 xmp_to_db.py --folder /path/to/project/Footage [OPTIONS]
    python3 xmp_to_db.py --csv ~/Desktop/metanas_projects.csv [OPTIONS]

Options:
    --folder PATH       Single project folder to process
    --csv PATH          Process all xmp_only projects from discovery CSV
    --main-db PATH      Also merge into this main database
    --dry-run           Show what would be created without writing
    --overwrite         Overwrite existing .db files
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

METANAS_HOME = Path.home() / ".metanas"

# Media extensions (must match footage_tagger.py)
VIDEO_EXTS = {
    ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".wmv", ".flv", ".m4v",
    ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".webm", ".r3d"
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
    ".heic", ".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf"
}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

# XMP namespaces used by METANAS
NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "tiff": "http://ns.adobe.com/tiff/1.0/",
    "xmpDM": "http://ns.adobe.com/xmp/1.0/DynamicMedia/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
}


def parse_xmp(xmp_path):
    """Parse a METANAS XMP sidecar and return a metadata dict."""
    try:
        tree = ET.parse(str(xmp_path))
        root = tree.getroot()
    except Exception as e:
        return None

    desc_el = root.find(".//rdf:Description", NS)
    if desc_el is None:
        return None

    metadata = {}

    # dc:description
    desc_text = root.find(".//dc:description/rdf:Alt/rdf:li", NS)
    if desc_text is not None and desc_text.text:
        metadata["description"] = desc_text.text.strip()

    # dc:subject (tags)
    tags = []
    for li in root.findall(".//dc:subject/rdf:Bag/rdf:li", NS):
        if li.text:
            tags.append(li.text.strip())
    if tags:
        metadata["tags"] = tags

    # tiff:Model / xmpDM:cameraModel
    camera = desc_el.find("tiff:Model", NS)
    if camera is not None and camera.text:
        metadata["camera_model"] = camera.text.strip()
    else:
        camera = desc_el.find("xmpDM:cameraModel", NS)
        if camera is not None and camera.text:
            metadata["camera_model"] = camera.text.strip()

    # xmpDM:logComment — structured "FIELD: value | FIELD: value"
    log_el = desc_el.find("xmpDM:logComment", NS)
    if log_el is not None and log_el.text:
        log_comment = log_el.text.strip()
        metadata["log_comment"] = log_comment
        _parse_log_comment(log_comment, metadata)

    # xmp:Rating
    rating_el = desc_el.find("xmp:Rating", NS)
    if rating_el is not None and rating_el.text:
        try:
            metadata["rating"] = int(rating_el.text)
        except ValueError:
            pass

    return metadata


def _parse_log_comment(log_comment, metadata):
    """Parse the structured logComment written by METANAS.
    Format: PERSONS: name | SHOT: wide | MOVEMENT: pan | TIME: morning | ...
    """
    parts = [p.strip() for p in log_comment.split("|")]
    field_map = {
        "PERSONS": "persons",
        "SHOT": "shot_type",
        "MOVEMENT": "camera_movement",
        "TIME": "time_of_day",
        "AUDIO": "audio_type",
        "CAMERA": "camera_model",
        "LIGHTING": "lighting",
        "MOOD": "mood",
        "PALETTE": "color_palette",
    }
    for part in parts:
        match = re.match(r"^([A-Z]+):\s*(.+)$", part)
        if match:
            key = match.group(1)
            value = match.group(2).strip()
            if key in field_map and value:
                db_field = field_map[key]
                if db_field == "persons":
                    metadata[db_field] = value  # comma-separated names
                elif db_field == "camera_model" and "camera_model" in metadata:
                    pass  # already got from tiff:Model
                else:
                    metadata[db_field] = value


def _parse_description_fields(description, metadata):
    """Extract structured fields from the description text.
    METANAS descriptions contain lines like 'Location: ...', 'Mood: ...' etc.
    """
    patterns = {
        "setting": r"Location:\s*(.+?)(?:\.|$)",
        "lighting": r"Lighting:\s*(.+?)(?:\.|$)",
        "camera_movement": r"Camera movement:\s*(.+?)(?:\.|$)",
        "motion": r"Motion detail:\s*(.+?)(?:\.|$)",
        "mood": r"Mood:\s*(.+?)(?:\.|$)",
        "time_of_day": r"Time of day:\s*(.+?)(?:\.|$)",
        "audio_type": r"Audio:\s*(.+?)(?:\.|$)",
        "color_palette": r"Color palette:\s*(.+?)(?:\.|$)",
        "shot_type": r"Shot type:\s*(.+?)(?:\.|$)",
        "transcription": r"Audio transcript:\s*(.+?)$",
    }
    for field, pattern in patterns.items():
        if field not in metadata:  # don't overwrite logComment values
            m = re.search(pattern, description)
            if m:
                metadata[field] = m.group(1).strip()

    # Subjects
    subjects_m = re.search(r"Subjects:\s*(.+?)(?:\.|$)", description)
    if subjects_m and "subjects" not in metadata:
        metadata["subjects"] = subjects_m.group(1).strip()

    # People
    persons_m = re.search(r"People in this clip:\s*(.+?)(?:\.|$)", description)
    if persons_m and "persons" not in metadata:
        metadata["persons"] = persons_m.group(1).strip()


def create_project_db(folder, dry_run=False, overwrite=False):
    """Scan folder for .xmp files and create a project .db from them."""
    folder = Path(folder)
    if not folder.exists():
        print(f"  ⚠ Folder not found: {folder}")
        return None, 0, 0

    # Find all XMP sidecars
    xmp_files = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".xmp"):
                xmp_files.append(Path(root) / f)

    if not xmp_files:
        print(f"  ⚠ No .xmp files found in: {folder}")
        return None, 0, 0

    # Determine DB path — same folder, named after the project
    project_name = folder.parent.name if "footage" in folder.name.lower() else folder.name
    db_name = f"{project_name}_metadata.db"
    db_path = folder / db_name

    if db_path.exists() and not overwrite:
        print(f"  ⚠ DB already exists (use --overwrite): {db_path.name}")
        return db_path, 0, 0

    if dry_run:
        print(f"  📋 Would create: {db_path.name} from {len(xmp_files)} XMP files")
        return db_path, len(xmp_files), 0

    # Create the database
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE IF NOT EXISTS media_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT UNIQUE NOT NULL,
        file_type TEXT, camera_model TEXT, duration REAL, fps REAL,
        description TEXT, shot_type TEXT, subjects TEXT,
        setting TEXT, lighting TEXT, motion TEXT, mood TEXT,
        camera_movement TEXT, time_of_day TEXT, audio_type TEXT,
        color_palette TEXT, mood_tags TEXT,
        tags TEXT, persons TEXT, transcription TEXT,
        vision_provider TEXT, phash TEXT,
        processed_at TEXT DEFAULT (datetime('now'))
    )""")

    success = 0
    failed = 0

    for xmp_path in xmp_files:
        # Find the corresponding media file
        media_path = _find_media_for_xmp(xmp_path)
        if media_path is None:
            failed += 1
            continue

        metadata = parse_xmp(xmp_path)
        if metadata is None:
            failed += 1
            continue

        # Also parse structured fields from the description
        if "description" in metadata:
            _parse_description_fields(metadata["description"], metadata)

        # Determine file type
        ext = media_path.suffix.lower()
        file_type = "video" if ext in VIDEO_EXTS else "image" if ext in IMAGE_EXTS else "unknown"

        # Build tags string (JSON array as stored by METANAS)
        import json
        tags_str = json.dumps(metadata.get("tags", []))
        subjects_str = metadata.get("subjects", "")
        persons_str = metadata.get("persons", "")

        try:
            conn.execute("""INSERT OR REPLACE INTO media_files
                (file_path, file_type, camera_model, description,
                 shot_type, subjects, setting, lighting, motion, mood,
                 camera_movement, time_of_day, audio_type, color_palette,
                 tags, persons, transcription, vision_provider, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (str(media_path), file_type,
                 metadata.get("camera_model", ""),
                 metadata.get("description", ""),
                 metadata.get("shot_type", ""),
                 subjects_str,
                 metadata.get("setting", ""),
                 metadata.get("lighting", ""),
                 metadata.get("motion", ""),
                 metadata.get("mood", ""),
                 metadata.get("camera_movement", ""),
                 metadata.get("time_of_day", ""),
                 metadata.get("audio_type", ""),
                 metadata.get("color_palette", ""),
                 tags_str,
                 persons_str,
                 metadata.get("transcription", ""),
                 "xmp_reconstructed",
                 datetime.now().isoformat()))
            success += 1
        except Exception as e:
            print(f"    ⚠ Error inserting {media_path.name}: {e}")
            failed += 1

    conn.commit()
    conn.close()

    return db_path, success, failed


def _find_media_for_xmp(xmp_path):
    """Find the media file that corresponds to an .xmp sidecar.
    XMP sidecars are named <media_filename>.xmp, e.g. DJI_0001.mp4.xmp
    or DJI_0001.xmp (replacing the extension)."""
    xmp_path = Path(xmp_path)
    stem = xmp_path.stem  # e.g. "DJI_0001.mp4" or "DJI_0001"

    # Case 1: XMP replaces the extension — DJI_0001.xmp → DJI_0001.mp4
    parent = xmp_path.parent
    for ext in sorted(MEDIA_EXTS):
        candidate = parent / f"{stem}{ext}"
        if candidate.exists():
            return candidate

    # Case 2: XMP appended to full filename — DJI_0001.mp4.xmp → DJI_0001.mp4
    if "." in stem:
        candidate = parent / stem
        if candidate.exists() and candidate.suffix.lower() in MEDIA_EXTS:
            return candidate

    return None


def merge_into_main_db(project_db_path, main_db_path):
    """Merge a project DB into the main METANAS database."""
    main_db = Path(main_db_path)
    proj_db = Path(project_db_path)

    if not proj_db.exists():
        return 0

    proj_conn = sqlite3.connect(str(proj_db))
    proj_conn.row_factory = sqlite3.Row
    rows = proj_conn.execute("SELECT * FROM media_files").fetchall()
    if not rows:
        proj_conn.close()
        return 0

    columns = rows[0].keys()
    data_cols = [c for c in columns if c != "id"]
    col_list = ", ".join(data_cols)
    placeholders = ", ".join("?" for _ in data_cols)

    main_conn = sqlite3.connect(str(main_db))
    main_conn.execute("""CREATE TABLE IF NOT EXISTS media_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_path TEXT UNIQUE NOT NULL,
        file_type TEXT, camera_model TEXT, duration REAL, fps REAL,
        description TEXT, shot_type TEXT, subjects TEXT,
        setting TEXT, lighting TEXT, motion TEXT, mood TEXT,
        camera_movement TEXT, time_of_day TEXT, audio_type TEXT,
        color_palette TEXT, mood_tags TEXT,
        tags TEXT, persons TEXT, transcription TEXT,
        vision_provider TEXT, phash TEXT,
        processed_at TEXT DEFAULT (datetime('now'))
    )""")

    merged = 0
    for row in rows:
        vals = [row[c] for c in data_cols]
        try:
            main_conn.execute(
                f"INSERT OR REPLACE INTO media_files ({col_list}) VALUES ({placeholders})",
                vals
            )
            merged += 1
        except Exception:
            pass

    main_conn.commit()

    # Rebuild FTS if it exists
    try:
        main_conn.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")
        main_conn.commit()
    except Exception:
        pass

    main_conn.close()
    proj_conn.close()
    return merged


def process_csv(csv_path, dry_run=False, overwrite=False, main_db=None):
    """Process all xmp_only projects from a discovery CSV."""
    if not Path(csv_path).exists():
        print(f"  ✗ CSV not found: {csv_path}")
        return

    projects = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            status = row.get("status", "").strip()
            source = row.get("tagged_source", "").strip()
            if status == "xmp_only" or source == "xmp":
                projects.append(row)

    if not projects:
        print("  ⚠ No xmp_only projects found in CSV")
        return

    print(f"  📋 Found {len(projects)} XMP-only projects to process")
    print()

    total_success = 0
    total_failed = 0
    total_merged = 0

    for proj in projects:
        folder = proj.get("folder_path", "")
        name = proj.get("project_name", Path(folder).name)
        print(f"  📂 {name}")

        db_path, success, failed = create_project_db(
            folder, dry_run=dry_run, overwrite=overwrite
        )
        total_success += success
        total_failed += failed

        if db_path and success > 0 and main_db and not dry_run:
            merged = merge_into_main_db(db_path, main_db)
            total_merged += merged
            print(f"    ✓ Created {db_path.name}: {success} files, {merged} merged to main DB")
        elif db_path and success > 0:
            print(f"    ✓ Created {db_path.name}: {success} files")
        elif dry_run and db_path:
            pass  # already printed in create_project_db
        print()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   ✓  XMP → DB reconstruction complete!   ║")
    print("  ╠══════════════════════════════════════════╣")
    print(f"  ║   Projects processed: {len(projects):>4}              ║")
    print(f"  ║   Files recovered:  {total_success:>6,}              ║")
    print(f"  ║   Failed:           {total_failed:>6,}              ║")
    if main_db:
        print(f"  ║   Merged to main:   {total_merged:>6,}              ║")
    print("  ╚══════════════════════════════════════════╝")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="METANAS — Reconstruct DB from XMP sidecars"
    )
    parser.add_argument("--folder", type=str, default=None,
                        help="Single project folder to process")
    parser.add_argument("--csv", type=str, default=None,
                        help="Process xmp_only projects from discovery CSV")
    parser.add_argument("--main-db", type=str, default=None,
                        help="Also merge results into this main database")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be done")
    parser.add_argument("--overwrite", action="store_true",
                        help="Overwrite existing .db files")
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   METANAS — XMP → DB Reconstruction      ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    if not args.folder and not args.csv:
        print("  ✗ Provide either --folder or --csv")
        print("  Usage:")
        print("    python3 xmp_to_db.py --folder /path/to/Footage")
        print("    python3 xmp_to_db.py --csv ~/Desktop/metanas_projects.csv")
        sys.exit(1)

    if args.csv:
        process_csv(args.csv, dry_run=args.dry_run,
                    overwrite=args.overwrite, main_db=args.main_db)
    elif args.folder:
        db_path, success, failed = create_project_db(
            args.folder, dry_run=args.dry_run, overwrite=args.overwrite
        )
        if db_path and success > 0:
            print(f"\n  ✓ Created: {db_path}")
            print(f"  ✓ Files recovered: {success}, Failed: {failed}")

            if args.main_db and not args.dry_run:
                merged = merge_into_main_db(db_path, args.main_db)
                print(f"  ✓ Merged {merged} rows into main database")
        print()


if __name__ == "__main__":
    main()
