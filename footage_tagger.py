#!/usr/bin/env python3
"""
Sheneller Ventures — Footage Metadata Tagger
Processes MP4 videos, JPG images, and Sony ARW RAW files on a NAS.
Generates XMP sidecar files AND embeds metadata directly into MP4/MOV
containers so Adobe Bridge and Premiere Pro can search and display it.

Vision providers:
  openai  — GPT-4o Vision
  gemini  — Google Gemini 2.5 Flash (google-genai SDK)
  ollama  — Local model via Ollama (Mac Mini)

Usage:
  python3 footage_tagger.py --config config.yaml
  python3 footage_tagger.py --config config.yaml --reprocess
  python3 footage_tagger.py --config config.yaml --folder "/Volumes/NAS/Projects/2026/MyProject"
  python3 footage_tagger.py --config config.yaml --project "031126a_FoodVlog_KL"
"""

import argparse
import base64
import concurrent.futures
import json
import logging
import re
import sqlite3
import subprocess
import sys
import shutil
import tempfile
import time
from pathlib import Path

import requests
import yaml

# ── Optional imports ──────────────────────────────────────────────────────────
try:
    from scenedetect import open_video, SceneManager
    from scenedetect.detectors import ContentDetector
    SCENEDETECT_AVAILABLE = True
except ImportError:
    SCENEDETECT_AVAILABLE = False

try:
    from faster_whisper import WhisperModel
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

try:
    from google import genai as google_genai
    from google.genai import types as google_genai_types
    import PIL.Image
    GEMINI_AVAILABLE = True
except Exception:
    GEMINI_AVAILABLE = False

# ── Cost tracker (Gemini 2.5 Flash pricing Apr 2026) ─────────────────────────
# $0.15 / 1M input tokens, $0.60 / 1M output tokens
# Each frame call ≈ 1 200 input tokens (image + prompt) + 400 output tokens
COST_TRACKER = {"calls": 0, "input_tokens": 0, "output_tokens": 0}
GEMINI_INPUT_PRICE  = 0.15 / 1_000_000   # $ per token
GEMINI_OUTPUT_PRICE = 0.60 / 1_000_000   # $ per token

# ── Throttling for API rate limiting (Feature 3) ────────────────────────────────
LAST_API_CALL_TIME = {"timestamp": 0}
MIN_API_INTERVAL = 4  # seconds between API calls (~15 req/min max)

def log_gemini_cost(label=""):
    c = COST_TRACKER
    est_cost = c["input_tokens"] * GEMINI_INPUT_PRICE + c["output_tokens"] * GEMINI_OUTPUT_PRICE
    log.info(f"  💰 Gemini cost so far{' ' + label if label else ''}: "
             f"{c['calls']} calls | "
             f"~{c['input_tokens']:,} in / {c['output_tokens']:,} out tokens | "
             f"≈ ${est_cost:.4f} USD")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
SKIP_FOLDERS = {"#recycle", "@eadir", "@tmp", ".spotlight-v100", ".trashes", ".fseventsd"}

SONY_MODEL_MAP = {
    "ilce-7sm3":  "Sony A7S III",
    "a7s3":       "Sony A7S III",
    "a7siii":     "Sony A7S III",
    "zv-e1":      "Sony ZV-E1",
    "zve1":       "Sony ZV-E1",
    "ilce-zv-e1": "Sony ZV-E1",
}

XMP_TEMPLATE = """\
<?xpacket begin='\ufeff' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/' x:xmptk='footage-tagger'>
  <rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
    <rdf:Description rdf:about=''
        xmlns:dc='http://purl.org/dc/elements/1.1/'
        xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
        xmlns:xmpDM='http://ns.adobe.com/xmp/1.0/DynamicMedia/'
        xmlns:xmp='http://ns.adobe.com/xap/1.0/'>
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
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>
"""

# ── Vision prompt ─────────────────────────────────────────────────────────────

def build_vision_prompt(reference_persons: list) -> str:
    persons_note = ""
    if reference_persons:
        names = ", ".join(p["name"] for p in reference_persons)
        persons_note = (
            f"\nIMPORTANT: You have been given reference photo(s) of known persons: {names}. "
            f"Carefully compare each face in the frame against these reference photos. "
            f"If you identify a match, include their name in 'identified_persons' AND use their "
            f"name throughout the description. "
            f"Only include a name if you are confident it is a match. Use an empty list if unsure."
        )
    return (
        "You are a professional video archivist creating searchable metadata for a film production "
        "company's footage library. Your descriptions must be detailed enough that an editor "
        "searching Adobe Bridge can find this specific clip by describing what they remember seeing.\n\n"
        "Analyse the provided frame(s) and return ONLY a valid JSON object with these exact keys:\n\n"
        "  shot_type          – one of: wide, medium, close-up, extreme close-up, aerial, POV, over-the-shoulder, two-shot, insert/cutaway\n"
        "  camera_movement    – one of: static, pan, tilt, dolly, tracking, crane/jib, steadicam/gimbal, handheld, drone/aerial, zoom, arc/orbit, time-lapse, slow motion\n"
        "  time_of_day        – one of: dawn, morning, midday, afternoon, golden hour, sunset, dusk, night, unknown\n"
        "  audio_type         – one of: silent, dialogue, ambient/natural sound, music, mixed, unknown\n"
        "  color_palette      – one of: warm, cool, neutral, high contrast, desaturated, vibrant, monochrome\n"
        "  subjects           – list of plain strings, each describing a visible person or subject "
                               "(e.g. 'woman in red dress with dark hair, mid-30s, speaking to camera')\n"
        "  setting            – specific environment: indoor/outdoor, type of room or location, "
                               "architectural features, city/country if recognisable, background details\n"
        "  lighting           – quality and source: e.g. soft natural window light, harsh midday sun, "
                               "warm golden hour, neon signs, studio softbox, overcast\n"
        "  motion             – camera and subject movement free-text detail: e.g. slow push-in on subject, "
                               "subject walking left to right, drone descending over rooftop\n"
        "  mood               – emotional tone: e.g. intimate and conversational, celebratory, tense, "
                               "peaceful, energetic, melancholic, humorous\n"
        "  mood_tags          – list of 4-6 conceptual/thematic tags describing feeling and intent rather than "
                               "literal content: e.g. 'luxury', 'nostalgia', 'freedom', 'teamwork', 'adventure', "
                               "'sustainability'. Think: what would a creative director search for?\n"
        "  tags               – list of 12-15 plain string keyword tags describing literal visible content: "
                               "activity, objects, location type, clothing colours, weather, props, cultural context\n"
        "  identified_persons – list of name strings of confirmed known persons visible (empty list if none)\n"
        "  description        – A detailed 4-6 sentence visual description so an editor can find this clip. "
                               "Include: who is in the shot and what they look like (name them if identified), "
                               "exactly what they are doing, where they are, what they are wearing, "
                               "the lighting and atmosphere, and notable background elements.\n"
        f"{persons_note}\n\n"
        "CRITICAL: Return ONLY a single valid JSON object. All values must be plain strings or lists of "
        "plain strings — never nested objects or dicts. Do not wrap in markdown. Do not truncate."
    )


