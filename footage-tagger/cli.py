#!/usr/bin/env python3
"""
METANAS CLI — headless ingest and index commands.

Usage:
    metanas ingest <footage_folder> [--out <brief_folder>] [--resume] [--cameras A7S3,ZVE1] [--workers N] [--quiet] [--dry-run]
    metanas index  <footage_folder> [--out <brief_folder>]
    metanas --version
"""

import argparse
import concurrent.futures
import json
import logging
import math
import os
import re
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ── Resolve paths ────────────────────────────────────────────────────────────
METANAS_HOME = Path.home() / ".metanas"
METANAS_HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = METANAS_HOME / "config.yaml"

# Import the engine — same module the GUI uses
_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
import footage_tagger as engine

# ── Version ──────────────────────────────────────────────────────────────────
_VERSION_FILE = _THIS_DIR / "version.json"
try:
    APP_VERSION = json.loads(_VERSION_FILE.read_text()).get("version", "unknown")
except Exception:
    APP_VERSION = "unknown"

# ── WhisperX (word-level timestamps + speaker diarization) ───────────────────
try:
    import whisperx as _whisperx
    WHISPERX_AVAILABLE = True
except ImportError:
    WHISPERX_AVAILABLE = False
    _whisperx = None

# ── Logging ──────────────────────────────────────────────────────────────────
log = logging.getLogger("metanas.cli")

# ── Constants ────────────────────────────────────────────────────────────────
# SOP folder pattern: _Footage_<Camera>_dayN
_SOP_CAMERA_RE = re.compile(
    r"_Footage_(?P<camera>[A-Za-z0-9]+)_day(?P<day>\d+)", re.IGNORECASE
)

CAMERA_ALIASES = {
    "a7s3": "A7S3", "A7S3": "A7S3",
    "zve1": "ZVE1", "ZVE1": "ZVE1",
    "act5": "ACT5", "ACT5": "ACT5",
    "drone": "Drone", "Drone": "Drone",
    "i360": "I360", "I360": "I360",
    "iphone": "Iphone", "Iphone": "Iphone", "iPhone": "Iphone",
}

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mxf"}

# ── xxhash64 ─────────────────────────────────────────────────────────────────
try:
    import xxhash
    def file_hash_xxh64(path: Path) -> str:
        h = xxhash.xxh64()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
except ImportError:
    # Pure-Python fallback using hashlib (slower but no extra dep)
    import hashlib
    def file_hash_xxh64(path: Path) -> str:
        h = hashlib.blake2b(digest_size=8)
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    log.warning("xxhash not installed — using blake2b as hash fallback. "
                "Install xxhash for spec-compliant hashing: pip install xxhash")


# ── WhisperX transcription ────────────────────────────────────────────────────

