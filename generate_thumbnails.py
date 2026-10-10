#!/usr/bin/env python3
"""
METANAS — Generate Missing Thumbnails
======================================
Scans the master database for files that don't have thumbnails yet
and generates them (keyframe extraction for videos, resize for images).
No API calls — purely local processing.

Usage:
    python3 generate_thumbnails.py [OPTIONS]

Options:
    --db PATH           Database path (default: ~/.metanas/footage_metadata.db)
    --thumbs PATH       Thumbnails directory (default: from config or ~/.metanas/thumbnails)
    --workers N         Parallel workers (default: 4)
    --dry-run           Show what would be generated without doing it
    --limit N           Only process first N missing files (for testing)
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

METANAS_HOME = Path.home() / ".metanas"

# Image extensions that don't need ffmpeg
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".heic", ".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf"}
VIDEO_EXTS = {".mp4", ".mov", ".mxf", ".avi", ".mkv", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg", ".mts", ".m2ts", ".3gp", ".webm", ".r3d"}


def load_config():
    config_path = METANAS_HOME / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {}


def get_missing_files(db_path, thumb_base):
    """Find all files in the DB that don't have thumbnail folders."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT file_path FROM media_files").fetchall()
    conn.close()

    missing = []
    have_thumbs = 0
    not_found = 0

    for (fp,) in rows:
        if not fp:
            continue
        p = Path(fp)
        stem = p.stem
        thumb_dir = Path(thumb_base) / stem

        if thumb_dir.exists() and any(thumb_dir.glob("*.jpg")):
            have_thumbs += 1
            continue

        if not p.exists():
            not_found += 1
            continue

        missing.append(fp)

    return missing, have_thumbs, not_found


def extract_video_thumbnail(file_path, thumb_dir, num_frames=3):
    """Extract keyframes from a video using ffmpeg."""
    p = Path(file_path)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Get duration
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=30
        )
        duration = float(probe.stdout.strip() or "0")

        if duration <= 0:
            # Fallback: just grab frame at 1 second
            out = thumb_dir / "frame_0000.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", "1", "-i", str(p),
                 "-frames:v", "1", "-q:v", "2", str(out)],
                capture_output=True, timeout=30
            )
            return out.exists()

        # Extract evenly-spaced frames
        count = 0
        for i in range(num_frames):
            t = duration * (i + 1) / (num_frames + 1)
            out = thumb_dir / f"frame_{i:04d}.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(t), "-i", str(p),
                 "-frames:v", "1", "-q:v", "2", str(out)],
                capture_output=True, timeout=30
            )
            if out.exists():
                count += 1

        return count > 0

    except Exception as e:
        return False


def extract_image_thumbnail(file_path, thumb_dir):
    """Create a thumbnail from an image file."""
    p = Path(file_path)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    dest = thumb_dir / "frame_0000.jpg"

    try:
        ext = p.suffix.lower()

        if ext in {".arw", ".cr2", ".cr3", ".nef", ".dng", ".raf"}:
            # RAW files — try to extract embedded preview with exiftool
            if shutil.which("exiftool"):
                with tempfile.TemporaryDirectory() as tmp:
                    preview = Path(tmp) / "preview.jpg"
                    subprocess.run(
                        ["exiftool", "-b", "-PreviewImage", "-w", str(preview), str(p)],
                        capture_output=True, timeout=30
                    )
                    # exiftool writes to a file named after the input
                    candidates = list(Path(tmp).glob("*.jpg"))
                    if candidates:
                        shutil.copy2(str(candidates[0]), str(dest))
                        return True

            # Fallback: try sips (macOS)
            if shutil.which("sips"):
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", str(p), "--out", str(dest)],
                    capture_output=True, timeout=30
                )
                return dest.exists()

        elif ext == ".heic":
            # HEIC — use sips on macOS
            if shutil.which("sips"):
                subprocess.run(
                    ["sips", "-s", "format", "jpeg", str(p), "--out", str(dest)],
                    capture_output=True, timeout=30
                )
                return dest.exists()

        else:
            # Standard image — use ffmpeg to create a JPEG thumbnail
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(p),
                 "-frames:v", "1", "-q:v", "2", str(dest)],
                capture_output=True, timeout=30
            )
            return dest.exists()

    except Exception:
        pass

    return False


