#!/usr/bin/env python3
"""
METANAS — Project Discovery
============================
Scans NAS volumes for Footage folders, identifies which are tagged
and which are not, and outputs a CSV for the user to fill in
project-specific tags before bulk tagging.

Usage:
    python3 discover_projects.py [OPTIONS]

Options:
    --scan-dirs DIR [DIR]   Directories to scan (default: see below)
    --output PATH           Output CSV path (default: ~/Desktop/metanas_projects.csv)
    --update PATH           Path to existing CSV — merges new projects in
                            without overwriting your edits
"""

import argparse
import csv
import os
import sqlite3
import sys
from pathlib import Path

# ── Media file extensions ────────────────────────────────────────────────────
VIDEO_EXTS = {
    ".mp4", ".mov", ".mxf", ".avi", ".mkv", ".wmv", ".flv", ".m4v",
    ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".webm", ".r3d"
}
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp",
    ".heic", ".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf"
}
MEDIA_EXTS = VIDEO_EXTS | IMAGE_EXTS

# ── Words in folder names that indicate non-taggable content ─────────────────
SKIP_KEYWORDS = {"stock", "render", "renders", "export", "exports",
                 "final", "delivery", "deliverables", "archive_old"}

# CSV columns
CSV_COLUMNS = [
    "project_name",
    "folder_path",
    "status",           # tagged / untagged / partial / xmp_only
    "tagged_source",    # db / xmp / none — how tagging was detected
    "total_files",
    "video_files",
    "image_files",
    "tagged_count",     # rows in DB or xmp sidecar count
    "custom_tags",      # comma-separated tags for this project
    "location",         # where the shoot took place
    "hotel_or_venue",   # hotel / resort / venue name
    "client",           # client name
    "year",             # production year
    "notes",            # anything else
    "tag_this",         # YES / NO / SKIP — user marks which to tag
]


def count_media_files(folder):
    """Count video and image files in a folder (recursive)."""
    videos = 0
    images = 0
    for root, dirs, files in os.walk(folder):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXTS:
                videos += 1
            elif ext in IMAGE_EXTS:
                images += 1
    return videos, images


def count_xmp_sidecars(folder):
    """Count .xmp sidecar files in a folder (recursive). Their presence means
    the media file was tagged by METANAS even if no .db was saved."""
    count = 0
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".xmp"):
                count += 1
    return count


def find_project_db(folder):
    """Check if a folder has a METANAS .db file and return row count.
    Falls back to counting .xmp sidecar files as evidence of tagging."""
    folder = Path(folder)

    # First: look for a proper .db file
    for db_file in folder.glob("*.db"):
        try:
            conn = sqlite3.connect(str(db_file))
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='media_files'"
            )
            if cursor.fetchone():
                count = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
                conn.close()
                return db_file.name, count, "db"
            conn.close()
        except Exception:
            pass

    # Second: check for .xmp sidecar files (tagged but no .db saved)
    xmp_count = count_xmp_sidecars(folder)
    if xmp_count > 0:
        return None, xmp_count, "xmp"

    return None, 0, None


def should_skip(folder_name):
    """Check if a folder name indicates non-taggable content."""
    lower = folder_name.lower()
    for keyword in SKIP_KEYWORDS:
        if keyword in lower:
            return True
    return False


def is_footage_folder(path):
    """Check if this is a Footage folder (has 'footage' in the name)."""
    return "footage" in path.name.lower()


def extract_year(path_str):
    """Try to extract a year from the folder path."""
    import re
    # Look for 4-digit year in path
    years = re.findall(r'20[12]\d', path_str)
    if years:
        return years[0]
    return ""


def extract_project_name(folder_path):
    """Extract a human-readable project name from the folder path."""
    p = Path(folder_path)
    # Walk up to find the parent project folder
    # e.g., .../051526a_Sigiriya_reel/051526a_Sigiriya_reel_Footage → 051526a_Sigiriya_reel
    if "footage" in p.name.lower():
        # Parent is likely the project folder
        return p.parent.name
    return p.name


def discover_projects(scan_dirs):
    """Scan directories for Footage folders."""
    projects = []
    seen_paths = set()

    for scan_dir in scan_dirs:
        scan_dir = Path(scan_dir)
        if not scan_dir.exists():
            print(f"  ⚠ Directory not found: {scan_dir}")
            continue

        print(f"  📂 Scanning: {scan_dir}")

        for root, dirs, files in os.walk(scan_dir):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]

            root_path = Path(root)

            # Only consider folders with "Footage" in the name
            if not is_footage_folder(root_path):
                continue

            # Skip stock/render folders
            if should_skip(root_path.name):
                continue

            # Avoid duplicates
            real_path = str(root_path.resolve())
            if real_path in seen_paths:
                continue
            seen_paths.add(real_path)

            # Don't recurse into sub-footage folders — this one is the project
            # Remove Footage subdirs from further traversal
            dirs[:] = [d for d in dirs if "footage" not in d.lower()]

            # Count media files
            videos, images = count_media_files(root_path)
            total = videos + images

            if total == 0:
                continue  # Skip empty folders

            # Check if already tagged (DB first, then XMP sidecars)
            db_name, tagged_count, source = find_project_db(root_path)
            if source == "xmp":
                status = "xmp_only"  # tagged via XMP but no .db saved
            elif tagged_count > 0:
                if tagged_count >= total * 0.9:  # 90%+ tagged
                    status = "tagged"
                else:
                    status = "partial"
            else:
                status = "untagged"

            project_name = extract_project_name(root_path)
            year = extract_year(str(root_path))

            projects.append({
                "project_name": project_name,
                "folder_path": str(root_path),
                "status": status,
                "tagged_source": source or "none",
                "total_files": total,
                "video_files": videos,
                "image_files": images,
                "tagged_count": tagged_count,
                "custom_tags": "",
                "location": "",
                "hotel_or_venue": "",
                "client": "",
                "year": year,
                "notes": "",
                "tag_this": "YES" if status == "untagged" else "NO",
            })

    return projects