def transcribe_whisperx(file_path: Path, config: dict, duration: float
                        ) -> tuple[str, list[dict]]:
    """Transcribe with WhisperX: word-level timestamps + speaker labels.

    Returns (plain_text, word_list) where word_list is:
        [{"start": float, "end": float, "word": str, "speaker": str}, ...]

    Falls back to the engine's faster_whisper if WhisperX is unavailable,
    returning a single-entry word list covering the whole clip.
    """
    if not WHISPERX_AVAILABLE:
        # Fallback: use the existing engine transcription (plain text only)
        text = engine.transcribe_audio(file_path, config)
        if text:
            words = [{"start": 0.0, "end": duration,
                       "word": text, "speaker": "unknown"}]
            return text, words
        return "", []

    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        whisper_model = config.get("whisper_model", "medium")
        batch_size = 8 if device == "cuda" else 4

        # 1. Load model and transcribe
        model = _whisperx.load_model(whisper_model, device,
                                      compute_type=compute_type)

        # Extract audio to WAV first (same approach as the engine)
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "audio.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(file_path),
                 "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                 str(wav_path)],
                capture_output=True, timeout=300
            )
            if not wav_path.exists():
                raise RuntimeError("ffmpeg audio extraction failed")

            audio = _whisperx.load_audio(str(wav_path))

        result = model.transcribe(audio, batch_size=batch_size)

        # 2. Align word-level timestamps
        model_a, metadata_a = _whisperx.load_align_model(
            language_code=result.get("language", "en"), device=device
        )
        result = _whisperx.align(
            result["segments"], model_a, metadata_a, audio, device,
            return_char_alignments=False
        )

        # 3. Speaker diarization (optional — requires HuggingFace token)
        hf_token = config.get("hf_token", os.environ.get("HF_TOKEN", ""))
        if hf_token:
            try:
                diarize_model = _whisperx.DiarizationPipeline(
                    use_auth_token=hf_token, device=device
                )
                diarize_segments = diarize_model(audio)
                result = _whisperx.assign_word_speakers(diarize_segments, result)
            except Exception as e:
                log.warning(f"  Speaker diarization failed (non-fatal): {e}")

        # 4. Extract word-level data
        words = []
        for seg in result.get("segments", []):
            speaker = seg.get("speaker", "unknown")
            for w in seg.get("words", []):
                words.append({
                    "start": round(w.get("start", 0), 3),
                    "end": round(w.get("end", 0), 3),
                    "word": w.get("word", ""),
                    "speaker": speaker,
                })

        # Plain text from all segments
        plain_text = " ".join(
            seg.get("text", "") for seg in result.get("segments", [])
        ).strip()

        if not words and plain_text:
            # Alignment failed but we have text — single entry fallback
            words = [{"start": 0.0, "end": duration,
                       "word": plain_text, "speaker": "unknown"}]

        log.info(f"  WhisperX: {len(words)} words, "
                 f"{len(set(w['speaker'] for w in words))} speaker(s)")
        return plain_text, words

    except Exception as e:
        log.warning(f"  WhisperX failed, falling back to faster_whisper: {e}")
        text = engine.transcribe_audio(file_path, config)
        if text:
            return text, [{"start": 0.0, "end": duration,
                            "word": text, "speaker": "unknown"}]
        return "", []


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def load_config():
    """Load config from ~/.metanas/config.yaml (same as the GUI)."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
    # Defaults the engine expects
    cfg.setdefault("db_path", str(METANAS_HOME / "footage_metadata.db"))
    cfg.setdefault("vision_provider", "gemini")
    cfg.setdefault("whisper_model", "medium")
    cfg.setdefault("scene_threshold", 30)
    cfg.setdefault("max_scenes_per_clip", 8)
    cfg.setdefault("write_xmp_sidecar", True)
    cfg.setdefault("embed_metadata", True)
    cfg.setdefault("process_images", True)
    cfg.setdefault("reference_persons", [])
    cfg.setdefault("thumbnails_path", str(METANAS_HOME / "thumbnails"))
    return cfg


def find_video_files(root: Path, cameras: list | None = None) -> list[Path]:
    """Discover video files, optionally filtering by camera folders."""
    skip_folders = engine.SKIP_FOLDERS
    all_videos = sorted(
        p for p in root.rglob("*")
        if p.suffix.lower() in VIDEO_EXTENSIONS
        and p.is_file()
        and not any(part.lower() in skip_folders for part in p.parts)
    )
    if not cameras:
        return all_videos
    # Filter to clips inside matching camera folders
    cam_set = {c.lower() for c in cameras}
    filtered = []
    for p in all_videos:
        cam, _ = parse_camera_day(p)
        if cam and cam.lower() in cam_set:
            filtered.append(p)
        elif not cam:
            # Clip not under an SOP camera folder — include it (might be top-level)
            filtered.append(p)
    return filtered


def parse_camera_day(file_path: Path) -> tuple[str, int]:
    """Extract camera and day from the SOP folder hierarchy.

    Looks for a parent folder matching _Footage_<Camera>_dayN.
    Returns (camera, day) or (None, 0).
    """
    for parent in file_path.parents:
        m = _SOP_CAMERA_RE.search(parent.name)
        if m:
            cam_raw = m.group("camera")
            cam = CAMERA_ALIASES.get(cam_raw, cam_raw)
            day = int(m.group("day"))
            return cam, day
    return None, 0


def make_clip_id(camera: str | None, day: int, file_path: Path) -> str:
    """Build clipId: <camera>_day<N>_<filename_without_ext>."""
    stem = file_path.stem
    cam = camera or "unknown"
    return f"{cam}_day{day}_{stem}"


# ── Audio analysis ───────────────────────────────────────────────────────────

def compute_audio_rms(file_path: Path) -> tuple[float, float]:
    """Return (rms_dbfs, silence_ratio) for the clip's audio.

    Uses ffmpeg to decode audio to raw PCM, compute RMS and silence ratio.
    silence_ratio = fraction of 0.5s windows below -50 dBFS.
    Returns (-inf, 1.0) if no audio.
    """
    try:
        # Extract raw 16-bit mono PCM
        result = subprocess.run(
            ["ffmpeg", "-i", str(file_path), "-vn", "-ac", "1", "-ar", "16000",
             "-f", "s16le", "-"],
            capture_output=True, timeout=300
        )
        if result.returncode != 0 or len(result.stdout) < 100:
            return float("-inf"), 1.0

        import numpy as np
        samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0

        # Overall RMS
        rms = np.sqrt(np.mean(samples ** 2))
        rms_dbfs = 20 * np.log10(max(rms, 1e-10))

        # Silence ratio: fraction of 0.5s windows below -50 dBFS
        window_size = 8000  # 0.5s at 16kHz
        n_windows = max(1, len(samples) // window_size)
        silent_windows = 0
        for i in range(n_windows):
            chunk = samples[i * window_size:(i + 1) * window_size]
            w_rms = np.sqrt(np.mean(chunk ** 2))
            w_db = 20 * np.log10(max(w_rms, 1e-10))
            if w_db < -50:
                silent_windows += 1
        silence_ratio = silent_windows / n_windows

        return round(rms_dbfs, 1), round(silence_ratio, 3)
    except Exception as e:
        log.warning(f"  Audio RMS failed: {e}")
        return float("-inf"), 1.0


def detect_lip_sync_issues(file_path: Path, tech_meta: dict,
                           audio_rms_dbfs: float) -> tuple[bool, list]:
    """Check for lip motion without matching audio.

    Simple heuristic: if a face is on screen (checked via OpenCV) and
    the audio is absent or below -50 dBFS for spans > 3s, flag it.
    Returns (needs_manual_sync, sync_spans as [[start, end], ...]).
    """
    if audio_rms_dbfs > -50:
        return False, []

    duration = tech_meta.get("duration", 0)
    if duration < 3:
        return False, []

    try:
        # Sample a few frames and check for faces
        import numpy as np
        positions = [duration * 0.25, duration * 0.5, duration * 0.75]
        face_found = False
        with tempfile.TemporaryDirectory() as tmp:
            for i, pos in enumerate(positions):
                frame_path = Path(tmp) / f"sync_check_{i}.jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(pos), "-i", str(file_path),
                     "-frames:v", "1", "-q:v", "5", str(frame_path)],
                    capture_output=True, timeout=30
                )
                if frame_path.exists() and engine.frame_has_face(str(frame_path)):
                    face_found = True
                    break

        if not face_found:
            return False, []

        # Face found + audio below threshold → flag entire clip
        # More granular: check 3s windows
        sync_spans = []
        window = 3.0
        n_windows = int(duration / window)
        # Use ffmpeg to get audio levels per window
        result = subprocess.run(
            ["ffmpeg", "-i", str(file_path), "-vn", "-ac", "1", "-ar", "16000",
             "-f", "s16le", "-"],
            capture_output=True, timeout=300
        )
        if result.returncode == 0 and len(result.stdout) > 100:
            samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
            window_samples = int(window * 16000)
            for i in range(n_windows):
                chunk = samples[i * window_samples:(i + 1) * window_samples]
                if len(chunk) < window_samples // 2:
                    continue
                w_rms = np.sqrt(np.mean(chunk ** 2))
                w_db = 20 * np.log10(max(w_rms, 1e-10))
                if w_db < -50:
                    start_s = round(i * window, 1)
                    end_s = round(min((i + 1) * window, duration), 1)
                    sync_spans.append([start_s, end_s])

            # Merge adjacent spans
            if sync_spans:
                merged = [sync_spans[0]]
                for s in sync_spans[1:]:
                    if s[0] <= merged[-1][1] + 0.1:
                        merged[-1][1] = s[1]
                    else:
                        merged.append(s)
                # Only flag if at least one span > 3s
                sync_spans = [s for s in merged if (s[1] - s[0]) >= 3.0]

        if sync_spans:
            return True, sync_spans
        return False, []

    except Exception as e:
        log.warning(f"  Lip sync check failed: {e}")
        return False, []


def classify_shot_type(metadata: dict, tech_meta: dict,
                       audio_rms_dbfs: float, silence_ratio: float,
                       usable: bool) -> str:
    """Classify as talking / broll / mixed / unusable.

    talking: known face on screen with speech for >50% of clip.
    broll: no speech.
    mixed: both.
    unusable: quality check says so.
    """
    if not usable:
        return "unusable"

    persons = metadata.get("identified_persons", [])
    has_face = bool(persons)
    has_speech = silence_ratio < 0.5 and audio_rms_dbfs > -50
    transcription = metadata.get("transcription", "")
    has_transcript = bool(transcription and len(transcription.strip()) > 10)

    if has_face and (has_speech or has_transcript):
        if silence_ratio > 0.3:
            return "mixed"
        return "talking"
    elif has_speech or has_transcript:
        return "mixed"
    else:
        return "broll"


def assess_usable(tech_meta: dict) -> bool:
    """Basic quality gate. Returns True if clip is usable.

    Checks: has video stream, duration > 0.5s, resolution >= 480p.
    """
    if not tech_meta.get("codec"):
        return False
    duration = tech_meta.get("duration", 0)
    if duration < 0.5:
        return False
    height = tech_meta.get("height", 0) or 0
    if height < 480:
        return False
    return True


# ── XMP extended sidecar ─────────────────────────────────────────────────────

_XMP_CLI_TEMPLATE = """\
<?xpacket begin='﻿' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/' x:xmptk='metanas-cli'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
        xmlns:dc='http://purl.org/dc/elements/1.1/'
        xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
        xmlns:xmpDM='http://ns.adobe.com/xmp/1.0/DynamicMedia/'
        xmlns:xmp='http://ns.adobe.com/xap/1.0/'
        xmlns:metanas='http://metanas.app/ns/1.0/'>
      <dc:description>
        <rdf:Alt>
          <rdf:li xml:lang='x-default'>{description}</rdf:li>
        </rdf:Alt>
      </dc:description>
      <dc:subject>
        <rdf:Bag>
          {subject_items}
        </rdf:Bag>
      </dc:subject>
      <tiff:Model>{camera_model}</tiff:Model>
      <xmpDM:cameraModel>{camera_model}</xmpDM:cameraModel>
      <xmpDM:logComment>{log_comment}</xmpDM:logComment>
      <xmp:Rating>{rating}</xmp:Rating>
      <metanas:clipId>{clip_id}</metanas:clipId>
      <metanas:fileHash>{file_hash}</metanas:fileHash>
      <metanas:camera>{camera}</metanas:camera>
      <metanas:day>{day}</metanas:day>
      <metanas:shotType>{shot_type}</metanas:shotType>
      <metanas:needsManualSync>{needs_manual_sync}</metanas:needsManualSync>
      <metanas:syncSpans>{sync_spans_json}</metanas:syncSpans>
      <metanas:audioRmsDbfs>{audio_rms_dbfs}</metanas:audioRmsDbfs>
      <metanas:silenceRatio>{silence_ratio}</metanas:silenceRatio>
      <metanas:usable>{usable}</metanas:usable>
      <metanas:transcript>{transcript_json}</metanas:transcript>
      <metanas:metanasVersion>{metanas_version}</metanas:metanasVersion>
      <metanas:processedAt>{processed_at}</metanas:processedAt>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