def encode_image_b64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def clean_json(raw: str) -> str:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    return raw.strip()


def safe_str_list(value) -> list:
    """Ensure a value from AI response is a flat list of strings."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                # Gemini sometimes returns {"description": "..."} objects
                result.append(item.get("description") or item.get("name") or str(item))
            else:
                result.append(str(item))
        return result
    return [str(value)]


# ── GPT-4o Vision ─────────────────────────────────────────────────────────────

def analyse_frame_with_openai(frame_path, api_key, model="gpt-4o",
                               reference_persons=None, retries=3):
    if not OPENAI_AVAILABLE:
        log.error("openai not installed. Run: pip3 install openai")
        return {}
    reference_persons = reference_persons or []
    client = OpenAI(api_key=api_key)
    prompt = build_vision_prompt(reference_persons)
    content = [{"type": "text", "text": prompt}]
    for person in reference_persons:
        ref_path = person.get("reference_image", "")
        if ref_path and Path(ref_path).exists():
            b64 = encode_image_b64(ref_path)
            content.append({"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"}})
            content.append({"type": "text", "text": f"↑ Reference photo of {person['name']}."})
    b64_frame = encode_image_b64(frame_path)
    ext = Path(frame_path).suffix.lower()
    mime = "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
    content.append({"type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64_frame}", "detail": "high"}})
    content.append({"type": "text", "text": "↑ Frame to analyse."})
    # Exponential backoff: [5, 15, 30, 60] seconds
    retry_delays = [5, 15, 30, 60]
    for attempt in range(4):  # max 4 retries
        try:
            # Throttle API calls to stay under rate limits (~15 req/min)
            elapsed = time.time() - LAST_API_CALL_TIME["timestamp"]
            if elapsed < MIN_API_INTERVAL:
                time.sleep(MIN_API_INTERVAL - elapsed)
            LAST_API_CALL_TIME["timestamp"] = time.time()

            response = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": content}],
                max_tokens=2048, temperature=0.2)
            return json.loads(clean_json(response.choices[0].message.content))
        except json.JSONDecodeError as e:
            log.warning(f"GPT-4o JSON parse error (attempt {attempt+1}/4): {e}")
        except Exception as e:
            # Detect rate limiting
            error_str = str(e).lower()
            if "429" in error_str or "rate" in error_str:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else 60
                log.warning(f"Rate limited — retrying in {delay}s")
                if attempt < 4 - 1:
                    time.sleep(delay)
            elif "503" in error_str or "service" in error_str:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else 60
                log.warning(f"Service unavailable (503) — retrying in {delay}s")
                if attempt < 4 - 1:
                    time.sleep(delay)
            else:
                log.warning(f"GPT-4o attempt {attempt+1}/4 failed: {e}")
                if attempt < 4 - 1:
                    delay = retry_delays[attempt] if attempt < len(retry_delays) else 60
                    time.sleep(delay)
    return {}


# ── Gemini Vision ─────────────────────────────────────────────────────────────

def analyse_frame_with_gemini(frame_path, api_key, model="gemini-2.5-flash",
                               reference_persons=None, retries=3):
    if not GEMINI_AVAILABLE:
        log.error("google-genai not installed. Run: pip3 install google-genai Pillow")
        return {}
    reference_persons = reference_persons or []
    client = google_genai.Client(api_key=api_key)
    prompt = build_vision_prompt(reference_persons)
    contents = [prompt]
    for person in reference_persons:
        ref_path = person.get("reference_image", "")
        if ref_path and Path(ref_path).exists():
            try:
                contents.append(f"Reference photo of {person['name']}:")
                contents.append(PIL.Image.open(ref_path))
            except Exception as e:
                log.warning(f"Could not load reference image for {person['name']}: {e}")
    try:
        contents.append("Frame to analyse:")
        contents.append(PIL.Image.open(frame_path))
    except Exception as e:
        log.error(f"Could not open frame for Gemini: {e}")
        return {}
    # Exponential backoff: [5, 15, 30, 60] seconds
    retry_delays = [5, 15, 30, 60]
    for attempt in range(4):  # max 4 retries
        try:
            # Throttle API calls to stay under rate limits (~15 req/min)
            elapsed = time.time() - LAST_API_CALL_TIME["timestamp"]
            if elapsed < MIN_API_INTERVAL:
                time.sleep(MIN_API_INTERVAL - elapsed)
            LAST_API_CALL_TIME["timestamp"] = time.time()

            response = client.models.generate_content(
                model=model, contents=contents,
                config=google_genai_types.GenerateContentConfig(
                    temperature=0.2, max_output_tokens=4096))
            # Track token usage for cost estimation
            try:
                usage = response.usage_metadata
                COST_TRACKER["calls"] += 1
                COST_TRACKER["input_tokens"]  += getattr(usage, "prompt_token_count", 1200)
                COST_TRACKER["output_tokens"] += getattr(usage, "candidates_token_count", 400)
            except Exception:
                COST_TRACKER["calls"] += 1
                COST_TRACKER["input_tokens"]  += 1200  # fallback estimate
                COST_TRACKER["output_tokens"] += 400
            return json.loads(clean_json(response.text))
        except json.JSONDecodeError as e:
            log.warning(f"Gemini JSON parse error (attempt {attempt+1}/4): {e}")
        except Exception as e:
            # Detect rate limiting
            error_str = str(e).lower()
            if "429" in error_str or "rate" in error_str:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else 60
                log.warning(f"Rate limited — retrying in {delay}s")
                if attempt < 4 - 1:
                    time.sleep(delay)
            elif "503" in error_str or "service" in error_str:
                delay = retry_delays[attempt] if attempt < len(retry_delays) else 60
                log.warning(f"Service unavailable (503) — retrying in {delay}s")
                if attempt < 4 - 1:
                    time.sleep(delay)
            else:
                log.warning(f"Gemini attempt {attempt+1}/4 failed: {e}")
                if attempt < 4 - 1:
                    delay = retry_delays[attempt] if attempt < len(retry_delays) else 60
                    time.sleep(delay)
    return {}


# ── Ollama Vision ─────────────────────────────────────────────────────────────

def analyse_frame_with_ollama(frame_path, ollama_url, model,
                               reference_persons=None, retries=3):
    reference_persons = reference_persons or []
    prompt = build_vision_prompt(reference_persons)
    images = []
    for person in reference_persons:
        ref_path = person.get("reference_image", "")
        if ref_path and Path(ref_path).exists():
            images.append(encode_image_b64(ref_path))
    images.append(encode_image_b64(frame_path))
    payload = {"model": model, "prompt": prompt, "images": images,
               "stream": False, "options": {"temperature": 0.2}}
    for attempt in range(retries):
        try:
            resp = requests.post(f"{ollama_url.rstrip('/')}/api/generate",
                                 json=payload, timeout=120)
            resp.raise_for_status()
            return json.loads(clean_json(resp.json().get("response", "")))
        except json.JSONDecodeError as e:
            log.warning(f"Ollama JSON parse error (attempt {attempt+1}/3): {e}")
        except Exception as e:
            log.warning(f"Ollama attempt {attempt+1}/3 failed: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return {}


# ── Route vision ──────────────────────────────────────────────────────────────

def analyse_frame(frame_path, config, reference_persons):
    provider = config.get("vision_provider", "ollama").lower()
    if provider == "openai":
        return analyse_frame_with_openai(
            frame_path, api_key=config["openai_api_key"],
            model=config.get("openai_vision_model", "gpt-4o"),
            reference_persons=reference_persons)
    elif provider == "gemini":
        return analyse_frame_with_gemini(
            frame_path, api_key=config["gemini_api_key"],
            model=config.get("gemini_vision_model", "gemini-2.5-flash"),
            reference_persons=reference_persons)
    else:
        return analyse_frame_with_ollama(
            frame_path, ollama_url=config.get("ollama_url", "http://localhost:11434"),
            model=config.get("ollama_vision_model", "llama3.2-vision"),
            reference_persons=reference_persons)


def analyse_frame_with_failover(frame_path, config, reference_persons):
    """Try primary provider, fall back to secondary if primary returns empty dict.
    Returns (result_dict, provider_used_string)."""
    primary = config.get("vision_provider", "ollama").lower()
    secondary = config.get("secondary_vision_provider", "").lower() or ""

    # Try primary provider
    if primary == "openai":
        result = analyse_frame_with_openai(
            frame_path, api_key=config.get("openai_api_key", ""),
            model=config.get("openai_vision_model", "gpt-4o"),
            reference_persons=reference_persons)
    elif primary == "gemini":
        result = analyse_frame_with_gemini(
            frame_path, api_key=config.get("gemini_api_key", ""),
            model=config.get("gemini_vision_model", "gemini-2.5-flash"),
            reference_persons=reference_persons)
    else:
        result = analyse_frame_with_ollama(
            frame_path, ollama_url=config.get("ollama_url", "http://localhost:11434"),
            model=config.get("ollama_vision_model", "llama3.2-vision"),
            reference_persons=reference_persons)

    # If result is empty and secondary is configured, try secondary
    if not result and secondary and secondary != "none":
        log.warning(f"Primary provider ({primary}) failed, trying secondary ({secondary})")
        if secondary == "openai":
            result = analyse_frame_with_openai(
                frame_path, api_key=config.get("openai_api_key", ""),
                model=config.get("openai_vision_model", "gpt-4o"),
                reference_persons=reference_persons)
        elif secondary == "gemini":
            result = analyse_frame_with_gemini(
                frame_path, api_key=config.get("gemini_api_key", ""),
                model=config.get("gemini_vision_model", "gemini-2.5-flash"),
                reference_persons=reference_persons)
        else:
            result = analyse_frame_with_ollama(
                frame_path, ollama_url=config.get("ollama_url", "http://localhost:11434"),
                model=config.get("ollama_vision_model", "llama3.2-vision"),
                reference_persons=reference_persons)
        provider_used = secondary if result else primary
    else:
        provider_used = primary

    return result, provider_used


# ── Technical metadata ────────────────────────────────────────────────────────

def get_tech_meta(file_path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(file_path)],
            capture_output=True, text=True, timeout=30)
        data = json.loads(result.stdout)
        fmt = data.get("format", {})
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
        tags = {k.lower(): v for k, v in fmt.get("tags", {}).items()}
        tags.update({k.lower(): v for k, v in video.get("tags", {}).items()})
        return {
            "duration": float(fmt.get("duration", 0)),
            "width": video.get("width"), "height": video.get("height"),
            "codec": video.get("codec_name"),
            "fps": eval(video.get("r_frame_rate", "0/1")),
            "has_audio": bool(audio), "tags": tags,
        }
    except Exception as e:
        log.warning(f"ffprobe failed: {e}")
        return {}


def infer_camera_type(tech_meta, file_path):
    # Check ffprobe tags
    for v in tech_meta.get("tags", {}).values():
        key = str(v).lower().replace(" ", "").replace("-", "")
        for pat, name in SONY_MODEL_MAP.items():
            if pat.replace("-", "") in key:
                return name
    # Check path/filename (handles both spaces and underscores)
    path_lower = str(file_path).lower().replace(" ", "").replace("_", "")
    if "a7s3" in path_lower or "a7siii" in path_lower or "sonya7s" in path_lower:
        return "Sony A7S III"
    if "zve1" in path_lower or "zv-e1" in path_lower:
        return "Sony ZV-E1"
    if "dji" in Path(file_path).name.lower():
        return "DJI Drone"
    # Check exiftool
    try:
        result = subprocess.run(["exiftool", "-CameraModelName", "-s3", str(file_path)],
                                capture_output=True, text=True, timeout=15)
        raw = result.stdout.strip().lower().replace(" ", "").replace("-", "")
        for pat, name in SONY_MODEL_MAP.items():
            if pat.replace("-", "") in raw:
                return name
    except Exception:
        pass
    return "Unknown"


# ── ARW preview extraction ────────────────────────────────────────────────────

def extract_arw_preview(arw_path, output_path):
    # Method 1: exiftool binary extract to stdout, write manually
    for tag in ["-JpgFromRaw", "-PreviewImage", "-ThumbnailImage"]:
        try:
            result = subprocess.run(
                ["exiftool", tag, "-b", str(arw_path)],
                capture_output=True, timeout=30)
            if result.returncode == 0 and len(result.stdout) > 1000:
                output_path.write_bytes(result.stdout)
                return True
        except Exception as e:
            log.warning(f"exiftool {tag} failed: {e}")
    return False


# ── Transcription ─────────────────────────────────────────────────────────────

def transcribe_audio(file_path, config):
    if not WHISPER_AVAILABLE:
        return ""
    try:
        device = config.get("whisper_device", "auto")
        compute = config.get("whisper_compute_type", "auto")
        device = "cpu" if device == "auto" else device
        compute = "int8" if compute == "auto" else compute
        model = WhisperModel(config.get("whisper_model", "medium"),
                             device=device, compute_type=compute)
        segments, _ = model.transcribe(str(file_path), beam_size=5)
        return " ".join(s.text for s in segments).strip()
    except Exception as e:
        log.warning(f"Transcription failed for {file_path.name}: {e}")
        return ""


# ── Scene detection & keyframe extraction ────────────────────────────────────

def extract_keyframes(file_path, config, tmp_dir):
    frames = []
    threshold = config.get("scene_threshold", 30)
    max_scenes = config.get("max_scenes_per_clip", 8)
    if SCENEDETECT_AVAILABLE:
        try:
            video = open_video(str(file_path))
            manager = SceneManager()
            manager.add_detector(ContentDetector(threshold=threshold))
            manager.detect_scenes(video)
            scenes = manager.get_scene_list()[:max_scenes]
            log.info(f"  {len(scenes)} scene(s) detected")
            for i, (start, _) in enumerate(scenes):
                out = tmp_dir / f"frame_{i:04d}.jpg"
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(start.get_seconds()), "-i", str(file_path),
                     "-frames:v", "1", "-q:v", "2", str(out)],
                    capture_output=True, timeout=30)
                if out.exists():
                    frames.append(str(out))
        except Exception as e:
            log.warning(f"Scene detection failed: {e}")
    if not frames:
        try:
            dur = get_tech_meta(file_path).get("duration", 0)
            ts = dur / 2 if dur > 2 else 1
            out = tmp_dir / "frame_0000.jpg"
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(ts), "-i", str(file_path),
                 "-frames:v", "1", "-q:v", "2", str(out)],
                capture_output=True, timeout=30)
            if out.exists():
                frames.append(str(out))
                log.info("  1 scene(s) detected (fallback)")
        except Exception as e:
            log.warning(f"Frame extraction fallback failed: {e}")
    return frames


# ── XMP sidecar ───────────────────────────────────────────────────────────────

def xs(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_xmp_fields(metadata):
    """Return (description_str, unique_tags_list, log_comment_str)."""
    persons = metadata.get("identified_persons", [])
    if not isinstance(persons, list):
        persons = []

    desc_parts = []
    if metadata.get("description"):
        desc_parts.append(metadata["description"].strip())
    if metadata.get("setting"):
        desc_parts.append(f"Location: {metadata['setting']}.")
    if metadata.get("lighting"):
        desc_parts.append(f"Lighting: {metadata['lighting']}.")
    if metadata.get("camera_movement"):
        desc_parts.append(f"Camera movement: {metadata['camera_movement']}.")
    if metadata.get("motion"):
        desc_parts.append(f"Motion detail: {metadata['motion']}.")
    if metadata.get("mood"):
        desc_parts.append(f"Mood: {metadata['mood']}.")
    if metadata.get("time_of_day"):
        desc_parts.append(f"Time of day: {metadata['time_of_day']}.")
    if metadata.get("audio_type"):
        desc_parts.append(f"Audio: {metadata['audio_type']}.")
    if metadata.get("color_palette"):
        desc_parts.append(f"Color palette: {metadata['color_palette']}.")
    if metadata.get("shot_type"):
        desc_parts.append(f"Shot type: {metadata['shot_type']}.")
    subjects = safe_str_list(metadata.get("subjects", []))
    if subjects:
        desc_parts.append(f"Subjects: {', '.join(subjects)}.")
    if persons:
        desc_parts.append(f"People in this clip: {', '.join(persons)}.")
    if metadata.get("transcription"):
        desc_parts.append(f"Audio transcript: {metadata['transcription'][:800]}")
    description = " ".join(desc_parts)

    tags = list(persons)  # persons first
    tags.extend(safe_str_list(metadata.get("tags", [])))
    tags.extend(safe_str_list(metadata.get("mood_tags", [])))  # conceptual/mood tags
    if metadata.get("shot_type"):
        tags.append(metadata["shot_type"])
    if metadata.get("camera_movement"):
        tags.append(metadata["camera_movement"])
    if metadata.get("mood"):
        tags.append(metadata["mood"])
    if metadata.get("time_of_day"):
        tags.append(metadata["time_of_day"])
    if metadata.get("audio_type"):
        tags.append(metadata["audio_type"])
    if metadata.get("color_palette"):
        tags.append(metadata["color_palette"])
    if metadata.get("camera_model") and metadata["camera_model"] != "Unknown":
        tags.append(metadata["camera_model"])
    if metadata.get("setting"):
        setting_words = [w.strip(",.()") for w in metadata["setting"].split() if len(w) > 3]
        tags.extend(setting_words[:5])

    seen, unique_tags = set(), []
    for t in tags:
        tl = str(t).strip().lower()
        if tl and tl not in seen:
            seen.add(tl)
            unique_tags.append(str(t).strip())

    log_comment = " | ".join(filter(None, [
        f"PERSONS: {', '.join(persons)}" if persons else "",
        f"SHOT: {metadata.get('shot_type', '')}",
        f"MOVEMENT: {metadata.get('camera_movement', '')}",
        f"TIME: {metadata.get('time_of_day', '')}",
        f"AUDIO: {metadata.get('audio_type', '')}",
        f"CAMERA: {metadata.get('camera_model', '')}",
        f"LIGHTING: {metadata.get('lighting', '')}",
        f"MOOD: {metadata.get('mood', '')}",
        f"PALETTE: {metadata.get('color_palette', '')}",
    ]))

    return description, unique_tags, log_comment


def write_xmp_sidecar(media_path, metadata, overwrite=False):
    """Write XMP sidecar file next to media_path.
    Will NOT overwrite an existing sidecar unless overwrite=True.
    This protects manually curated metadata and prevents daily re-runs
    from trampling over previously tagged files.
    """
    xmp_path = media_path.with_suffix(".xmp")

    if xmp_path.exists() and not overwrite:
        log.info(f"  XMP sidecar already exists — skipping write: {xmp_path.name}")
        return xmp_path

    description, unique_tags, log_comment = build_xmp_fields(metadata)
    camera_model = metadata.get("camera_model", "Unknown")

    subject_items = "\n          ".join(f"<rdf:li>{xs(t)}</rdf:li>" for t in unique_tags)

    xmp_content = XMP_TEMPLATE.format(
        description=xs(description),
        subject_items=subject_items,
        camera_model=xs(camera_model),
        log_comment=xs(log_comment),
        rating=0,
    )
    xmp_path.write_text(xmp_content, encoding="utf-8")
    log.info(f"  XMP sidecar written: {xmp_path.name}")
    return xmp_path


# ── Embed metadata into MP4/MOV via exiftool ─────────────────────────────────
# This is required for Adobe Bridge to display metadata for video files.
# exiftool only writes to the metadata container — the video stream is untouched.

def embed_metadata_in_video(media_path, metadata):
    description, unique_tags, _ = build_xmp_fields(metadata)
    persons = metadata.get("identified_persons", [])
    if not isinstance(persons, list):
        persons = []

    # Build exiftool arguments
    cmd = [
        "exiftool",
        "-overwrite_original",          # no backup file created
        f"-XMP-dc:Description={description}",
        f"-XMP-dc:Title={Path(media_path).stem}",
        f"-Comment={description[:500]}",  # also write to QuickTime Comment for max compat
    ]

    # Write each keyword individually
    for tag in unique_tags[:25]:  # exiftool handles list fields via multiple args
        cmd.append(f"-XMP-dc:Subject={tag}")
        cmd.append(f"-Keywords={tag}")

    # Camera model
    camera = metadata.get("camera_model", "")
    if camera and camera != "Unknown":
        cmd.append(f"-XMP-tiff:Model={camera}")
        cmd.append(f"-XMP-xmpDM:CameraModel={camera}")

    # Rich structured metadata in dynamic media namespace
    log_parts = []
    if metadata.get("shot_type"):    log_parts.append(f"SHOT: {metadata['shot_type']}")
    if metadata.get("camera_movement"): log_parts.append(f"MOVEMENT: {metadata['camera_movement']}")
    if metadata.get("time_of_day"):  log_parts.append(f"TIME: {metadata['time_of_day']}")
    if metadata.get("audio_type"):   log_parts.append(f"AUDIO: {metadata['audio_type']}")
    if metadata.get("color_palette"): log_parts.append(f"PALETTE: {metadata['color_palette']}")
    if camera:                       log_parts.append(f"CAMERA: {camera}")
    if metadata.get("mood"):         log_parts.append(f"MOOD: {metadata['mood']}")
    if log_parts:
        cmd.append(f"-XMP-xmpDM:LogComment={' | '.join(log_parts)}")

    cmd.append(str(media_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            log.info(f"  ✓ Metadata embedded into: {media_path.name}")
        else:
            log.warning(f"  exiftool embed warning: {result.stderr.strip()[:200]}")
    except Exception as e:
        log.warning(f"  Could not embed metadata into {media_path.name}: {e}")


# ── Embed metadata into image via exiftool ────────────────────────────────────

def embed_metadata_in_image(media_path, metadata):
    description, unique_tags, _ = build_xmp_fields(metadata)

    cmd = [
        "exiftool",
        "-overwrite_original",
        f"-XMP-dc:Description={description}",
        f"-Description={description}",
        f"-ImageDescription={description[:500]}",
        f"-Caption-Abstract={description[:500]}",
    ]
    for tag in unique_tags[:25]:
        cmd.append(f"-XMP-dc:Subject={tag}")
        cmd.append(f"-Keywords={tag}")

    camera = metadata.get("camera_model", "")
    if camera and camera != "Unknown":
        cmd.append(f"-XMP-tiff:Model={camera}")

    cmd.append(str(media_path))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            log.info(f"  ✓ Metadata embedded into: {media_path.name}")
        else:
            log.warning(f"  exiftool embed warning: {result.stderr.strip()[:200]}")
    except Exception as e:
        log.warning(f"  Could not embed metadata into {media_path.name}: {e}")


# ── SQLite database ───────────────────────────────────────────────────────────

def init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            file_type TEXT, camera_model TEXT, duration REAL, fps REAL,
            description TEXT, shot_type TEXT, subjects TEXT,
            setting TEXT, lighting TEXT, motion TEXT, mood TEXT,
            camera_movement TEXT, time_of_day TEXT, audio_type TEXT,
            color_palette TEXT, mood_tags TEXT,
            tags TEXT, persons TEXT, transcription TEXT,
            vision_provider TEXT,
            processed_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
            file_path, description, subjects, setting, tags, persons, transcription,
            content='media_files', content_rowid='id'
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media_files BEGIN
            INSERT INTO media_fts(rowid,file_path,description,subjects,setting,tags,persons,transcription)
            VALUES(new.id,new.file_path,new.description,new.subjects,new.setting,new.tags,new.persons,new.transcription);
        END
    """)
    conn.commit()
    return conn