def process_file(file_path, thumb_base):
    """Generate thumbnail for a single file."""
    p = Path(file_path)
    ext = p.suffix.lower()
    thumb_dir = Path(thumb_base) / p.stem

    if ext in VIDEO_EXTS:
        ok = extract_video_thumbnail(file_path, thumb_dir)
    elif ext in IMAGE_EXTS:
        ok = extract_image_thumbnail(file_path, thumb_dir)
    else:
        # Try as video (catch-all)
        ok = extract_video_thumbnail(file_path, thumb_dir)

    return file_path, ok


def main():
    parser = argparse.ArgumentParser(
        description="METANAS — Generate missing thumbnails"
    )
    parser.add_argument("--db", type=str, default=None,
                        help="Database path")
    parser.add_argument("--thumbs", type=str, default=None,
                        help="Thumbnails directory")
    parser.add_argument("--workers", type=int, default=4,
                        help="Parallel workers (default: 4)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be generated")
    parser.add_argument("--limit", type=int, default=0,
                        help="Only process first N missing files")
    args = parser.parse_args()

    config = load_config()
    db_path = args.db or config.get("db_path", str(METANAS_HOME / "footage_metadata.db"))
    thumb_base = args.thumbs or config.get("thumbnails_path", str(METANAS_HOME / "thumbnails"))

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   METANAS — Thumbnail Generator          ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    if not Path(db_path).exists():
        print(f"  ✗ Database not found: {db_path}")
        sys.exit(1)

    # Check ffmpeg
    if not shutil.which("ffmpeg"):
        print("  ✗ ffmpeg not found — install it: brew install ffmpeg")
        sys.exit(1)

    print(f"  📂 Database: {db_path}")
    print(f"  🖼  Thumbnails: {thumb_base}")
    print()
    print("  Scanning for missing thumbnails…")

    missing, have_thumbs, not_found = get_missing_files(db_path, thumb_base)

    print(f"  ✓ Already have thumbnails: {have_thumbs:,}")
    print(f"  ⚠ Source file not found:   {not_found:,}")
    print(f"  📋 Missing thumbnails:     {len(missing):,}")

    if not missing:
        print("\n  ✓ All files have thumbnails!")
        sys.exit(0)

    if args.limit > 0:
        missing = missing[:args.limit]
        print(f"  🔢 Limited to first {args.limit} files")

    if args.dry_run:
        print(f"\n  🏁 Dry run — would generate {len(missing):,} thumbnails")
        for fp in missing[:20]:
            print(f"    • {Path(fp).name}")
        if len(missing) > 20:
            print(f"    … and {len(missing) - 20} more")
        sys.exit(0)

    print(f"\n  🚀 Generating thumbnails with {args.workers} workers…")
    print()

    success = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_file, fp, thumb_base): fp for fp in missing}

        for i, future in enumerate(as_completed(futures), 1):
            fp, ok = future.result()
            name = Path(fp).name
            if ok:
                success += 1
                status = "✓"
            else:
                failed += 1
                status = "✗"

            # Progress line
            if i % 50 == 0 or i == len(missing):
                pct = i / len(missing) * 100
                print(f"  [{i:,}/{len(missing):,}] {pct:.0f}% — {success:,} ok, {failed:,} failed")

    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   ✓  Thumbnail generation complete!      ║")
    print("  ╠══════════════════════════════════════════╣")
    print(f"  ║   Generated:  {success:>6,}                    ║")
    print(f"  ║   Failed:     {failed:>6,}                    ║")
    print(f"  ║   Total:      {success + have_thumbs:>6,} / {success + have_thumbs + failed + not_found:,}          ║")
    print("  ╚══════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