def load_existing_csv(csv_path):
    """Load an existing CSV to preserve user edits."""
    existing = {}
    if not Path(csv_path).exists():
        return existing
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            existing[row.get("folder_path", "")] = row
    return existing


def write_csv(projects, output_path, existing=None):
    """Write projects to CSV, merging with existing data if provided."""
    existing = existing or {}

    # Merge: preserve user edits for known projects
    merged = []
    for proj in projects:
        path = proj["folder_path"]
        if path in existing:
            old = existing[path]
            # Update counts (may have changed) but keep user edits
            old["total_files"] = proj["total_files"]
            old["video_files"] = proj["video_files"]
            old["image_files"] = proj["image_files"]
            old["tagged_count"] = proj["tagged_count"]
            old["status"] = proj["status"]
            merged.append(old)
        else:
            merged.append(proj)

    # Sort: untagged first, then partial, then tagged
    status_order = {"untagged": 0, "partial": 1, "xmp_only": 2, "tagged": 3}
    merged.sort(key=lambda p: (status_order.get(p.get("status", ""), 3), p.get("project_name", "")))

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged)

    return merged


def main():
    parser = argparse.ArgumentParser(
        description="METANAS — Discover projects for bulk tagging"
    )
    parser.add_argument("--scan-dirs", nargs="*", default=[
        "/Volumes/NAS-SHR-9TB",
        "/Volumes/Assort2025/Sheneller Projects",
    ], help="Directories to scan")
    parser.add_argument("--output", type=str,
                        default=str(Path.home() / "Desktop" / "metanas_projects.csv"),
                        help="Output CSV path")
    parser.add_argument("--update", type=str, default=None,
                        help="Existing CSV to merge with (preserves your edits)")
    args = parser.parse_args()

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   METANAS — Project Discovery            ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    # If updating, load existing CSV
    existing = {}
    if args.update:
        existing = load_existing_csv(args.update)
        print(f"  📋 Loaded {len(existing)} existing projects from CSV")
    elif Path(args.output).exists():
        # Auto-detect existing CSV at output path
        existing = load_existing_csv(args.output)
        if existing:
            print(f"  📋 Found existing CSV with {len(existing)} projects — will preserve your edits")

    projects = discover_projects(args.scan_dirs)

    if not projects:
        print("\n  ✗ No Footage folders found.")
        sys.exit(1)

    # Write CSV
    merged = write_csv(projects, args.output, existing)

    # Stats
    untagged = sum(1 for p in merged if p.get("status") == "untagged")
    partial = sum(1 for p in merged if p.get("status") == "partial")
    xmp_only = sum(1 for p in merged if p.get("status") == "xmp_only")
    tagged = sum(1 for p in merged if p.get("status") == "tagged")
    total_files = sum(int(p.get("total_files", 0)) for p in merged)
    untagged_files = sum(int(p.get("total_files", 0)) for p in merged if p.get("status") == "untagged")

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   ✓  Discovery complete!                 ║")
    print("  ╠══════════════════════════════════════════╣")
    print(f"  ║   Total projects:    {len(merged):>4}                ║")
    print(f"  ║   Tagged (with DB):  {tagged:>4}                ║")
    print(f"  ║   Tagged (XMP only): {xmp_only:>4}  ← needs DB   ║")
    print(f"  ║   Partially tagged:  {partial:>4}                ║")
    print(f"  ║   Untagged:          {untagged:>4}                ║")
    print(f"  ║   Total files:    {total_files:>7,}                ║")
    print(f"  ║   Files to tag:   {untagged_files:>7,}                ║")
    print("  ╚══════════════════════════════════════════╝")
    print()
    print(f"  📄 CSV saved: {args.output}")
    print()
    print("  Next steps:")
    print("  1. Open the CSV in Excel / Numbers / Google Sheets")
    print("  2. Fill in custom_tags, location, hotel_or_venue, client for each project")
    print("  3. Set tag_this to YES / NO / SKIP for each project")
    print("  4. Save the CSV")
    print("  5. Run:  python3 batch_tag.py --csv <path_to_csv>")
    print()


if __name__ == "__main__":
    main()