def upsert_db(conn, data):
    conn.execute("""
        INSERT INTO media_files
            (file_path,file_type,camera_model,duration,fps,description,shot_type,
             subjects,setting,lighting,motion,mood,
             camera_movement,time_of_day,audio_type,color_palette,mood_tags,
             tags,persons,transcription,vision_provider)
        VALUES
            (:file_path,:file_type,:camera_model,:duration,:fps,:description,:shot_type,
             :subjects,:setting,:lighting,:motion,:mood,
             :camera_movement,:time_of_day,:audio_type,:color_palette,:mood_tags,
             :tags,:persons,:transcription,:vision_provider)
        ON CONFLICT(file_path) DO UPDATE SET
            camera_model=excluded.camera_model, duration=excluded.duration,
            fps=excluded.fps, description=excluded.description,
            shot_type=excluded.shot_type,
            subjects=excluded.subjects, setting=excluded.setting,
            lighting=excluded.lighting, motion=excluded.motion,
            mood=excluded.mood,
            camera_movement=excluded.camera_movement,
            time_of_day=excluded.time_of_day,
            audio_type=excluded.audio_type,
            color_palette=excluded.color_palette,
            mood_tags=excluded.mood_tags,
            tags=excluded.tags,
            persons=excluded.persons, transcription=excluded.transcription,
            vision_provider=excluded.vision_provider,
            processed_at=datetime('now')
    """, data)
    conn.commit()