"""


def write_cli_sidecar(media_path: Path, metadata: dict, cli_fields: dict,
                      overwrite: bool = False) -> Path:
    """Write the extended CLI sidecar atomically.

    Includes all original METANAS fields plus the new CLI fields.
    """
    xmp_path = media_path.with_suffix(".xmp")

    if xmp_path.exists() and not overwrite:
        log.info(f"  XMP sidecar exists — skipping: {xmp_path.name}")
        return xmp_path

    xs = engine.xs
    description, unique_tags, log_comment = engine.build_xmp_fields(metadata)
    camera_model = metadata.get("camera_model", "Unknown")
    subject_items = "\n          ".join(f"<rdf:li>{xs(t)}</rdf:li>" for t in unique_tags)

    # Transcript: store as JSON string
    transcript_words = cli_fields.get("transcript_words", [])
    transcript_json = json.dumps(transcript_words, separators=(",", ":"))

    xmp_content = _XMP_CLI_TEMPLATE.format(
        description=xs(description),
        subject_items=subject_items,
        camera_model=xs(camera_model),
        log_comment=xs(log_comment),
        rating=0,
        clip_id=xs(cli_fields.get("clip_id", "")),
        file_hash=xs(cli_fields.get("file_hash", "")),
        camera=xs(cli_fields.get("camera", "")),
        day=cli_fields.get("day", 0),
        shot_type=xs(cli_fields.get("shot_type", "")),
        needs_manual_sync=str(cli_fields.get("needs_manual_sync", False)).lower(),
        sync_spans_json=xs(json.dumps(cli_fields.get("sync_spans", []), separators=(",", ":"))),
        audio_rms_dbfs=cli_fields.get("audio_rms_dbfs", 0),
        silence_ratio=cli_fields.get("silence_ratio", 0),
        usable=str(cli_fields.get("usable", True)).lower(),
        transcript_json=xs(transcript_json),
        metanas_version=xs(APP_VERSION),
        processed_at=xs(datetime.now(timezone.utc).isoformat()),
    )

    # Atomic write: temp file + rename
    tmp_path = xmp_path.with_suffix(".xmp.tmp")
    try:
        tmp_path.write_text(xmp_content, encoding="utf-8")
        tmp_path.rename(xmp_path)
        log.info(f"  XMP sidecar written: {xmp_path.name}")
    except Exception as e:
        log.error(f"  XMP write failed: {e}")
        tmp_path.unlink(missing_ok=True)
        raise

    return xmp_path


# ── Locking ──────────────────────────────────────────────────────────────────

def acquire_lock(out_dir: Path) -> Path:
    """Write .metanas.lock with PID and start time. Raises if already locked."""
    lock_path = out_dir / ".metanas.lock"
    if lock_path.exists():
        try:
            lock_data = json.loads(lock_path.read_text())
            pid = lock_data.get("pid", 0)
            # Check if PID is still alive
            try:
                os.kill(pid, 0)
                # Process is alive — refuse
                print(f"ERROR: Another ingest is running (PID {pid}, started {lock_data.get('started', '?')})",
                      file=sys.stderr)
                print(f"Lock file: {lock_path}", file=sys.stderr)
                sys.exit(2)
            except OSError:
                # Process is dead — stale lock, remove it
                log.info(f"Removing stale lock (PID {pid} is dead)")
                lock_path.unlink()
        except (json.JSONDecodeError, KeyError):
            # Corrupt lock file — remove it
            lock_path.unlink()

    lock_data = {
        "pid": os.getpid(),
        "started": datetime.now(timezone.utc).isoformat(),
    }
    lock_path.write_text(json.dumps(lock_data))
    return lock_path


def release_lock(lock_path: Path):
    """Remove the lock file."""
    try:
        lock_path.unlink(missing_ok=True)
    except Exception:
        pass


# ── clips.json builder ───────────────────────────────────────────────────────

def parse_sidecar(xmp_path: Path) -> dict | None:
    """Parse a CLI-format XMP sidecar into a clips.json record."""
    try:
        text = xmp_path.read_text(encoding="utf-8")
    except Exception:
        return None

    def _extract(tag: str, namespace: str = "metanas") -> str:
        pattern = rf"<{namespace}:{tag}>(.*?)</{namespace}:{tag}>"
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""

    def _extract_dc_description() -> str:
        m = re.search(r"<rdf:li xml:lang='x-default'>(.*?)</rdf:li>", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    def _extract_dc_subjects() -> list[str]:
        items = re.findall(r"<rdf:li>(.*?)</rdf:li>", text)
        return [_unescape_xml(i) for i in items]

    def _unescape_xml(s: str) -> str:
        return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")

    clip_id = _unescape_xml(_extract("clipId"))
    if not clip_id:
        # Not a CLI sidecar — try to build clip_id from the media file
        media_path = xmp_path.with_suffix("")
        # Try common video extensions
        for ext in VIDEO_EXTENSIONS:
            candidate = xmp_path.with_suffix(ext)
            if candidate.exists():
                media_path = candidate
                break
        cam, day = parse_camera_day(media_path)
        clip_id = make_clip_id(cam, day, media_path)

    file_hash = _unescape_xml(_extract("fileHash"))
    camera = _unescape_xml(_extract("camera"))
    day_str = _extract("day")
    day = int(day_str) if day_str.isdigit() else 0
    shot_type = _unescape_xml(_extract("shotType"))
    needs_sync = _extract("needsManualSync").lower() == "true"
    sync_spans_raw = _unescape_xml(_extract("syncSpans"))
    try:
        sync_spans = json.loads(sync_spans_raw) if sync_spans_raw else []
    except json.JSONDecodeError:
        sync_spans = []
    audio_rms_raw = _extract("audioRmsDbfs")
    try:
        audio_rms = float(audio_rms_raw) if audio_rms_raw else 0
    except ValueError:
        audio_rms = 0
    silence_raw = _extract("silenceRatio")
    try:
        silence_ratio = float(silence_raw) if silence_raw else 0
    except ValueError:
        silence_ratio = 0
    usable = _extract("usable").lower() != "false"

    transcript_raw = _unescape_xml(_extract("transcript"))
    try:
        transcript_words = json.loads(transcript_raw) if transcript_raw else []
    except json.JSONDecodeError:
        transcript_words = []

    version = _unescape_xml(_extract("metanasVersion"))
    processed_at = _unescape_xml(_extract("processedAt"))

    # Existing engine fields from XMP
    description = _unescape_xml(_extract_dc_description())
    tags = _extract_dc_subjects()
    camera_model = _unescape_xml(_extract("Model", "tiff"))
    log_comment = _unescape_xml(_extract("logComment", "xmpDM"))

    # Determine media file path
    media_file = None
    for ext in VIDEO_EXTENSIONS:
        candidate = xmp_path.with_suffix(ext)
        if candidate.exists():
            media_file = candidate
            break

    # Get duration from ffprobe if media exists
    duration = 0
    if media_file:
        try:
            tech = engine.get_tech_meta(media_file)
            duration = tech.get("duration", 0)
        except Exception:
            pass

    return {
        "clip_id": clip_id,
        "path": str(media_file) if media_file else str(xmp_path.with_suffix("")),
        "file_hash": file_hash,
        "camera": camera,
        "day": day,
        "shot_type": shot_type,
        "needs_manual_sync": needs_sync,
        "sync_spans": sync_spans,
        "audio_rms_dbfs": audio_rms,
        "silence_ratio": silence_ratio,
        "usable": usable,
        "description": description,
        "tags": tags,
        "camera_model": camera_model,
        "transcript_words": transcript_words,
        "duration": duration,
        "metanas_version": version,
        "processed_at": processed_at,
    }


def build_clips_json(footage_folder: Path, out_dir: Path) -> Path:
    """Build clips.json from all XMP sidecars under footage_folder."""
    sidecars = sorted(footage_folder.rglob("*.xmp"))
    clips = []
    for xmp in sidecars:
        record = parse_sidecar(xmp)
        if record:
            clips.append(record)

    index = {
        "schema_version": 1,
        "project": {
            "footage_folder": str(footage_folder),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "clip_count": len(clips),
        },
        "clips": clips,
    }

    out_path = out_dir / "clips.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")
        tmp_path.rename(out_path)
    except Exception as e:
        tmp_path.unlink(missing_ok=True)
        raise

    return out_path


# ── Failures tracking ────────────────────────────────────────────────────────

def write_failures(out_dir: Path, failures: list[dict]):
    """Write failures.json atomically."""
    out_path = out_dir / "failures.json"
    tmp_path = out_path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(failures, indent=2), encoding="utf-8")
        tmp_path.rename(out_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)


# ═════════════════════════════════════════════════════════════════════════════
#  PER-CLIP PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def process_clip(file_path: Path, config: dict, reference_persons: list,
                 out_dir: Path, reprocess: bool = False) -> dict:
    """Run the full ingest pipeline on one video clip.

    Returns the clip record dict (for progress reporting).
    Raises on unrecoverable error.
    """
    # 1. Probe
    tech = engine.get_tech_meta(file_path)
    duration = tech.get("duration", 0)
    has_audio = tech.get("has_audio", False)

    # 2. Camera and day (new)
    folder_camera, day = parse_camera_day(file_path)
    meta_camera = engine.infer_camera_type(tech, file_path)
    camera = folder_camera or meta_camera or "Unknown"
    camera_warning = None
    if folder_camera and meta_camera and folder_camera.lower() != meta_camera.lower():
        camera_warning = f"Folder says {folder_camera}, metadata says {meta_camera}"
        log.warning(f"  Camera mismatch: {camera_warning} — keeping folder value")

    clip_id = make_clip_id(camera, day, file_path)

    # Hash for resume
    file_hash = file_hash_xxh64(file_path)

    # Check resume: if sidecar exists and hash matches, skip
    xmp_path = file_path.with_suffix(".xmp")
    if not reprocess and xmp_path.exists():
        try:
            existing = parse_sidecar(xmp_path)
            if existing and existing.get("file_hash") == file_hash:
                return {"clip_id": clip_id, "status": "skipped", "shot_type": "",
                        "duration": duration, "camera": camera, "day": day,
                        "file_path": file_path}
        except Exception:
            pass

    # 3. Scene detection + keyframes
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        keyframes = engine.extract_keyframes(file_path, config, tmp_path)

        # 4. Thumbnails — copy to out_dir/thumbs/
        thumbs_dir = out_dir / "thumbs"
        thumbs_dir.mkdir(parents=True, exist_ok=True)
        scene_thumbs = []
        for i, kf in enumerate(keyframes):
            kf_path = Path(kf)
            dest = thumbs_dir / f"{clip_id}_scene{i:02d}.jpg"
            # Resize to 480px wide
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(kf_path), "-vf", "scale=480:-1",
                     "-q:v", "3", str(dest)],
                    capture_output=True, timeout=30
                )
                if dest.exists():
                    scene_thumbs.append(str(dest.relative_to(out_dir)))
            except Exception:
                pass

        # 5. Transcription — WhisperX (word-level + speakers) with faster_whisper fallback
        transcription = ""
        transcript_words = []
        if has_audio:
            transcription, transcript_words = transcribe_whisperx(
                file_path, config, duration
            )

        # 6. Face matching and tags (existing engine)
        metadata = {"transcription": transcription}
        for kf in keyframes:
            ai_meta, provider = engine.analyse_frame_with_failover(
                kf, config, reference_persons
            )
            # Merge AI metadata (last frame wins for single-value fields)
            for key in ("description", "shot_type", "subjects", "setting",
                        "lighting", "motion", "mood", "camera_movement",
                        "time_of_day", "audio_type", "color_palette",
                        "mood_tags", "tags", "identified_persons"):
                val = ai_meta.get(key)
                if val:
                    if isinstance(val, list):
                        existing = metadata.get(key, [])
                        if isinstance(existing, list):
                            metadata[key] = existing + val
                        else:
                            metadata[key] = val
                    else:
                        metadata[key] = val

        # Deduplicate list fields
        for key in ("subjects", "tags", "mood_tags", "identified_persons"):
            if key in metadata and isinstance(metadata[key], list):
                seen = set()
                deduped = []
                for item in metadata[key]:
                    s = str(item).strip().lower()
                    if s and s not in seen:
                        seen.add(s)
                        deduped.append(str(item).strip())
                metadata[key] = deduped

        metadata["transcription"] = transcription
        metadata["camera_model"] = camera
        metadata["custom_tags"] = []

    # 7. Shot type and audio check (new)
    audio_rms, silence_ratio = float("-inf"), 1.0
    needs_sync, sync_spans = False, []
    if has_audio:
        audio_rms, silence_ratio = compute_audio_rms(file_path)
        needs_sync, sync_spans = detect_lip_sync_issues(
            file_path, tech, audio_rms
        )

    # 8. Quality / usable
    usable = assess_usable(tech)
    shot_type = classify_shot_type(metadata, tech, audio_rms, silence_ratio, usable)

    # 9. Write sidecar (atomic, then move on)
    cli_fields = {
        "clip_id": clip_id,
        "file_hash": file_hash,
        "camera": camera,
        "day": day,
        "shot_type": shot_type,
        "needs_manual_sync": needs_sync,
        "sync_spans": sync_spans,
        "audio_rms_dbfs": audio_rms if math.isfinite(audio_rms) else -999,
        "silence_ratio": silence_ratio,
        "usable": usable,
        "transcript_words": transcript_words,
        "scene_thumbs": scene_thumbs,
    }

    write_cli_sidecar(file_path, metadata, cli_fields, overwrite=reprocess)

    # Also run the engine's embed (for Premiere compatibility)
    if config.get("embed_metadata", True):
        try:
            engine.embed_metadata_in_video(file_path, metadata)
        except Exception as e:
            log.warning(f"  Embed failed (non-fatal): {e}")

    # Also upsert to DB (keeps the GUI's database in sync)
    try:
        conn = sqlite3.connect(config["db_path"], timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        engine.upsert_db(conn, {
            "file_path": str(file_path), "file_type": "video",
            "camera_model": camera,
            "duration": duration, "fps": tech.get("fps", 0),
            "description": metadata.get("description", ""),
            "shot_type": metadata.get("shot_type", ""),
            "subjects": json.dumps(engine.safe_str_list(metadata.get("subjects", []))),
            "setting": metadata.get("setting", ""),
            "lighting": metadata.get("lighting", ""),
            "motion": metadata.get("motion", ""),
            "mood": metadata.get("mood", ""),
            "camera_movement": metadata.get("camera_movement", ""),
            "time_of_day": metadata.get("time_of_day", ""),
            "audio_type": metadata.get("audio_type", ""),
            "color_palette": metadata.get("color_palette", ""),
            "mood_tags": json.dumps(engine.safe_str_list(metadata.get("mood_tags", []))),
            "tags": json.dumps(engine.safe_str_list(metadata.get("tags", []))),
            "persons": json.dumps(engine.safe_str_list(metadata.get("identified_persons", []))),
            "transcription": transcription,
            "vision_provider": config.get("vision_provider", "gemini"),
        })
        conn.close()
    except Exception as e:
        log.warning(f"  DB upsert failed (non-fatal): {e}")

    # Format duration as mm:ss
    dur_str = f"{int(duration)//60:02d}:{int(duration)%60:02d}"

    sync_note = ""
    if needs_sync:
        spans_str = " ".join(f"{s[0]:.0f}-{s[1]:.0f}" for s in sync_spans)
        sync_note = f"SYNC NEEDED {spans_str}"

    return {
        "clip_id": clip_id,
        "status": "ok" if not sync_note else sync_note,
        "shot_type": shot_type,
        "duration": duration,
        "duration_str": dur_str,
        "camera": camera,
        "day": day,
        "file_path": file_path,
        "needs_sync": needs_sync,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  INGEST COMMAND
# ═════════════════════════════════════════════════════════════════════════════

def cmd_ingest(args):
    """Run the full ingest pipeline on a footage folder."""
    footage_folder = Path(args.footage_folder).resolve()
    if not footage_folder.exists():
        print(f"ERROR: Folder not found: {footage_folder}", file=sys.stderr)
        sys.exit(2)

    # Determine output directory
    if args.out:
        out_dir = Path(args.out).resolve()
    else:
        # Default: sibling _Premiere/brief folder
        project_root = footage_folder.parent
        project_name = project_root.name
        out_dir = project_root / f"{project_name}_Premiere" / "brief"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Parse cameras filter
    cameras = None
    if args.cameras:
        cameras = [CAMERA_ALIASES.get(c.strip(), c.strip())
                   for c in args.cameras.split(",")]

    # Load config
    config = load_config()
    workers = args.workers or config.get("max_workers", os.cpu_count() or 4)
    # GPU transcription stays serial
    if config.get("vision_provider", "gemini").lower() == "ollama" and workers > 1:
        workers = 1

    reference_persons = config.get("reference_persons", [])

    # Discover clips
    videos = find_video_files(footage_folder, cameras)
    total = len(videos)

    if total == 0:
        print("No video files found.", file=sys.stderr)
        sys.exit(2)

    # Dry run
    if args.dry_run:
        print(f"Dry run: {total} clip(s) found\n")
        for i, v in enumerate(videos, 1):
            cam, day = parse_camera_day(v)
            cam_str = cam or "?"
            print(f"  [{i}/{total}] {cam_str}_day{day}/{v.name}")
        print(f"\nTotal: {total} clips")
        sys.exit(0)

    # Acquire lock
    lock_path = acquire_lock(out_dir)

    # Register signal handlers to clean up lock
    _original_sigint = signal.getsignal(signal.SIGINT)
    _original_sigterm = signal.getsignal(signal.SIGTERM)

    def _cleanup(signum, frame):
        release_lock(lock_path)
        if signum == signal.SIGINT:
            signal.signal(signal.SIGINT, _original_sigint)
            os.kill(os.getpid(), signal.SIGINT)
        else:
            sys.exit(1)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    if not args.quiet:
        print(f"METANAS ingest v{APP_VERSION}")
        print(f"Folder: {footage_folder}")
        print(f"Output: {out_dir}")
        print(f"Clips:  {total}")
        print(f"Workers: {workers}")
        print()

    start_time = time.time()
    processed = 0
    skipped = 0
    failed_count = 0
    failures = []
    sync_flagged = 0
    talking_seconds = 0
    broll_seconds = 0

    try:
        # Process clips (serially for now — vision API rate limiting makes
        # true parallelism tricky; the engine handles its own threading)
        for i, video in enumerate(videos, 1):
            rel_path = video.relative_to(footage_folder)
            try:
                result = process_clip(
                    video, config, reference_persons, out_dir,
                    reprocess=not args.resume
                )

                if result.get("status") == "skipped":
                    skipped += 1
                    if not args.quiet:
                        print(f"[{i}/{total}] {rel_path}  SKIP (up to date)",
                              flush=True)
                    continue

                processed += 1
                shot = result.get("shot_type", "")
                dur_str = result.get("duration_str", "??:??")
                status = result.get("status", "ok")
                dur = result.get("duration", 0)

                if shot == "talking":
                    talking_seconds += dur
                elif shot == "broll":
                    broll_seconds += dur
                if result.get("needs_sync"):
                    sync_flagged += 1

                if not args.quiet:
                    print(f"[{i}/{total}] {rel_path}  {shot}  {dur_str}  {status}",
                          flush=True)

            except Exception as e:
                failed_count += 1
                failures.append({
                    "path": str(video),
                    "error": str(e),
                })
                if not args.quiet:
                    err_msg = str(e)[:80]
                    print(f"[{i}/{total}] {rel_path}  -  -  FAILED {err_msg}",
                          flush=True)

        # Write failures.json
        if failures:
            write_failures(out_dir, failures)

        # Build clips.json (the index step)
        if not args.quiet:
            print("\nBuilding clips.json index...", flush=True)
        clips_path = build_clips_json(footage_folder, out_dir)
        if not args.quiet:
            idx = json.loads(clips_path.read_text())
            print(f"Index: {len(idx.get('clips', []))} clips → {clips_path}")

    finally:
        release_lock(lock_path)

    # Summary
    elapsed = time.time() - start_time
    elapsed_min = elapsed / 60

    if not args.quiet:
        print(f"\n{'─' * 60}")
        print(f"Processed: {processed}  Skipped: {skipped}  Failed: {failed_count}")
        if sync_flagged:
            print(f"Sync flagged: {sync_flagged}")
        print(f"Talking: {talking_seconds/60:.0f} min  B-roll: {broll_seconds/60:.0f} min")
        print(f"Wall time: {elapsed_min:.1f} min")

    # Exit code
    if failed_count > 0:
        sys.exit(1)
    sys.exit(0)


# ═════════════════════════════════════════════════════════════════════════════
#  INDEX COMMAND
# ═════════════════════════════════════════════════════════════════════════════

def cmd_index(args):
    """Rebuild clips.json from existing sidecars."""
    footage_folder = Path(args.footage_folder).resolve()
    if not footage_folder.exists():
        print(f"ERROR: Folder not found: {footage_folder}", file=sys.stderr)
        sys.exit(2)

    if args.out:
        out_dir = Path(args.out).resolve()
    else:
        project_root = footage_folder.parent
        project_name = project_root.name
        out_dir = project_root / f"{project_name}_Premiere" / "brief"
    out_dir.mkdir(parents=True, exist_ok=True)

    clips_path = build_clips_json(footage_folder, out_dir)
    idx = json.loads(clips_path.read_text())
    count = len(idx.get("clips", []))
    print(f"Index rebuilt: {count} clips → {clips_path}")


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="metanas",
        description="METANAS CLI — tag and index footage from the command line",
    )
    parser.add_argument("--version", action="version",
                        version=f"metanas {APP_VERSION}")

    subparsers = parser.add_subparsers(dest="command")

    # ── ingest ────────────────────────────────────────────────────────────
    p_ingest = subparsers.add_parser("ingest",
                                      help="Tag and transcribe a footage folder")
    p_ingest.add_argument("footage_folder", help="The _Footage folder to process")
    p_ingest.add_argument("--out", default=None,
                          help="Output folder for clips.json, thumbs/, failures.json "
                               "(default: <project>_Premiere/brief)")
    p_ingest.add_argument("--resume", action="store_true",
                          help="Skip clips whose sidecar exists and hash matches")
    p_ingest.add_argument("--cameras", default=None,
                          help="Comma-separated camera filter (e.g. A7S3,ZVE1)")
    p_ingest.add_argument("--workers", type=int, default=None,
                          help="Parallel workers (default: CPU count)")
    p_ingest.add_argument("--quiet", action="store_true",
                          help="Suppress per-clip progress lines")
    p_ingest.add_argument("--dry-run", action="store_true",
                          help="List what would be processed and exit")

    # ── index ─────────────────────────────────────────────────────────────
    p_index = subparsers.add_parser("index",
                                     help="Rebuild clips.json from existing sidecars")
    p_index.add_argument("footage_folder", help="The _Footage folder to scan")
    p_index.add_argument("--out", default=None,
                         help="Output folder for clips.json")

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "index":
        cmd_index(args)
    else:
        parser.print_help()
        sys.exit(2)


if __name__ == "__main__":
    main()