def already_processed(conn, file_path):
    """Return (should_skip: bool, reason: str).
    Skips if the file is already in the database OR if an XMP sidecar exists on disk.
    Both checks are needed so daily runs never overwrite existing work even if the
    database is cleared or a file was processed in a previous session.
    """
    fp = Path(file_path)
    in_db = conn.execute("SELECT id FROM media_files WHERE file_path=?",
                         (str(fp),)).fetchone() is not None
    xmp_exists = fp.with_suffix(".xmp").exists()
    if in_db and xmp_exists:
        return True, "already in DB + XMP exists"
    if in_db:
        return True, "already in DB (XMP missing — run --reprocess to re-embed)"
    if xmp_exists:
        return True, "XMP sidecar exists (not in DB — run --reprocess to re-index)"
    return False, ""


# ── File discovery ────────────────────────────────────────────────────────────

def should_skip(path):
    return any(part.lower() in SKIP_FOLDERS for part in path.parts)


def find_video_files(root):
    return sorted(p for p in root.rglob("*")
                  if p.suffix.lower() in (".mp4", ".mov", ".mxf")
                  and p.is_file() and not should_skip(p))


def find_image_files(root):
    return sorted(p for p in root.rglob("*")
                  if p.suffix.lower() in (".jpg", ".jpeg", ".arw")
                  and p.is_file() and not should_skip(p))


# ── Video processing ──────────────────────────────────────────────────────────

def process_video(file_path, config, conn, reference_persons, reprocess=False):
    fp_str = str(file_path)
    skip, reason = already_processed(conn, file_path)
    if not reprocess and skip:
        log.info(f"  SKIP — {reason}: {file_path.name}")
        return

    log.info(f"Processing: {file_path.name}")
    tech = get_tech_meta(file_path)
    camera_model = infer_camera_type(tech, file_path)
    provider = config.get("vision_provider", "ollama")

    ai_meta = {}
    provider_used = provider
    with tempfile.TemporaryDirectory() as tmp:
        frames = extract_keyframes(file_path, config, Path(tmp))
        if frames:
            log.info(f"  Analysing keyframe with {provider}…")
            ai_meta, provider_used = analyse_frame_with_failover(frames[0], config, reference_persons)

            # ── Save keyframes to permanent thumbnails folder for UI preview ──
            try:
                metanas_home = Path.home() / ".metanas"
                thumb_base = Path(config.get("thumbnails_path", str(metanas_home / "thumbnails")))
                thumb_dir = thumb_base / file_path.stem
                thumb_dir.mkdir(parents=True, exist_ok=True)
                for i, frame_path in enumerate(frames):
                    dest = thumb_dir / f"frame_{i:04d}.jpg"
                    shutil.copy2(frame_path, dest)
                log.info(f"  ✓ {len(frames)} thumbnail(s) saved for preview")
            except Exception as e:
                log.warning(f"  Could not save thumbnails: {e}")
        else:
            log.warning("  No keyframes extracted")

    persons = safe_str_list(ai_meta.get("identified_persons", []))
    if persons:
        log.info(f"  ✓ Identified: {', '.join(persons)}")
    else:
        log.info("  No known persons identified in this clip")

    transcription = ""
    if config.get("transcribe_audio", True) and tech.get("has_audio"):
        log.info("  Transcribing audio…")
        transcription = transcribe_audio(file_path, config)

    # Parse custom tags from config (Feature 1: Custom Preset Tags)
    custom_tags_str = config.get("custom_tags", "")
    custom_tags_list = [t.strip() for t in custom_tags_str.split(",") if t.strip()] if custom_tags_str else []

    # Extend tags with custom tags
    ai_tags = safe_str_list(ai_meta.get("tags", []))
    ai_tags.extend(custom_tags_list)

    metadata = {
        "description":        ai_meta.get("description", ""),
        "shot_type":          ai_meta.get("shot_type", ""),
        "subjects":           safe_str_list(ai_meta.get("subjects", [])),
        "setting":            ai_meta.get("setting", ""),
        "lighting":           ai_meta.get("lighting", ""),
        "motion":             ai_meta.get("motion", ""),
        "mood":               ai_meta.get("mood", ""),
        "camera_movement":    ai_meta.get("camera_movement", ""),
        "time_of_day":        ai_meta.get("time_of_day", ""),
        "audio_type":         ai_meta.get("audio_type", ""),
        "color_palette":      ai_meta.get("color_palette", ""),
        "mood_tags":          safe_str_list(ai_meta.get("mood_tags", [])),
        "tags":               ai_tags,
        "identified_persons": persons,
        "camera_model":       camera_model,
        "transcription":      transcription,
        "custom_tags":        custom_tags_list,
    }

    # Write XMP sidecar (controlled by settings — default on)
    if config.get("write_xmp_sidecar", True):
        write_xmp_sidecar(file_path, metadata, overwrite=reprocess)

    # Embed directly into video file so Bridge/Resolve can read natively (default on)
    if config.get("embed_metadata", True):
        embed_metadata_in_video(file_path, metadata)

    log.info(f"  ✓ Done — {metadata['shot_type'] or 'unknown'} | {metadata['camera_movement'] or 'static'} | {camera_model}")

    upsert_db(conn, {
        "file_path": fp_str, "file_type": "video", "camera_model": camera_model,
        "duration": tech.get("duration", 0), "fps": tech.get("fps", 0),
        "description": metadata["description"], "shot_type": metadata["shot_type"],
        "subjects": json.dumps(metadata["subjects"]), "setting": metadata["setting"],
        "lighting": metadata["lighting"], "motion": metadata["motion"],
        "mood": metadata["mood"],
        "camera_movement": metadata["camera_movement"],
        "time_of_day": metadata["time_of_day"],
        "audio_type": metadata["audio_type"],
        "color_palette": metadata["color_palette"],
        "mood_tags": json.dumps(metadata["mood_tags"]),
        "tags": json.dumps(metadata["tags"]),
        "persons": json.dumps(persons),
        "transcription": transcription, "vision_provider": provider_used,
    })


# ── Image processing ──────────────────────────────────────────────────────────

def process_image(file_path, config, conn, reference_persons, reprocess=False):
    fp_str = str(file_path)
    skip, reason = already_processed(conn, file_path)
    if not reprocess and skip:
        log.info(f"  SKIP — {reason}: {file_path.name}")
        return

    log.info(f"Processing: {file_path.name}")
    provider = config.get("vision_provider", "ollama")
    provider_used = provider

    if file_path.suffix.lower() == ".arw":
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp) / (file_path.stem + "_preview.jpg")
            if extract_arw_preview(file_path, preview):
                log.info("  ARW preview extracted")
                ai_meta, provider_used = analyse_frame_with_failover(str(preview), config, reference_persons)
                # Save ARW preview as thumbnail for UI
                try:
                    metanas_home = Path.home() / ".metanas"
                    thumb_base = Path(config.get("thumbnails_path", str(metanas_home / "thumbnails")))
                    thumb_dir = thumb_base / file_path.stem
                    thumb_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(preview, thumb_dir / "frame_0000.jpg")
                    log.info("  ✓ Thumbnail saved for preview")
                except Exception as e:
                    log.warning(f"  Could not save thumbnail: {e}")
            else:
                log.warning("  Could not extract ARW preview — skipping vision")
                ai_meta = {}
    else:
        ai_meta, provider_used = analyse_frame_with_failover(str(file_path), config, reference_persons)
        # For standard images (jpg/png), create a thumbnail copy for the UI
        try:
            metanas_home = Path.home() / ".metanas"
            thumb_base = Path(config.get("thumbnails_path", str(metanas_home / "thumbnails")))
            thumb_dir = thumb_base / file_path.stem
            thumb_dir.mkdir(parents=True, exist_ok=True)
            dest = thumb_dir / "frame_0000.jpg"
            if not dest.exists():
                shutil.copy2(file_path, dest)
                log.info("  ✓ Thumbnail saved for preview")
        except Exception as e:
            log.warning(f"  Could not save thumbnail: {e}")

    persons = safe_str_list(ai_meta.get("identified_persons", []))
    if persons:
        log.info(f"  ✓ Identified: {', '.join(persons)}")
    else:
        log.info("  No known persons identified in this image")

    camera_model = "Unknown"
    try:
        result = subprocess.run(["exiftool", "-CameraModelName", "-s3", str(file_path)],
                                capture_output=True, text=True, timeout=15)
        raw = result.stdout.strip().lower().replace(" ", "").replace("-", "")
        for pat, name in SONY_MODEL_MAP.items():
            if pat.replace("-", "") in raw:
                camera_model = name
                break
    except Exception:
        pass

    # Parse custom tags from config (Feature 1: Custom Preset Tags)
    custom_tags_str = config.get("custom_tags", "")
    custom_tags_list = [t.strip() for t in custom_tags_str.split(",") if t.strip()] if custom_tags_str else []

    # Extend tags with custom tags
    ai_tags = safe_str_list(ai_meta.get("tags", []))
    ai_tags.extend(custom_tags_list)

    metadata = {
        "description":        ai_meta.get("description", ""),
        "shot_type":          ai_meta.get("shot_type", ""),
        "subjects":           safe_str_list(ai_meta.get("subjects", [])),
        "setting":            ai_meta.get("setting", ""),
        "lighting":           ai_meta.get("lighting", ""),
        "motion":             ai_meta.get("motion", ""),
        "mood":               ai_meta.get("mood", ""),
        "camera_movement":    ai_meta.get("camera_movement", ""),
        "time_of_day":        ai_meta.get("time_of_day", ""),
        "audio_type":         ai_meta.get("audio_type", ""),
        "color_palette":      ai_meta.get("color_palette", ""),
        "mood_tags":          safe_str_list(ai_meta.get("mood_tags", [])),
        "tags":               ai_tags,
        "identified_persons": persons,
        "camera_model":       camera_model,
        "transcription":      "",
        "custom_tags":        custom_tags_list,
    }

    if config.get("write_xmp_sidecar", True):
        write_xmp_sidecar(file_path, metadata, overwrite=reprocess)
    if config.get("embed_metadata", True):
        embed_metadata_in_image(file_path, metadata)

    log.info(f"  ✓ Done — {metadata['shot_type'] or 'unknown'} | {metadata['camera_movement'] or 'static'} | {camera_model}")

    upsert_db(conn, {
        "file_path": fp_str, "file_type": "image", "camera_model": camera_model,
        "duration": 0, "fps": 0, "description": metadata["description"],
        "shot_type": metadata["shot_type"], "subjects": json.dumps(metadata["subjects"]),
        "setting": metadata["setting"], "lighting": metadata["lighting"],
        "motion": metadata["motion"], "mood": metadata["mood"],
        "camera_movement": metadata["camera_movement"],
        "time_of_day": metadata["time_of_day"],
        "audio_type": metadata["audio_type"],
        "color_palette": metadata["color_palette"],
        "mood_tags": json.dumps(metadata["mood_tags"]),
        "tags": json.dumps(metadata["tags"]), "persons": json.dumps(persons),
        "transcription": "", "vision_provider": provider_used,
    })


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sheneller Footage Tagger")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--project", default=None,
                        help="Search for a project folder by name under nas_mount_path")
    parser.add_argument("--folder", default=None,
                        help="Full path to a specific folder to process (overrides nas_mount_path)")
    parser.add_argument("--reprocess", action="store_true",
                        help="Re-embed metadata into already-processed files")
    parser.add_argument("--db-path", default=None,
                        help="Override the database path (used for per-project DBs)")
    parser.add_argument("--custom-tags", default=None,
                        help="Comma-separated custom tags to add to all clips (Feature 1)")
    args = parser.parse_args()

    if not Path(args.config).exists():
        log.error(f"Config not found: {args.config}")
        sys.exit(1)

    with open(args.config) as f:
        config = yaml.safe_load(f)

    # Per-project DB override — keeps the main DB untouched
    if args.db_path:
        config["db_path"] = args.db_path
        log.info(f"Project DB: {args.db_path}")

    # Custom tags from CLI (Feature 1: Custom Preset Tags)
    if args.custom_tags:
        config["custom_tags"] = args.custom_tags
        log.info(f"Custom tags: {args.custom_tags}")

    provider = config.get("vision_provider", "ollama").lower()
    model_name = {
        "openai": config.get("openai_vision_model", "gpt-4o"),
        "gemini": config.get("gemini_vision_model", "gemini-2.5-flash"),
        "ollama": config.get("ollama_vision_model", "llama3.2-vision"),
    }.get(provider, "unknown")
    log.info(f"Vision provider: {provider.upper()} ({model_name})")

    # ── Determine scan root ───────────────────────────────────────────────────
    if args.folder:
        # Direct path supplied — use it regardless of config
        scan_root = Path(args.folder)
        if not scan_root.exists():
            log.error(f"Folder not found: {scan_root}")
            sys.exit(1)
        log.info(f"Scanning folder: {scan_root}")
    else:
        nas_root = Path(config["nas_mount_path"])
        if not nas_root.exists():
            log.error(f"NAS not found: {nas_root}")
            sys.exit(1)
        if args.project:
            candidates = list(nas_root.rglob(args.project))
            if not candidates:
                log.error(f"Project folder '{args.project}' not found under {nas_root}")
                sys.exit(1)
            scan_root = candidates[0]
            log.info(f"Scanning project: {scan_root}")
        else:
            scan_root = nas_root
            log.info(f"Scanning: {scan_root}")

    reference_persons = config.get("reference_persons") or []
    for p in reference_persons:
        ref = p.get("reference_image", "")
        if Path(ref).exists():
            log.info(f"  Loaded reference image for: {p['name']}")
        else:
            log.warning(f"  Reference image NOT found for: {p['name']} ({ref})")
    if reference_persons:
        log.info(f"Person ID enabled for: {', '.join(p['name'] for p in reference_persons)}")

    log.info("Loading Whisper model…")
    conn = init_db(config["db_path"])

    videos = find_video_files(scan_root)
    images = find_image_files(scan_root) if config.get("process_images", True) else []
    total_files = len(videos) + len(images)

    # Pre-count how many are already done so we can show a useful summary
    skipped_count = sum(
        1 for fp in list(videos) + list(images)
        if already_processed(conn, fp)[0]
    )
    new_count = total_files - skipped_count
    log.info(f"Found {len(videos)} video(s) and {len(images)} image(s) — "
             f"{new_count} new, {skipped_count} already tagged (will skip)")
    if args.reprocess:
        log.info("  --reprocess flag set: existing XMP/metadata WILL be overwritten")

    start = time.time()
    processed = 0

    # Feature 4: Parallel Multi-Worker Processing
    max_workers = config.get("max_workers", 4)
    log.info(f"Using {max_workers} worker(s) for parallel processing")

    # Thread-safe lock for SQLite access (SQLite is not thread-safe by default)
    import threading
    _db_lock = threading.Lock()
    failed_files = []  # Feature 3: track failed files for retry

    def process_file_wrapper(file_type, file_index, total_index, file_path):
        """Wrapper to process a single file and track progress."""
        # Each thread gets its own DB connection for thread safety
        thread_conn = sqlite3.connect(config["db_path"])
        skip, _ = already_processed(thread_conn, file_path)
        log.info(f"\n[{file_type} {file_index}/{total_index}] {file_path.parent.name} / {file_path.name}")
        try:
            if file_type == "Video":
                process_video(file_path, config, thread_conn, reference_persons, args.reprocess)
            else:
                process_image(file_path, config, thread_conn, reference_persons, args.reprocess)
            thread_conn.close()
            if args.reprocess or not skip:
                return (1, None)
            return (0, None)
        except Exception as e:
            log.error(f"  ERROR: {e}")
            thread_conn.close()
            return (0, (file_type, file_index, total_index, file_path))

    # Build file list
    all_files = []
    for i, vp in enumerate(videos, 1):
        all_files.append(("Video", i, len(videos), vp))
    for i, ip in enumerate(images, 1):
        all_files.append(("Image", i, len(images), ip))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(process_file_wrapper, ft, fi, ti, fp): (ft, fi, ti, fp)
                   for ft, fi, ti, fp in all_files}
        for future in concurrent.futures.as_completed(futures):
            try:
                count, failed = future.result()
                processed += count
                if failed:
                    failed_files.append(failed)
                # Per-clip cost + ETA (same as v13.6)
                if processed > 0:
                    total_files = len(all_files)
                    eta = (time.time() - start) / max(processed, 1) * max(total_files - processed, 0) / 3600
                    log.info(f"  ETA: {eta:.1f}h remaining for new files")
                if config.get("vision_provider", "ollama").lower() == "gemini" and COST_TRACKER["calls"] > 0:
                    log_gemini_cost()
            except Exception as e:
                log.error(f"Worker exception: {e}")

    # Feature 3: Retry failed files
    if failed_files:
        log.info(f"\n── Retrying {len(failed_files)} failed file(s)… ──────────────────────────")
        for ft, fi, ti, fp in failed_files:
            log.info(f"  Retry: {fp.name}")
            retry_conn = sqlite3.connect(config["db_path"])
            try:
                if ft == "Video":
                    process_video(fp, config, retry_conn, reference_persons, args.reprocess)
                else:
                    process_image(fp, config, retry_conn, reference_persons, args.reprocess)
                processed += 1
                log.info(f"  ✓ Retry succeeded: {fp.name}")
            except Exception as e:
                log.error(f"  ✗ Retry failed: {fp.name} — {e}")
            finally:
                retry_conn.close()

    # Log costs
    if config.get("vision_provider", "ollama").lower() == "gemini" and COST_TRACKER["calls"] > 0:
        log_gemini_cost()

    conn.close()
    elapsed = (time.time() - start) / 60
    log.info(f"\n✓ Finished. {processed} new file(s) tagged, "
             f"{skipped_count} skipped (already done) — {elapsed:.0f} min total.")

    provider = config.get("vision_provider", "ollama").lower()
    if provider == "gemini" and COST_TRACKER["calls"] > 0:
        log.info("\n── Gemini API Cost Summary ──────────────────────────────────────────")
        log_gemini_cost("(final)")
        total_cost = (COST_TRACKER["input_tokens"] * GEMINI_INPUT_PRICE +
                      COST_TRACKER["output_tokens"] * GEMINI_OUTPUT_PRICE)
        log.info(f"  Total API calls : {COST_TRACKER['calls']}")
        log.info(f"  Input tokens    : {COST_TRACKER['input_tokens']:,}")
        log.info(f"  Output tokens   : {COST_TRACKER['output_tokens']:,}")
        log.info(f"  Estimated cost  : ${total_cost:.4f} USD")
        log.info(f"  (Rates: $0.15/1M input, $0.60/1M output — Gemini 2.5 Flash)")
        log.info("─────────────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
