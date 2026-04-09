#!/usr/bin/env python3
"""
METANAS — Metadata Tagger for NAS Footage
Web interface for the footage_tagger.py pipeline.

Usage:
    python3 app.py
    Open: http://localhost:5151
"""

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import hashlib
import hmac
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml
from flask import Flask, Response, jsonify, request, send_file

app = Flask(__name__)

BASE_DIR      = Path(__file__).parent
# ── All mutable user data lives OUTSIDE the app bundle so it survives updates
# ── and avoids macOS app-bundle write-protection (authorization denied).
METANAS_HOME  = Path.home() / ".metanas"
METANAS_HOME.mkdir(parents=True, exist_ok=True)
CONFIG_PATH   = METANAS_HOME / "config.yaml"
HISTORY_PATH  = METANAS_HOME / "job_history.json"

# ── One-time migration: if config exists inside old bundle location, move it ──
_OLD_CONFIG = BASE_DIR / "config.yaml"
if _OLD_CONFIG.exists() and not CONFIG_PATH.exists():
    try:
        with open(_OLD_CONFIG) as _f:
            _old_cfg = yaml.safe_load(_f) or {}
        # Fix db_path / thumbnails_path if they were stored inside the old bundle
        _old_db = _old_cfg.get("db_path", "")
        if _old_db and str(BASE_DIR) in _old_db:
            _old_cfg["db_path"] = str(METANAS_HOME / "footage_metadata.db")
        _old_thumb = _old_cfg.get("thumbnails_path", "")
        if _old_thumb and str(BASE_DIR) in _old_thumb:
            _old_cfg["thumbnails_path"] = str(METANAS_HOME / "thumbnails")
        with open(CONFIG_PATH, "w") as _f:
            yaml.dump(_old_cfg, _f, default_flow_style=False, allow_unicode=True)
    except Exception:
        pass  # if migration fails, fresh config will be created on first save

_OLD_HISTORY = BASE_DIR / "job_history.json"
if _OLD_HISTORY.exists() and not HISTORY_PATH.exists():
    try:
        import shutil as _shutil
        _shutil.copy2(_OLD_HISTORY, HISTORY_PATH)
    except Exception:
        pass

# ── Migrate old DB from bundle to ~/.metanas/ if present ─────────────────────
_OLD_DB = BASE_DIR / "footage_metadata.db"
_NEW_DB = METANAS_HOME / "footage_metadata.db"
if _OLD_DB.exists() and not _NEW_DB.exists():
    try:
        import shutil as _shutil
        _shutil.copy2(_OLD_DB, _NEW_DB)
        # Ensure the copy is user-writable — source inside app bundle may be read-only
        _NEW_DB.chmod(0o644)
    except Exception:
        pass

# ── Ensure existing DB is writable (catches bundles copied with bad perms) ───
if _NEW_DB.exists():
    try:
        import stat as _stat
        _mode = _NEW_DB.stat().st_mode
        if not (_mode & _stat.S_IWRITE):
            _NEW_DB.chmod(_mode | _stat.S_IWRITE | _stat.S_IREAD)
    except Exception:
        pass

# ── App version & update check ───────────────────────────────────────────────
APP_VERSION = "13.5.0"

# Host a public GitHub Gist with this JSON and paste its raw URL here.
# To release an update: edit the Gist, bump "version", update the notes.
# Format:
#   { "version": "1.1.0",
#     "download_url": "https://yoursite.com/download",
#     "release_notes": "What's new in this version",
#     "required": false }   ← set true to force update (blocks UI)
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/Shehaan23/metanas/main/version.json"

_update_info: dict = {}   # populated by background thread at startup

def _check_for_updates():
    """Background thread: fetch version manifest and compare to APP_VERSION."""
    global _update_info
    try:
        req = urllib.request.Request(UPDATE_MANIFEST_URL, headers={"User-Agent": f"METANAS/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            manifest = json.loads(resp.read().decode())
        latest   = manifest.get("version", "0.0.0")
        required = manifest.get("required", False)

        def _ver(v):
            return tuple(int(x) for x in str(v).split("."))

        if _ver(latest) > _ver(APP_VERSION):
            _update_info = {
                "available":      True,
                "current":        APP_VERSION,
                "latest":         latest,
                "download_url":   manifest.get("download_url", ""),
                "file_url":       manifest.get("file_url", ""),
                "release_notes":  manifest.get("release_notes", ""),
                "required":       required,
            }
    except Exception:
        pass   # silently ignore — no network, bad URL, etc.

# Start background update check 8 seconds after launch (avoids slowing startup)
def _deferred_update_check():
    time.sleep(8)
    _check_for_updates()

threading.Thread(target=_deferred_update_check, daemon=True).start()


# ── License configuration ─────────────────────────────────────────────────────
# Set this to your Gumroad product permalink (the slug in your product URL).
# e.g. if your product URL is https://shenellerventures.gumroad.com/l/metanas
# then set: GUMROAD_PRODUCT_ID = "metanas"
GUMROAD_PRODUCT_ID = "X6t8j5HG24oTU-e8e9JNzw=="  # Gumroad product ID (from Content tab)
LICENSE_PATH       = Path.home() / ".metanas" / "license.json"
# How many days between online re-verification checks
LICENSE_RECHECK_DAYS  = 7
# How many days to allow offline use before requiring a re-check
LICENSE_GRACE_DAYS    = 30

# ── License helpers ───────────────────────────────────────────────────────────

def get_machine_id() -> str:
    """Return a stable hardware fingerprint for this Mac.
    Result is cached to ~/.metanas/machine_id so it never changes between
    launches even if ioreg is slow or unavailable."""
    cache_path = Path.home() / ".metanas" / "machine_id"

    # Return cached value if available
    if cache_path.exists():
        try:
            cached = cache_path.read_text().strip()
            if cached:
                return cached
        except Exception:
            pass

    # Try ioreg (macOS hardware UUID — most stable)
    mid = ""
    try:
        result = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.splitlines():
            if "IOPlatformUUID" in line:
                mid = line.split('"')[-2]
                break
    except Exception:
        pass

    # Fallback — stable hash of hostname + username
    if not mid:
        import socket, getpass
        raw = f"{socket.gethostname()}-{getpass.getuser()}"
        mid = hashlib.sha256(raw.encode()).hexdigest()[:32]

    # Cache it so every future call returns the same value
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(mid)
    except Exception:
        pass

    return mid


def load_license() -> dict:
    if not LICENSE_PATH.exists():
        return {}
    try:
        with open(LICENSE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_license(data: dict):
    LICENSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LICENSE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def clear_license():
    if LICENSE_PATH.exists():
        LICENSE_PATH.unlink()


def verify_with_gumroad(license_key: str, increment: bool = False):
    """
    Call the Gumroad license verification API.
    Returns (valid: bool|None, purchase: dict|None, error_msg: str|None)
      valid=True  → key is active and good
      valid=False → key is definitively invalid/revoked/refunded
      valid=None  → network error (offline); cannot determine
    """
    try:
        payload = urllib.parse.urlencode({
            "product_id":           GUMROAD_PRODUCT_ID,
            "license_key":          license_key.strip().upper(),
            "increment_uses_count": str(increment).lower(),
        }).encode()
        req = urllib.request.Request(
            "https://api.gumroad.com/v2/licenses/verify",
            data=payload,
            method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())

        if not result.get("success"):
            return False, None, result.get("message", "Invalid license key.")

        purchase = result.get("purchase", {})
        if purchase.get("refunded") or purchase.get("chargebacked"):
            return False, None, "This license has been refunded or disputed."
        if purchase.get("subscription_cancelled_at") or purchase.get("subscription_failed_at"):
            return False, None, "This license subscription is no longer active."

        return True, purchase, None

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, None, "License key not found."
        return None, None, f"Network error ({e.code})"
    except Exception as e:
        return None, None, str(e)


def is_licensed() -> tuple[bool, str]:
    """
    Check whether this machine has a valid active license.
    Returns (licensed: bool, reason: str)
    """
    data = load_license()
    if not data or not data.get("license_key"):
        return False, "no_license"

    # Machine binding check
    if data.get("machine_id") != get_machine_id():
        return False, "wrong_machine"

    now = datetime.now(timezone.utc)

    # Check if periodic online re-verification is due
    last_check_str = data.get("last_verified", "")
    needs_recheck  = True
    offline_ok     = False
    if last_check_str:
        try:
            last_dt = datetime.fromisoformat(last_check_str)
            days_since = (now - last_dt).days
            needs_recheck = days_since >= LICENSE_RECHECK_DAYS
            offline_ok    = days_since < LICENSE_GRACE_DAYS
        except Exception:
            pass

    if needs_recheck:
        valid, purchase, err = verify_with_gumroad(data["license_key"])
        if valid is True:
            # Refresh cache
            data["last_verified"] = now.isoformat()
            save_license(data)
            return True, "ok"
        elif valid is False:
            # Definitively revoked — clear local license
            clear_license()
            return False, "revoked"
        else:
            # Network error — allow grace period
            if offline_ok:
                return True, "offline_grace"
            return False, "offline_expired"

    return True, "ok"


# ── Active jobs ───────────────────────────────────────────────────────────────
jobs = {}


# ── Config helpers ─────────────────────────────────────────────────────────────

def load_config():
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {
            "nas_mount_path": "/Volumes/YourNAS",
            "db_path": str(METANAS_HOME / "footage_metadata.db"),
        "vision_provider": "gemini",
        "openai_api_key": "",
        "openai_vision_model": "gpt-4o",
        "gemini_api_key": "",
        "gemini_vision_model": "gemini-2.5-flash",
        "ollama_url": "http://localhost:11434",
        "ollama_vision_model": "llama3.2-vision",
        "whisper_model": "medium",
        "transcribe_audio": True,
        "scene_threshold": 30,
        "max_scenes_per_clip": 8,
        "process_images": True,
        "reference_persons": [],
        "caffeinate": False,
        "write_xmp_sidecar": True,
        "embed_metadata": True,
        }

    # ── Auto-heal: ensure db_path points to a writable location ──────────────
    # Catches cases where an old config has db_path inside an app bundle or
    # other protected directory (produces "authorization denied" on sqlite3.connect).
    # NOTE: we do NOT require the .db file to exist yet — only the parent folder.
    db = cfg.get("db_path", "")
    # TCC-protected folders — macOS blocks app-bundle subprocesses from writing here
    _tcc_roots = [str(Path.home() / d) for d in ("Desktop", "Documents", "Downloads")]
    needs_heal = (
        not db
        or ".app/" in db
        or ".app\\" in db
        or any(db.startswith(r) for r in _tcc_roots)
    )
    # Only reset if the parent directory doesn't exist or isn't writable
    # (but allow db_path where the .db file doesn't exist yet — tagger will create it)
    if not needs_heal and db:
        parent = Path(db).parent
        if not parent.exists() or not os.access(str(parent), os.W_OK):
            needs_heal = True
    if needs_heal:
        cfg["db_path"] = str(METANAS_HOME / "footage_metadata.db")
        try:
            write_config(cfg)   # persist the fix so it only runs once
        except Exception:
            pass

    return cfg


def write_config(data):
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)


def mask_key(v):
    """Return a masked display value for API keys.
    Uses a sentinel so the save handler knows NOT to overwrite the real key."""
    v = str(v or "")
    if not v or "YOUR-" in v or len(v) < 12:
        return v
    # Sentinel format: first 8 chars + fixed placeholder + last 4 chars
    return v[:8] + "••••••••••••" + v[-4:]


# ── Job history ────────────────────────────────────────────────────────────────

def load_history():
    if HISTORY_PATH.exists():
        try:
            with open(HISTORY_PATH) as f:
                return json.load(f)
        except Exception:
            return []
    return []


def append_history(entry):
    history = load_history()
    history.insert(0, entry)
    with open(HISTORY_PATH, "w") as f:
        json.dump(history[:200], f, indent=2)


def migrate_db(db_path: str):
    """Add any columns that were introduced after the initial schema creation.
    Safe to call on every open — uses ALTER TABLE … ADD COLUMN which is a no-op
    if the column already exists (caught and ignored)."""
    try:
        conn = sqlite3.connect(db_path)
        new_columns = [
            ("fps",             "REAL"),
            ("shot_type",       "TEXT"),
            ("camera_movement", "TEXT"),
            ("time_of_day",     "TEXT"),
            ("audio_type",      "TEXT"),
            ("color_palette",   "TEXT"),
            ("mood_tags",       "TEXT"),
        ]
        existing = {row[1] for row in conn.execute("PRAGMA table_info(media_files)").fetchall()}
        for col, col_type in new_columns:
            if col not in existing:
                conn.execute(f"ALTER TABLE media_files ADD COLUMN {col} {col_type}")
        conn.commit()
        conn.close()
    except Exception:
        pass   # Don't crash the app if migration fails on a locked/missing DB


# ── API routes ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML_TEMPLATE

@app.route("/activate")
def activate_page():
    return ACTIVATION_PAGE


@app.route("/api/settings", methods=["GET"])
def get_settings():
    config = load_config()
    masked = dict(config)
    for k in ("openai_api_key", "gemini_api_key"):
        masked[k] = mask_key(masked.get(k, ""))
    return jsonify(masked)


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.json or {}
    config = load_config()
    for k, v in data.items():
        if isinstance(v, str) and "•••" in v:
            continue  # keep existing masked key
        config[k] = v

    # ── If db_path was updated, ensure its parent directory exists ──
    new_db = config.get("db_path", "")
    if new_db:
        db_parent = Path(new_db).expanduser().parent
        try:
            db_parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # read-only volume, etc — tagger will report the error

    write_config(config)
    return jsonify({"ok": True})


@app.route("/api/stats")
def stats():
    config = load_config()
    db_path = config.get("db_path", "")
    nas_ok  = Path(config.get("nas_mount_path", "")).exists()
    db_file_exists = db_path and Path(db_path).exists()
    if not db_path or not db_file_exists:
        return jsonify({"total": 0, "videos": 0, "images": 0,
                        "cameras": {}, "db_exists": False, "nas_ok": nas_ok,
                        "db_path_set": bool(db_path),
                        "db_parent_ok": bool(db_path) and Path(db_path).parent.exists()})
    migrate_db(db_path)
    try:
        conn    = sqlite3.connect(db_path)
        total   = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
        videos  = conn.execute("SELECT COUNT(*) FROM media_files WHERE file_type='video'").fetchone()[0]
        images  = conn.execute("SELECT COUNT(*) FROM media_files WHERE file_type='image'").fetchone()[0]
        cameras = {}
        for row in conn.execute("SELECT camera_model, COUNT(*) FROM media_files GROUP BY camera_model"):
            cameras[row[0] or "Unknown"] = row[1]
        last = conn.execute(
            "SELECT processed_at FROM media_files ORDER BY processed_at DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return jsonify({"total": total, "videos": videos, "images": images,
                        "cameras": cameras, "db_exists": True,
                        "nas_ok": nas_ok, "last_run": last[0] if last else None})
    except Exception as e:
        return jsonify({"error": str(e), "total": 0, "videos": 0, "images": 0,
                        "db_exists": True, "nas_ok": nas_ok})


@app.route("/api/tag", methods=["POST"])
def start_tag():
    data           = request.json or {}
    folder         = data.get("folder", "").strip()
    reprocess      = data.get("reprocess", False)
    save_to_main   = data.get("save_to_main", True)
    project_db     = data.get("project_db", "").strip()
    project_folder = data.get("project_folder", "").strip()

    if not folder:
        return jsonify({"error": "No folder specified"}), 400
    if not Path(folder).exists():
        return jsonify({"error": f"Folder not found: {folder}"}), 400

    job_id = str(uuid.uuid4())[:8]
    q      = queue.Queue()
    jobs[job_id] = {
        "queue": q, "status": "running", "folder": folder,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ended": None, "summary": None, "process": None,
        "reprocess": reprocess,
    }

    script = BASE_DIR / "footage_tagger.py"
    # Use the venv Python explicitly — sys.executable can resolve incorrectly
    # when Flask is launched from a macOS app bundle context.
    VENV_PYTHON = METANAS_HOME / ".venv" / "bin" / "python3"
    _python = str(VENV_PYTHON) if VENV_PYTHON.exists() else sys.executable
    cmd    = [_python, str(script), "--config", str(CONFIG_PATH),
              "--folder", folder]
    if reprocess:
        cmd.append("--reprocess")

    # ── Project-specific database support ─────────────────────────────
    # Resolve the project DB path so footage_tagger writes to the correct file.
    # If user chose a custom folder, use that; otherwise default to METANAS_HOME/project_dbs/
    project_db_path = None
    if project_db:
        # Ensure filename ends with .db
        if not project_db.endswith(".db"):
            project_db += ".db"
        # Determine save directory
        if project_folder:
            proj_dir = Path(project_folder).expanduser()
        else:
            proj_dir = METANAS_HOME / "project_dbs"
        proj_dir.mkdir(parents=True, exist_ok=True)
        project_db_path = str(proj_dir / project_db)

    # If save_to_main is False and no project DB specified, that's an error
    # (frontend should prevent this, but guard against it)
    if not save_to_main and not project_db_path:
        return jsonify({"error": "No database target — enable main archive or enter a project DB name."}), 400

    # Pass --db-path to footage_tagger when we have a project DB.
    # If save_to_main is False, the project DB *replaces* the main archive.
    # If save_to_main is True AND a project DB is set, footage_tagger writes
    # to the project DB (via --db-path) and we also write to main archive
    # by not overriding the config db_path — BUT footage_tagger only writes
    # to one DB at a time, so we handle dual-write by running the tagger
    # with the project DB path, then copying new rows to main afterwards.
    if project_db_path:
        cmd.extend(["--db-path", project_db_path])
        # Remember custom project DB folders so /api/project-dbs can find them later
        if project_folder:
            cfg = load_config()
            known_folders = cfg.get("project_db_folders", [])
            abs_folder = str(Path(project_folder).expanduser().resolve())
            if abs_folder not in known_folders:
                known_folders.append(abs_folder)
                cfg["project_db_folders"] = known_folders
                write_config(cfg)

    caffeinate_enabled = load_config().get("caffeinate", False)

    def run():
        caff_proc = None
        try:
            # ── Start caffeinate if enabled ──────────────────────────────────
            if caffeinate_enabled:
                try:
                    caff_proc = subprocess.Popen(["caffeinate", "-i"])
                    q.put({"line": "☕ Caffeinate active — Mac will not sleep during tagging"})
                except FileNotFoundError:
                    q.put({"line": "⚠ caffeinate not found (non-Mac?), continuing without it"})

            # Build a clean environment for the subprocess.
            # When launched from a macOS app bundle, os.environ can contain
            # PYTHONHOME / PYTHONPATH pointing at the system Python, which
            # breaks venv imports (google-genai, sqlite3, etc.).
            # We use the venv Python explicitly and give it only the vars it needs.
            import pwd as _pwd
            _pw = _pwd.getpwuid(os.getuid())
            _venv_bin = str(METANAS_HOME / ".venv" / "bin")
            # Include both Homebrew locations (Apple Silicon = /opt/homebrew,
            # Intel = /usr/local) so ffmpeg, ffprobe, exiftool are always found.
            _base_path = (
                "/opt/homebrew/bin:/opt/homebrew/sbin"
                ":/usr/local/bin:/usr/local/sbin"
                ":/usr/bin:/bin:/usr/sbin:/sbin"
            )
            _subprocess_env = {
                "HOME":            _pw.pw_dir,
                "USER":            _pw.pw_name,
                "LOGNAME":         _pw.pw_name,
                "PATH":            _venv_bin + ":" + _base_path,
                "TMPDIR":          os.environ.get("TMPDIR", "/tmp"),
                "LANG":            os.environ.get("LANG", "en_US.UTF-8"),
                "PYTHONUNBUFFERED": "1",
                # Deliberately omit PYTHONHOME / PYTHONPATH so the venv
                # manages its own sys.path without interference.
            }
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
                env=_subprocess_env
            )
            jobs[job_id]["process"] = proc
            summary_lines = []
            for line in proc.stdout:
                line = line.rstrip()
                q.put({"line": line})
                if any(kw in line for kw in ("Finished.", "Estimated cost", "💰")):
                    summary_lines.append(line)
            proc.wait()

            # ── Dual-write: copy project DB rows → main archive ──────────
            if proc.returncode == 0 and save_to_main and project_db_path:
                try:
                    cfg = load_config()
                    main_db = cfg.get("db_path", str(METANAS_HOME / "footage_metadata.db"))
                    q.put({"line": "📦 Syncing tagged data to main archive…"})
                    proj_conn = sqlite3.connect(project_db_path)
                    proj_conn.row_factory = sqlite3.Row
                    rows = proj_conn.execute("SELECT * FROM media_files").fetchall()
                    if rows:
                        cols = [desc[0] for desc in proj_conn.execute("SELECT * FROM media_files LIMIT 1").description]
                        # Exclude 'id' so main DB auto-generates its own IDs
                        data_cols = [c for c in cols if c != "id"]
                        placeholders = ", ".join("?" for _ in data_cols)
                        col_list = ", ".join(data_cols)
                        main_conn = sqlite3.connect(main_db)
                        # Ensure the main DB has the same table structure
                        # (init_db is called at startup, but just in case)
                        copied = 0
                        for row in rows:
                            vals = [row[c] for c in data_cols]
                            try:
                                # Use INSERT OR REPLACE keyed on file_path (UNIQUE)
                                main_conn.execute(
                                    f"INSERT OR REPLACE INTO media_files ({col_list}) VALUES ({placeholders})",
                                    vals
                                )
                                copied += 1
                            except Exception as row_err:
                                q.put({"line": f"⚠ Could not sync row: {row_err}"})
                        main_conn.commit()
                        main_conn.close()
                        q.put({"line": f"✓ {copied} file(s) synced to main archive"})
                    else:
                        q.put({"line": "ℹ No rows in project DB to sync"})
                    proj_conn.close()
                except Exception as sync_err:
                    q.put({"line": f"⚠ Sync to main archive failed: {sync_err}"})

            jobs[job_id]["status"]  = "done" if proc.returncode == 0 else "error"
            jobs[job_id]["ended"]   = time.strftime("%Y-%m-%d %H:%M:%S")
            jobs[job_id]["summary"] = " | ".join(summary_lines)
            append_history({
                "job_id":    job_id,
                "folder":    folder,
                "started":   jobs[job_id]["started"],
                "ended":     jobs[job_id]["ended"],
                "status":    jobs[job_id]["status"],
                "summary":   jobs[job_id]["summary"],
                "reprocess": reprocess,
            })
        except Exception as e:
            q.put({"line": f"ERROR: {e}"})
            jobs[job_id]["status"] = "error"
        finally:
            # ── Stop caffeinate when job ends ─────────────────────────────────
            if caff_proc and caff_proc.poll() is None:
                caff_proc.terminate()
                q.put({"line": "☕ Caffeinate released — Mac can sleep again"})
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/stream/<job_id>")
def stream(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404

    def generate():
        q = jobs[job_id]["queue"]
        while True:
            try:
                msg = q.get(timeout=25)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            if msg is None:
                payload = json.dumps({"__done__": True, "status": jobs[job_id]["status"]})
                yield f"data: {payload}\n\n"
                break
            yield f"data: {json.dumps(msg)}\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/stop/<job_id>", methods=["POST"])
def stop_job(job_id):
    job = jobs.get(job_id)
    if job:
        proc = job.get("process")
        if proc:
            proc.terminate()
        job["status"] = "stopped"
        job["ended"]  = time.strftime("%Y-%m-%d %H:%M:%S")
        job["queue"].put(None)
    return jsonify({"ok": True})


@app.route("/api/history")
def history():
    return jsonify(load_history())


# ── Smart Search: AI query expansion ──────────────────────────────────────────
def _expand_query(user_query: str, config: dict) -> str:
    """Use Gemini/OpenAI to expand a search query with synonyms and related terms.
    Returns an FTS5-compatible OR query string.
    Example: 'old lady smiling' → 'old OR lady OR smiling OR elderly OR woman OR senior OR grandmother OR happy OR joyful'
    """
    api_key  = config.get("gemini_api_key", "") or config.get("openai_api_key", "")
    provider = config.get("vision_provider", "gemini")
    if not api_key:
        # No API key — fall back to original query
        return user_query

    EXPAND_PROMPT = (
        "You are helping a video editor search their footage library. "
        "Given this search query, generate a list of synonyms, related terms, "
        "and alternative phrasings that a footage tagger might have used to describe "
        "similar content. Think about:\n"
        "- Synonyms (old lady → elderly woman, senior)\n"
        "- Related concepts (smiling → happy, joyful, cheerful, laughing)\n"
        "- Visual descriptions (sunset → golden hour, dusk, warm light)\n"
        "- Common tagging terms used in video production\n\n"
        "IMPORTANT: Return ONLY a comma-separated list of individual words and short phrases. "
        "No explanations, no numbering, no categories. Keep each term to 1-3 words max. "
        "Include the original query terms too. Aim for 10-20 terms total.\n\n"
        f"Search query: {user_query}"
    )

    try:
        if provider == "gemini":
            from google import genai as _genai
            _client = _genai.Client(api_key=api_key)
            resp = _client.models.generate_content(
                model=config.get("gemini_vision_model", "gemini-2.5-flash"),
                contents=EXPAND_PROMPT
            )
            raw = resp.text.strip()
        else:
            from openai import OpenAI
            resp = OpenAI(api_key=api_key).chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "user", "content": EXPAND_PROMPT}],
                temperature=0.3
            )
            raw = resp.choices[0].message.content.strip()

        # Parse comma-separated terms into FTS5 OR query
        terms = [t.strip().strip('"').strip("'") for t in raw.split(",") if t.strip()]
        # Also add the original words
        for word in user_query.split():
            w = word.strip().lower()
            if w and w not in [t.lower() for t in terms]:
                terms.insert(0, w)
        # Build FTS5 OR query — each term quoted to handle multi-word phrases
        fts_parts = []
        for t in terms[:25]:  # cap at 25 terms to avoid huge queries
            # For multi-word terms, wrap in quotes; single words go as-is
            cleaned = t.strip()
            if not cleaned:
                continue
            if " " in cleaned:
                fts_parts.append(f'"{cleaned}"')
            else:
                fts_parts.append(cleaned)
        return " OR ".join(fts_parts) if fts_parts else user_query
    except Exception:
        # AI call failed — fall back to original query
        return user_query


@app.route("/api/expand-query", methods=["POST"])
def expand_query_api():
    """Endpoint for the frontend to request query expansion."""
    data  = request.get_json() or {}
    query = data.get("q", "").strip()
    if not query:
        return jsonify({"expanded": "", "original": ""})
    config   = load_config()
    expanded = _expand_query(query, config)
    return jsonify({"expanded": expanded, "original": query})


@app.route("/api/search")
def search_api():
    query           = request.args.get("q", "").strip()
    smart           = request.args.get("smart", "false") == "true"
    ftype           = request.args.get("type", "")
    person          = request.args.get("person", "")
    camera          = request.args.get("camera", "")
    shot_type       = request.args.get("shot_type", "")
    setting         = request.args.get("setting", "")
    mood            = request.args.get("mood", "")
    lighting        = request.args.get("lighting", "")
    camera_movement = request.args.get("camera_movement", "")
    time_of_day     = request.args.get("time_of_day", "")
    audio_type      = request.args.get("audio_type", "")
    color_palette   = request.args.get("color_palette", "")
    file_ext        = request.args.get("file_ext", "")
    fps             = request.args.get("fps", "")
    has_people      = request.args.get("has_people", "")
    db_param        = request.args.get("db", "")

    config  = load_config()
    if db_param and Path(db_param).exists():
        db_path = db_param
    else:
        db_path = config.get("db_path", "")
    if not db_path or not Path(db_path).exists():
        return jsonify({"error": "Database not found. Run the tagger first."}), 404

    migrate_db(db_path)   # ensure fps column exists in older databases

    def _filters(sql, params, p=""):
        if ftype:           sql += f" AND {p}file_type = ?";        params.append(ftype)
        if camera:          sql += f" AND {p}camera_model = ?";     params.append(camera)
        if shot_type:       sql += f" AND {p}shot_type = ?";        params.append(shot_type)
        if setting:         sql += f" AND {p}setting = ?";          params.append(setting)
        if mood:            sql += f" AND {p}mood = ?";             params.append(mood)
        if lighting:        sql += f" AND {p}lighting = ?";         params.append(lighting)
        if camera_movement: sql += f" AND {p}camera_movement = ?";  params.append(camera_movement)
        if time_of_day:     sql += f" AND {p}time_of_day = ?";      params.append(time_of_day)
        if audio_type:      sql += f" AND {p}audio_type = ?";       params.append(audio_type)
        if color_palette:   sql += f" AND {p}color_palette = ?";    params.append(color_palette)
        if file_ext:
            c = f"{p}file_path"
            sql += f" AND LOWER(SUBSTR({c},INSTR({c},\'.\',-1)+1)) = ?"; params.append(file_ext.lower())
        if fps:
            sql += f" AND CAST({p}fps AS INTEGER) = ?"; params.append(int(fps))
        if has_people == "yes":
            sql += f" AND {p}persons IS NOT NULL AND {p}persons != \'[]\' AND {p}persons != \'\'"
        elif has_people == "no":
            sql += f" AND ({p}persons IS NULL OR {p}persons = \'[]\' OR {p}persons = \'\'"
        return sql, params

    SEL  = "file_path,file_type,camera_model,description,shot_type,persons,tags,setting,lighting,mood,fps,processed_at,camera_movement,time_of_day,audio_type,color_palette,mood_tags"
    MSEL = "m.file_path,m.file_type,m.camera_model,m.description,m.shot_type,m.persons,m.tags,m.setting,m.lighting,m.mood,m.fps,m.processed_at,m.camera_movement,m.time_of_day,m.audio_type,m.color_palette,m.mood_tags"

    # ── Smart search: expand query with AI synonyms ─────────────────
    expanded_query = None
    if query and smart:
        expanded_query = _expand_query(query, config)

    try:
        conn = sqlite3.connect(db_path)
        if person:
            sql, params = f"SELECT {SEL} FROM media_files WHERE LOWER(persons) LIKE ?", [f"%{person.lower()}%"]
            sql, params = _filters(sql, params)
            sql += " LIMIT 100"
        elif query:
            search_q = expanded_query if expanded_query else query
            sql = f"SELECT {MSEL} FROM media_fts f JOIN media_files m ON m.rowid=f.rowid WHERE media_fts MATCH ?"
            params = [search_q]
            sql, params = _filters(sql, params, "m.")
            sql += " LIMIT 50"
        else:
            sql, params = f"SELECT {SEL} FROM media_files WHERE 1=1", []
            sql, params = _filters(sql, params)
            sql += " ORDER BY processed_at DESC LIMIT 50"

        rows    = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            results.append({
                "file_path":       r[0],  "file_type":    r[1],
                "camera_model":    r[2] or "Unknown",
                "description":     r[3] or "",  "shot_type":  r[4] or "",
                "persons":         json.loads(r[5] or "[]"),
                "tags":            json.loads(r[6] or "[]"),
                "setting":         r[7] or "",   "lighting":   r[8] or "",
                "mood":            r[9] or "",   "fps":        r[10] or "",
                "processed_at":    r[11] or "",
                "camera_movement": r[12] or "",
                "time_of_day":     r[13] or "",
                "audio_type":      r[14] or "",
                "color_palette":   r[15] or "",
                "mood_tags":       json.loads(r[16] or "[]"),
                "filename":        Path(r[0]).name,
                "folder":          Path(r[0]).parent.name,
            })
        conn.close()
        # Include expansion info so the UI can show what was searched
        if expanded_query and expanded_query != query:
            return jsonify({"results": results, "expanded": expanded_query, "original": query})
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── File action routes ─────────────────────────────────────────────────────────

@app.route("/api/reveal", methods=["POST"])
def reveal_in_finder():
    """Select the file in Finder without opening it."""
    fp = (request.json or {}).get("file_path", "")
    if not fp or not Path(fp).exists():
        return jsonify({"error": "File not found"}), 404
    subprocess.Popen(["open", "-R", fp])
    return jsonify({"ok": True})


@app.route("/api/open-premiere", methods=["POST"])
def open_in_premiere():
    """Open the file directly in Adobe Premiere Pro."""
    fp = (request.json or {}).get("file_path", "")
    if not fp or not Path(fp).exists():
        return jsonify({"error": "File not found"}), 404
    result = subprocess.run(
        ["open", "-a", "Adobe Premiere Pro", fp],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return jsonify({"error": result.stderr.strip() or "Premiere Pro not found"}), 500
    return jsonify({"ok": True})


@app.route("/api/send-to-folder", methods=["POST"])
def send_to_folder():
    """Copy file (and its XMP sidecar if present) to the configured send folder."""
    import shutil
    fp   = (request.json or {}).get("file_path", "")
    dest = load_config().get("send_folder", "")
    if not fp or not Path(fp).exists():
        return jsonify({"error": "File not found"}), 404
    if not dest:
        return jsonify({"error": "No send folder configured. Set it in Settings."}), 400
    dest_dir = Path(dest)
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fp, dest_dir / Path(fp).name)
    # Copy XMP sidecar too if it exists
    xmp = Path(fp).with_suffix(".xmp")
    if xmp.exists():
        shutil.copy2(xmp, dest_dir / xmp.name)
    return jsonify({"ok": True, "dest": str(dest_dir / Path(fp).name)})



@app.route("/api/pick-folder", methods=["POST"])
def pick_folder():
    """Open a native macOS folder picker via osascript."""
    data       = request.get_json() or {}
    start_path = data.get("start_path", "~")

    # Expand ~ and verify the path exists; fall back to home directory
    start_path = str(Path(start_path).expanduser().resolve())
    if not Path(start_path).exists():
        start_path = str(Path.home())

    try:
        # 'choose folder' is a Standard Additions command.
        # Use separate -e flags (one per statement) — more reliable than a
        # single multi-line -e string.  Do NOT send Apple Events to Finder or
        # System Events; those trigger TCC permission prompts that silently fail
        # when the process is running in the background from a .app bundle.
        result = subprocess.run(
            [
                "osascript",
                "-e", f'set chosen to choose folder with prompt "Select a folder" default location POSIX file "{start_path}"',
                "-e", "return POSIX path of chosen",
            ],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                return jsonify({"path": path, "type": "folder"})
        # User cancelled (osascript exits non-zero on cancel)
        return jsonify({"error": "cancelled"}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Picker timed out"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/check-path")
def check_path():
    """Check whether a file path exists on disk."""
    path = urllib.parse.unquote(request.args.get("path", ""))
    if not path:
        return jsonify({"exists": False, "parent_exists": False})
    p = Path(path).expanduser()
    return jsonify({
        "exists": p.exists(),
        "parent_exists": p.parent.exists(),
        "parent_writable": p.parent.exists() and os.access(str(p.parent), os.W_OK),
    })


@app.route("/api/pick-file", methods=["POST"])
def pick_file():
    """Open a native macOS FILE picker via osascript."""
    data       = request.get_json() or {}
    start_path = data.get("start_path", "~")
    file_types = data.get("file_types", [])   # e.g. ["db"] or ["jpg","jpeg","png"]

    start_path = str(Path(start_path).expanduser().resolve())
    if not Path(start_path).exists():
        start_path = str(Path.home())

    # Build optional type filter clause
    if file_types:
        # osascript file type filter uses UTIs or file extensions
        ext_list = "{" + ", ".join(f'"{e}"' for e in file_types) + "}"
        type_clause = f" of type {ext_list}"
    else:
        type_clause = ""

    try:
        # Same approach as pick-folder: separate -e flags, no Apple Events to
        # Finder/System Events (avoids TCC permission failures from background).
        result = subprocess.run(
            [
                "osascript",
                "-e", f'set chosen to choose file with prompt "Select a file"{type_clause} default location POSIX file "{start_path}"',
                "-e", "return POSIX path of chosen",
            ],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            path = result.stdout.strip()
            if path:
                return jsonify({"path": path, "type": "file"})
        return jsonify({"error": "cancelled"}), 400
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Picker timed out"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/thumbnail")
def thumbnail():
    """Serve a cached keyframe thumbnail by path."""
    path = request.args.get("path", "")
    # Decode any URL-encoded characters (e.g. %20 → space)
    path = urllib.parse.unquote(path)
    p = Path(path)
    if path and p.exists() and p.is_file():
        return send_file(str(p.resolve()), mimetype="image/jpeg")
    return ("", 404)


@app.route("/api/file-thumbnails")
def file_thumbnails():
    """Return a list of cached thumbnail paths for a given media file."""
    fp         = urllib.parse.unquote(request.args.get("path", ""))
    config     = load_config()
    thumb_base = config.get("thumbnails_path", str(METANAS_HOME / "thumbnails"))
    if not fp:
        return jsonify([])
    stem  = Path(fp).stem
    tdir  = Path(thumb_base) / stem
    if not tdir.exists():
        return jsonify([])
    thumbs = sorted(tdir.glob("*.jpg"))
    return jsonify([str(t) for t in thumbs[:8]])


@app.route("/api/project-dbs")
def project_dbs():
    """List all project-specific SQLite databases.
    Scans the default project_dbs folder AND any custom folder the user
    has recently used (stored in config as project_db_folders)."""
    config   = load_config()
    # Collect all directories to scan
    scan_dirs = set()
    default_dir = METANAS_HOME / "project_dbs"
    if default_dir.exists():
        scan_dirs.add(default_dir)
    # Also scan any custom folders previously used
    for extra in config.get("project_db_folders", []):
        p = Path(extra).expanduser()
        if p.exists() and p.is_dir():
            scan_dirs.add(p)

    dbs = []
    seen_paths = set()
    for proj_dir in scan_dirs:
        for f in sorted(proj_dir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
            if str(f) in seen_paths:
                continue
            seen_paths.add(str(f))
            try:
                conn  = sqlite3.connect(str(f))
                count = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
                conn.close()
                dbs.append({
                    "name":     f.name,
                    "path":     str(f),
                    "count":    count,
                    "modified": __import__("datetime").datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"),
                })
            except Exception:
                pass
    # Sort all by modification date, newest first
    dbs.sort(key=lambda d: d["modified"], reverse=True)
    return jsonify(dbs)


@app.route("/api/filter-options")
def filter_options():
    """Return unique values for all search filter dropdowns."""
    db_param = request.args.get("db", "")
    config   = load_config()
    db_path  = db_param if (db_param and Path(db_param).exists()) else config.get("db_path", "")
    if not db_path or not Path(db_path).exists():
        return jsonify({})
    migrate_db(db_path)
    try:
        conn = sqlite3.connect(db_path)
        def uniq(col):
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM media_files WHERE {col} IS NOT NULL AND {col} != \'\' ORDER BY {col}"
            ).fetchall()]
        exts     = sorted(set(r[0] for r in conn.execute(
            "SELECT DISTINCT LOWER(SUBSTR(file_path,INSTR(file_path,\'.\',-1)+1)) FROM media_files"
        ).fetchall() if r[0]))
        fps_opts = sorted(set(r[0] for r in conn.execute(
            "SELECT DISTINCT CAST(fps AS INTEGER) FROM media_files WHERE fps IS NOT NULL AND fps != \'\' ORDER BY CAST(fps AS INTEGER)"
        ).fetchall() if r[0]))
        result = {
            "cameras":          uniq("camera_model"),
            "shot_types":       uniq("shot_type"),
            "camera_movements": uniq("camera_movement"),
            "time_of_day":      uniq("time_of_day"),
            "audio_types":      uniq("audio_type"),
            "color_palettes":   uniq("color_palette"),
            "settings":         uniq("setting"),
            "moods":            uniq("mood"),
            "lightings":        uniq("lighting"),
            "file_types":       ["video", "image"],
            "extensions":       exts,
            "fps_options":      fps_opts,
        }
        conn.close()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/api/script-source", methods=["POST"])
def script_source():
    """Parse a VO script with AI, then search DB for each shot."""
    data           = request.get_json() or {}
    script_text    = data.get("script", "").strip()
    db_param       = data.get("db", "")
    max_per        = int(data.get("max_per_shot", 5))
    media_filter   = data.get("media_filter", "all")        # all | video | image
    detection_mode = data.get("detection_mode", "both")      # both | scene | audio
    results_limit  = int(data.get("results_limit", 10))      # 5 | 10 | 15
    smart_search   = data.get("smart_search", True)           # default ON for script source
    if not script_text:
        return jsonify({"error": "No script provided"}), 400
    config  = load_config()
    db_path = db_param if (db_param and Path(db_param).exists()) else config.get("db_path", "")
    if not db_path or not Path(db_path).exists():
        return jsonify({"error": "Database not found — tag some footage first."}), 404
    api_key  = config.get("gemini_api_key", "") or config.get("openai_api_key", "")
    provider = config.get("vision_provider", "gemini")
    PROMPT   = (
        "You are a professional video editor assistant. Given this VO script or shot list, "
        "identify each distinct shot or scene required. For each shot output a JSON object with: "
        "\"label\": short description (max 10 words), "
        "\"query\": concise search keywords (3-8 words) to find this shot in a footage database. "
        "Return ONLY a JSON array, nothing else.\n\nScript:\n"
    )
    try:
        if provider == "gemini":
            from google import genai as _genai
            _client = _genai.Client(api_key=api_key)
            resp = _client.models.generate_content(
                model=config.get("gemini_vision_model", "gemini-2.5-flash"),
                contents=PROMPT + script_text
            )
            raw  = resp.text.strip()
        else:
            from openai import OpenAI
            resp = OpenAI(api_key=api_key).chat.completions.create(
                model="gpt-4o",
                messages=[{"role":"user","content": PROMPT + script_text}],
                temperature=0.2
            )
            raw = resp.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.split("\n")[1:])
        if raw.endswith("```"):
            raw = "\n".join(raw.split("\n")[:-1])
        shots_parsed = json.loads(raw)
    except Exception as e:
        return jsonify({"error": f"AI parsing failed: {str(e)}"}), 500

    SEL = ("m.file_path,m.file_type,m.camera_model,m.description,m.shot_type,"
           "m.persons,m.tags,m.setting,m.lighting,m.mood,m.fps,m.processed_at,"
           "m.camera_movement,m.time_of_day,m.audio_type,m.color_palette,m.mood_tags,"
           "m.transcription")
    FB  = ("file_path,file_type,camera_model,description,shot_type,"
           "persons,tags,setting,lighting,mood,fps,processed_at,"
           "camera_movement,time_of_day,audio_type,color_palette,mood_tags,"
           "transcription")

    # ── Build WHERE clause for media type filter ──────────────────
    media_where = ""
    media_params = []
    if media_filter == "video":
        media_where = " AND m.file_type = ?"
        media_params = ["video"]
    elif media_filter == "image":
        media_where = " AND m.file_type = ?"
        media_params = ["image"]

    # ── Detection mode affects which columns are searched ─────────
    # scene  = description, shot_type, setting, tags, mood (visual)
    # audio  = transcription, audio_type
    # both   = all of the above (default FTS behaviour)
    if detection_mode == "scene":
        fts_fields = "description OR shot_type OR setting OR tags OR mood"
    elif detection_mode == "audio":
        fts_fields = "transcription OR audio_type"
    else:
        fts_fields = None   # default — match all indexed columns

    try:
        conn = sqlite3.connect(db_path)
        results_out = []
        for shot in shots_parsed:
            label = shot.get("label", "")
            query = shot.get("query", "")

            # ── Smart search: expand the AI-generated query with synonyms ──
            search_query = query
            if smart_search and query:
                try:
                    search_query = _expand_query(query, config)
                except Exception:
                    search_query = query

            # ── FTS search ────────────────────────────────────
            try:
                if detection_mode == "scene":
                    fts_query = f"(description OR shot_type OR setting OR tags OR mood) : {search_query}"
                elif detection_mode == "audio":
                    fts_query = f"(transcription) : {search_query}"
                else:
                    fts_query = search_query

                rows = conn.execute(
                    f"SELECT {SEL} FROM media_fts f JOIN media_files m ON m.rowid=f.rowid "
                    f"WHERE media_fts MATCH ?{media_where} LIMIT ?",
                    [fts_query] + media_params + [max_per]
                ).fetchall()
            except Exception:
                rows = []

            # ── Fallback: LIKE search if FTS returns nothing ──
            if not rows:
                try:
                    kw = query.split()[0].lower() if query.split() else ""
                    fb_media = ""
                    fb_params = []
                    if media_filter == "video":
                        fb_media = " AND file_type = ?"
                        fb_params = ["video"]
                    elif media_filter == "image":
                        fb_media = " AND file_type = ?"
                        fb_params = ["image"]

                    if detection_mode == "scene":
                        search_col = "LOWER(description||' '||COALESCE(tags,'')||' '||COALESCE(setting,''))"
                    elif detection_mode == "audio":
                        search_col = "LOWER(COALESCE(transcription,'')||' '||COALESCE(audio_type,''))"
                    else:
                        search_col = "LOWER(description||' '||COALESCE(tags,'')||' '||COALESCE(transcription,''))"

                    rows = conn.execute(
                        f"SELECT {FB} FROM media_files WHERE {search_col} LIKE ?{fb_media} ORDER BY processed_at DESC LIMIT ?",
                        [f"%{kw}%"] + fb_params + [max_per]
                    ).fetchall()
                except Exception:
                    rows = []

            clips = [{"file_path":r[0],"file_type":r[1],"camera_model":r[2] or "Unknown",
                      "description":r[3] or "","shot_type":r[4] or "",
                      "persons":json.loads(r[5] or "[]"),"tags":json.loads(r[6] or "[]"),
                      "setting":r[7] or "","lighting":r[8] or "","mood":r[9] or "",
                      "fps":r[10] or "","processed_at":r[11] or "",
                      "camera_movement":r[12] or "","time_of_day":r[13] or "",
                      "audio_type":r[14] or "","color_palette":r[15] or "",
                      "mood_tags":json.loads(r[16] or "[]"),
                      "transcription":r[17] or "",
                      "filename":Path(r[0]).name,"folder":Path(r[0]).parent.name,
                      "_score":"","_toast":""} for r in rows]

            # ── Apply results_limit to total displayed per shot ──
            clips = clips[:results_limit]
            results_out.append({"label":label,"query":query,"results":clips,"_open":False})
        conn.close()
        if results_out:
            results_out[0]["_open"] = True
        return jsonify({"shots": results_out})
    except Exception as e:
        return jsonify({"error": f"DB search failed: {str(e)}"}), 500


# ── License API routes ─────────────────────────────────────────────────────────

@app.route("/api/activate-license", methods=["POST"])
def activate_license():
    data        = request.get_json() or {}
    license_key = data.get("license_key", "").strip().upper()
    if not license_key:
        return jsonify({"error": "Please enter a license key."}), 400

    valid, purchase, err = verify_with_gumroad(license_key, increment=True)

    if valid is None:
        # Network error — can't verify right now
        return jsonify({"error": f"Could not reach the activation server. Check your internet connection and try again.\n({err})"}), 503

    if not valid:
        return jsonify({"error": err or "Invalid license key."}), 402

    # Valid — save binding
    machine_id = get_machine_id()
    save_license({
        "license_key":   license_key,
        "machine_id":    machine_id,
        "last_verified": datetime.now(timezone.utc).isoformat(),
        "email":         purchase.get("email", ""),
        "activated_at":  datetime.now(timezone.utc).isoformat(),
    })
    return jsonify({"ok": True, "email": purchase.get("email", "")})


@app.route("/api/license-status")
def license_status():
    licensed, reason = is_licensed()
    data = load_license()
    return jsonify({
        "licensed": licensed,
        "reason":   reason,
        "email":    data.get("email", ""),
        "activated_at": data.get("activated_at", ""),
    })


@app.route("/api/deactivate-license", methods=["POST"])
def deactivate_license():
    clear_license()
    return jsonify({"ok": True})


@app.route("/api/update-status")
def update_status():
    return jsonify({
        "current_version": APP_VERSION,
        **_update_info,
        "available": _update_info.get("available", False),
    })


@app.route("/api/check-updates", methods=["POST"])
def trigger_update_check():
    """Allow manual re-check from Settings."""
    threading.Thread(target=_check_for_updates, daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/apply-update", methods=["POST"])
def apply_update():
    """Download the latest app.py from GitHub and replace local copies, then restart."""
    import shutil
    global _update_info

    if not _update_info.get("available"):
        return jsonify({"error": "No update available"}), 400

    file_url = _update_info.get("file_url", "")
    if not file_url:
        return jsonify({"error": "No file_url in update manifest — update version.json on GitHub"}), 400

    try:
        # 1. Download new app.py to a temp location
        tmp_path = METANAS_HOME / "app_update.py.tmp"
        req = urllib.request.Request(file_url, headers={"User-Agent": f"METANAS/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            new_code = resp.read()

        # Sanity check — must contain APP_VERSION and Flask
        code_text = new_code.decode("utf-8", errors="replace")
        if "APP_VERSION" not in code_text or "Flask" not in code_text:
            return jsonify({"error": "Downloaded file does not look like a valid app.py"}), 400

        with open(tmp_path, "wb") as f:
            f.write(new_code)

        # 2. Find all app.py locations in the bundle and replace them
        bundle_root = BASE_DIR  # Contents/Resources/footage-tagger/
        targets = [
            bundle_root / "app.py",                       # footage-tagger/app.py
            bundle_root.parent / "app.py",                # Resources/app.py
        ]
        replaced = []
        for target in targets:
            if target.exists():
                shutil.copy2(str(tmp_path), str(target))
                replaced.append(str(target))

        # 3. Clear __pycache__ in all relevant directories
        for d in [bundle_root, bundle_root.parent]:
            cache_dir = d / "__pycache__"
            if cache_dir.exists():
                shutil.rmtree(str(cache_dir), ignore_errors=True)

        # 4. Clean up temp file
        tmp_path.unlink(missing_ok=True)

        # 5. Schedule a restart — kill the Flask process after a short delay
        #    so the response can be sent back to the client first
        def _restart():
            import signal
            time.sleep(2)
            os.kill(os.getpid(), signal.SIGTERM)

        threading.Thread(target=_restart, daemon=True).start()

        return jsonify({
            "ok": True,
            "replaced": replaced,
            "message": f"Updated to {_update_info.get('latest', '?')}. METANAS is restarting…"
        })

    except Exception as e:
        return jsonify({"error": f"Update failed: {e}"}), 500


# ── Before-request license gate ────────────────────────────────────────────────

UNLOCKED_ENDPOINTS = {
    "activate_page", "activate_license", "license_status", "deactivate_license",
    "update_status", "trigger_update_check", "apply_update", "static"
}

@app.before_request
def check_license_gate():
    # Allow license/activation endpoints through unconditionally
    if request.endpoint in UNLOCKED_ENDPOINTS:
        return None
    licensed, reason = is_licensed()
    if not licensed:
        if request.path.startswith("/api/"):
            return jsonify({"error": "Not licensed.", "reason": reason}), 402
        return ACTIVATION_PAGE


# ── Activation page ────────────────────────────────────────────────────────────

ACTIVATION_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>METANAS — Activate</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #0f0f0f;
      color: #e0e0e0;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .card {
      background: #1a1a1a;
      border: 1px solid #2a2a2a;
      border-radius: 16px;
      padding: 48px 44px;
      width: 100%;
      max-width: 440px;
      box-shadow: 0 24px 64px rgba(0,0,0,.6);
    }
    .logo {
      width: 72px; height: 72px;
      background: #181818;
      border: 2px solid #ff8c00;
      border-radius: 16px;
      display: flex; align-items: center; justify-content: center;
      font-size: 28px; font-weight: 800;
      letter-spacing: -1px;
      margin: 0 auto 28px;
      box-shadow: 0 0 20px rgba(255,140,0,.25);
    }
    .logo span:first-child { color: #fff; }
    .logo span:last-child  { color: #ff8c00; }
    h1 { font-size: 22px; font-weight: 700; text-align: center; margin-bottom: 8px; }
    .sub {
      font-size: 13px; color: #888; text-align: center;
      margin-bottom: 32px; line-height: 1.5;
    }
    label { display: block; font-size: 12px; color: #aaa; margin-bottom: 6px; font-weight: 500; }
    input[type=text] {
      width: 100%; padding: 12px 14px;
      background: #111; border: 1px solid #333; border-radius: 8px;
      color: #e0e0e0; font-size: 15px; letter-spacing: .5px;
      font-family: monospace;
      outline: none; transition: border .2s;
    }
    input[type=text]:focus { border-color: #ff8c00; }
    .btn {
      width: 100%; padding: 13px;
      background: #ff8c00; color: #0f0f0f;
      border: none; border-radius: 8px;
      font-size: 15px; font-weight: 700;
      cursor: pointer; margin-top: 16px;
      transition: opacity .15s;
    }
    .btn:hover { opacity: .88; }
    .btn:disabled { opacity: .45; cursor: not-allowed; }
    .error {
      background: rgba(220,50,50,.12);
      border: 1px solid rgba(220,50,50,.3);
      border-radius: 8px;
      color: #f87171;
      font-size: 13px;
      padding: 10px 14px;
      margin-top: 14px;
      display: none;
      white-space: pre-wrap;
    }
    .success {
      background: rgba(50,200,100,.1);
      border: 1px solid rgba(50,200,100,.3);
      border-radius: 8px;
      color: #6ee7a0;
      font-size: 13px;
      padding: 10px 14px;
      margin-top: 14px;
      display: none;
    }
    .footer {
      text-align: center;
      margin-top: 28px;
      font-size: 12px;
      color: #555;
    }
    .footer a { color: #ff8c00; text-decoration: none; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo"><span>M</span><span>n</span></div>
    <h1>Activate METANAS</h1>
    <p class="sub">Enter the license key from your purchase confirmation email to unlock the app.</p>

    <label>License Key</label>
    <input type="text" id="key" placeholder="XXXX-XXXX-XXXX-XXXX"
           autocomplete="off" autocorrect="off" autocapitalize="characters" spellcheck="false">

    <button class="btn" id="btn" onclick="activate()">Activate</button>
    <div class="error" id="err"></div>
    <div class="success" id="ok"></div>

    <div class="footer">
      Don't have a license? <a href="https://shenellerventures.gumroad.com/l/metanas" target="_blank">Purchase here →</a>
    </div>
  </div>

  <script>
    async function activate() {
      const key = document.getElementById('key').value.trim();
      const btn = document.getElementById('btn');
      const err = document.getElementById('err');
      const ok  = document.getElementById('ok');
      err.style.display = 'none';
      ok.style.display  = 'none';
      if (!key) { err.textContent = 'Please enter your license key.'; err.style.display='block'; return; }
      btn.disabled = true;
      btn.textContent = 'Activating…';
      try {
        const res  = await fetch('/api/activate-license', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({license_key: key})
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          ok.textContent  = '✓ Activated successfully! Loading METANAS…';
          ok.style.display = 'block';
          setTimeout(() => window.location.href = '/', 1200);
        } else {
          err.textContent  = data.error || 'Activation failed.';
          err.style.display = 'block';
          btn.disabled = false;
          btn.textContent = 'Activate';
        }
      } catch(e) {
        err.textContent  = 'Network error. Please check your connection.';
        err.style.display = 'block';
        btn.disabled = false;
        btn.textContent = 'Activate';
      }
    }
    document.getElementById('key').addEventListener('keydown', e => { if (e.key === 'Enter') activate(); });
  </script>
</body>
</html>"""


# ── HTML / CSS / JS (single-page app) ─────────────────────────────────────────

HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>METANAS</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/alpinejs/3.13.5/cdn.min.js" defer></script>
<style>
  :root {
    --bg:       #0d0d0d;
    --surface:  #161616;
    --surface2: #1f1f1f;
    --border:   #2a2a2a;
    --accent:   #f97316;
    --accent2:  #fb923c;
    --text:     #e5e5e5;
    --muted:    #737373;
    --success:  #22c55e;
    --error:    #ef4444;
    --warn:     #eab308;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif; font-size: 14px; display: flex; height: 100vh; overflow: hidden; }

  /* Sidebar */
  #sidebar { width: 200px; min-width: 200px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 0; }
  .logo { padding: 22px 20px 18px; border-bottom: 1px solid var(--border); }
  .logo-mark { font-size: 20px; font-weight: 800; letter-spacing: 3px; color: var(--accent); }
  .logo-sub  { font-size: 10px; color: var(--muted); letter-spacing: 1px; margin-top: 2px; text-transform: uppercase; }
  nav { flex: 1; padding: 12px 0; }
  .nav-item { display: flex; align-items: center; gap: 10px; padding: 10px 18px; cursor: pointer; color: var(--muted); border-left: 3px solid transparent; transition: all .15s; font-size: 13px; }
  .nav-item:hover { color: var(--text); background: var(--surface2); }
  .nav-item.active { color: var(--accent); border-left-color: var(--accent); background: rgba(249,115,22,.08); }
  .nav-icon { font-size: 16px; width: 20px; text-align: center; }
  .sidebar-footer { padding: 14px 18px; border-top: 1px solid var(--border); }
  .status-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--success); display: inline-block; margin-right: 6px; }
  .status-dot.off { background: var(--error); }

  /* Main */
  #main { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
  .view { display: none; flex-direction: column; height: 100%; }
  .view.active { display: flex; }
  .view-header { padding: 28px 32px 20px; border-bottom: 1px solid var(--border); }
  .view-title { font-size: 20px; font-weight: 700; }
  .view-sub { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .view-body { flex: 1; padding: 28px 32px; overflow-y: auto; }

  /* Cards */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 20px; }
  .card-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(170px, 1fr)); gap: 14px; }
  .stat-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 18px 20px; }
  .stat-num  { font-size: 32px; font-weight: 800; color: var(--accent); line-height: 1; }
  .stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 1px; margin-top: 6px; }

  /* Forms */
  label { font-size: 12px; color: var(--muted); display: block; margin-bottom: 6px; text-transform: uppercase; letter-spacing: .5px; }
  input[type=text], input[type=password], select, textarea {
    width: 100%; background: var(--surface2); border: 1px solid var(--border); border-radius: 7px;
    color: var(--text); padding: 9px 12px; font-size: 13px; outline: none; font-family: inherit;
    transition: border-color .15s;
  }
  input[type=text]:focus, input[type=password]:focus, select:focus, textarea:focus { border-color: var(--accent); }
  select option { background: var(--surface2); }
  .form-row { margin-bottom: 18px; }
  .form-row-inline { display: flex; gap: 14px; }
  .form-row-inline .form-row { flex: 1; }

  /* Toggle */
  .toggle-wrap { display: flex; align-items: center; gap: 10px; cursor: pointer; }
  .toggle { width: 36px; height: 20px; background: var(--border); border-radius: 10px; position: relative; transition: background .2s; flex-shrink: 0; }
  .toggle.on { background: var(--accent); }

  /* Video scrubbing preview */
  .scrub-wrap { position:relative; width:100%; aspect-ratio:16/9; background:#000; border-radius:6px 6px 0 0; overflow:hidden; cursor:col-resize; margin-bottom:0; }
  .scrub-img { position:absolute; inset:0; width:100%; height:100%; object-fit:cover; opacity:0; transition:opacity .08s; }
  .scrub-img.active { opacity:1; }
  .scrub-placeholder { position:absolute; inset:0; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:6px; color:#555; font-size:11px; }
  .scrub-bar { position:absolute; bottom:0; left:0; height:3px; background:var(--accent); transition:width .05s; pointer-events:none; }
  .scrub-timecode { position:absolute; bottom:6px; right:8px; font-size:10px; font-family:monospace; color:rgba(255,255,255,.7); background:rgba(0,0,0,.5); padding:1px 5px; border-radius:3px; }
  .toggle::after { content:''; position: absolute; width: 14px; height: 14px; background: #fff; border-radius: 50%; top: 3px; left: 3px; transition: left .2s; }
  .toggle.on::after { left: 19px; }
  .toggle-label { font-size: 13px; color: var(--text); }

  /* Buttons */
  .btn { padding: 9px 18px; border-radius: 7px; border: none; cursor: pointer; font-size: 13px; font-weight: 600; transition: all .15s; font-family: inherit; }
  .btn-primary { background: var(--accent); color: #fff; }
  .btn-primary:hover { background: var(--accent2); }
  .btn-primary:disabled { background: #4a3020; color: #888; cursor: not-allowed; }
  .btn-danger  { background: var(--error); color: #fff; }
  .btn-danger:hover  { background: #dc2626; }
  .btn-ghost   { background: transparent; border: 1px solid var(--border); color: var(--text); }
  .btn-ghost:hover { border-color: var(--accent); color: var(--accent); }
  .btn-sm { padding: 6px 12px; font-size: 12px; }

  /* Terminal log */
  #log-wrap { background: #080808; border: 1px solid var(--border); border-radius: 10px; height: 340px; overflow-y: auto; padding: 14px 16px; font-family: "SF Mono", "Fira Code", "Cascadia Code", monospace; font-size: 12px; line-height: 1.65; }
  .log-line { white-space: pre-wrap; word-break: break-all; }
  .log-line.ok   { color: var(--success); }
  .log-line.err  { color: var(--error); }
  .log-line.warn { color: var(--warn); }
  .log-line.cost { color: var(--accent); }
  .log-line.skip { color: var(--muted); }

  /* Progress bar */
  .prog-bar { height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin: 12px 0; }
  .prog-fill { height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent2)); transition: width .4s; }

  /* Search results */
  .result-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 14px; margin-top: 20px; }
  .result-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px; padding: 16px; cursor: default; transition: border-color .15s; }
  .result-card:hover { border-color: var(--accent); }
  .result-filename { font-weight: 700; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .result-folder   { font-size: 11px; color: var(--muted); margin-top: 2px; }
  .result-desc     { font-size: 12px; color: #a3a3a3; margin: 10px 0 8px; line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
  .tag-row  { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .tag      { background: var(--surface2); border: 1px solid var(--border); border-radius: 4px; padding: 2px 7px; font-size: 11px; color: var(--muted); }
  .tag.person { border-color: var(--accent); color: var(--accent); background: rgba(249,115,22,.08); }
  .badge { display: inline-flex; align-items: center; gap: 4px; font-size: 10px; padding: 2px 7px; border-radius: 4px; font-weight: 600; text-transform: uppercase; letter-spacing: .5px; }
  .badge-video { background: rgba(99,102,241,.2); color: #818cf8; }
  .badge-image { background: rgba(16,185,129,.2); color: #34d399; }

  /* Script sourcing */
  .script-shot { background:var(--surface); border:1px solid var(--border); border-radius:10px; margin-bottom:16px; overflow:hidden; }
  .script-shot-header { display:flex; align-items:center; gap:12px; padding:12px 16px; border-bottom:1px solid var(--border); background:var(--surface2); cursor:pointer; }
  .shot-num { background:var(--accent); color:#fff; border-radius:50%; width:24px; height:24px; display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:800; flex-shrink:0; }
  .shot-label { font-size:13px; font-weight:600; flex:1; }
  .shot-query { font-size:11px; color:var(--muted); font-family:monospace; }
  .shot-count { font-size:11px; color:var(--accent); font-weight:600; white-space:nowrap; }
  .shot-chevron { color:var(--muted); font-size:12px; transition:transform .2s; }
  .shot-chevron.open { transform:rotate(90deg); }
  .script-clip-row { display:flex; align-items:center; gap:12px; padding:10px 16px; border-bottom:1px solid var(--border); transition:background .1s; }
  .script-clip-row:last-child { border-bottom:none; }
  .script-clip-row:hover { background:var(--surface2); }
  .clip-thumb { width:80px; height:45px; background:#000; border-radius:4px; object-fit:cover; flex-shrink:0; display:flex; align-items:center; justify-content:center; color:#555; font-size:18px; overflow:hidden; }
  .clip-info { flex:1; min-width:0; }
  .clip-name { font-size:12px; font-weight:600; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .clip-meta { font-size:11px; color:var(--muted); margin-top:2px; }
  .clip-desc { font-size:11px; color:var(--text); margin-top:3px; line-height:1.4; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
  .clip-score { font-size:11px; color:var(--success); font-weight:600; white-space:nowrap; }
  .script-analyzing { display:flex; flex-direction:column; align-items:center; justify-content:center; gap:12px; padding:48px; color:var(--muted); }
  .analyzing-pulse { width:48px; height:48px; border-radius:50%; border:3px solid var(--border); border-top-color:var(--accent); animation:spin .8s linear infinite; }

  /* History table */
  .history-table { width: 100%; border-collapse: collapse; }
  .history-table th { text-align: left; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; padding: 8px 12px; border-bottom: 1px solid var(--border); }
  .history-table td { padding: 11px 12px; border-bottom: 1px solid var(--border); font-size: 12px; vertical-align: middle; }
  .history-table tr:hover td { background: var(--surface2); }
  .status-badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
  .status-done    { background: rgba(34,197,94,.15); color: var(--success); }
  .status-error   { background: rgba(239,68,68,.15); color: var(--error); }
  .status-stopped { background: rgba(234,179,8,.15); color: var(--warn); }
  .status-running { background: rgba(249,115,22,.15); color: var(--accent); }

  /* Folder input with button */
  .folder-row { display: flex; gap: 8px; }
  .folder-row input { flex: 1; }

  /* Settings sections */
  .settings-section { margin-bottom: 30px; }
  .settings-section-title { font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid var(--border); }

  /* Alerts */
  .alert { padding: 12px 16px; border-radius: 8px; font-size: 13px; margin-bottom: 16px; }
  .alert-info    { background: rgba(99,102,241,.1); border: 1px solid rgba(99,102,241,.3); color: #a5b4fc; }
  .alert-success { background: rgba(34,197,94,.1);  border: 1px solid rgba(34,197,94,.3);  color: #86efac; }
  .alert-error   { background: rgba(239,68,68,.1);  border: 1px solid rgba(239,68,68,.3);  color: #fca5a5; }

  /* Misc */
  .pill-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
  .pill { padding: 5px 14px; border-radius: 20px; font-size: 12px; border: 1px solid var(--border); cursor: pointer; color: var(--muted); transition: all .15s; }
  .pill:hover { border-color: var(--accent); color: var(--accent); }
  .pill.active { background: var(--accent); border-color: var(--accent); color: #fff; }
  .divider { height: 1px; background: var(--border); margin: 24px 0; }
  .empty-state { text-align: center; padding: 60px 20px; color: var(--muted); }
  .empty-icon { font-size: 48px; margin-bottom: 12px; }
  .empty-text { font-size: 14px; }
  .job-running-bar { background: rgba(249,115,22,.1); border-top: 1px solid rgba(249,115,22,.2); padding: 10px 20px; display: flex; align-items: center; gap: 10px; font-size: 12px; color: var(--accent); }
  .spinner { width: 14px; height: 14px; border: 2px solid rgba(249,115,22,.3); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; flex-shrink: 0; }
  @keyframes spin { to { transform: rotate(360deg); } }
  /* Result card actions */
  .result-actions { display: flex; gap: 6px; margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }
  .result-btn { background: var(--surface2); border: 1px solid var(--border); border-radius: 5px; padding: 4px 10px; font-size: 11px; color: var(--muted); cursor: pointer; transition: all .15s; font-family: inherit; }
  .result-btn:hover { border-color: var(--accent); color: var(--accent); background: rgba(249,115,22,.08); }
  .result-btn:last-child { margin-left: auto; font-weight: 600; }

  ::-webkit-scrollbar { width: 6px; height: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: #3a3a3a; }
</style>
</head>
<body x-data="app()" x-init="init()">

<!-- ── Update banner ───────────────────────────────────────────────────── -->
<div x-data="updateBanner()" x-init="check()"
     x-show="info.available"
     :style="info.required ? 'background:#7f1d1d;border-color:#ef4444' : ''"
     style="display:none; position:fixed; top:0; left:0; right:0; z-index:9999;
            background:#1a1200; border-bottom:1px solid #ff8c00;
            padding:10px 20px; display:flex; align-items:center; gap:14px;
            font-size:13px; color:#e0e0e0;">
  <span style="font-size:16px" x-text="info.required ? '🚨' : '🎉'"></span>
  <span>
    <strong x-text="info.required ? 'Required update:' : 'Update available:'"></strong>
    METANAS <span x-text="info.latest"></span> is ready —
    <span x-show="info.release_notes" x-text="info.release_notes" style="color:#aaa"></span>
  </span>
  <button @click="
      $el.textContent = 'Updating…'; $el.disabled = true;
      fetch('/api/apply-update', {method:'POST'})
        .then(r => r.json().then(d => ({ok:r.ok, data:d})))
        .then(({ok, data}) => {
          if (ok) { $el.textContent = 'Restarting…'; setTimeout(() => location.reload(), 4000); }
          else { $el.textContent = data.error || 'Failed'; $el.disabled = false; }
        })
        .catch(() => { $el.textContent = 'Network error'; $el.disabled = false; })
    "
    style="margin-left:auto; background:#ff8c00; color:#0f0f0f; padding:6px 14px;
           border-radius:6px; font-weight:700; border:none; cursor:pointer; white-space:nowrap; flex-shrink:0; font-family:inherit; font-size:inherit;">
    Install <span x-text="info.latest"></span>
  </button>
  <button x-show="!info.required" @click="info.available=false"
          style="background:none;border:none;color:#888;cursor:pointer;font-size:18px;line-height:1;flex-shrink:0">✕</button>
</div>

<!-- ── Sidebar ─────────────────────────────────────────────────────────── -->
<div id="sidebar">
  <div class="logo">
    <div class="logo-mark">META<span style="color:#e5e5e5">NAS</span></div>
    <div class="logo-sub">Footage Metadata Tagger</div>
  </div>
  <nav>
    <div class="nav-item" :class="{active: view==='dashboard'}" @click="view='dashboard'; loadStats()">
      <span class="nav-icon">◈</span> Dashboard
    </div>
    <div class="nav-item" :class="{active: view==='tag'}" @click="view='tag'">
      <span class="nav-icon">▶</span> Tag Footage
    </div>
    <div class="nav-item" :class="{active: view==='search'}" @click="view='search'; if(!searchResults.length) loadRecent()">
      <span class="nav-icon">⌕</span> Search
    </div>
    <div class="nav-item" :class="{active: view==='script'}" @click="view='script'">
      <span class="nav-icon">✦</span> Script Source
    </div>
    <div class="nav-item" :class="{active: view==='history'}" @click="view='history'; loadHistory()">
      <span class="nav-icon">≡</span> History
    </div>
    <div class="nav-item" :class="{active: view==='settings'}" @click="view='settings'; loadSettings()">
      <span class="nav-icon">⚙</span> Settings
    </div>
  </nav>
  <div class="sidebar-footer">
    <span class="status-dot" :class="{off: !nasOk}"></span>
    <span style="font-size:11px;color:var(--muted)" x-text="nasOk ? 'NAS Connected' : 'NAS Offline'"></span>
  </div>
</div>

<!-- ── Main ───────────────────────────────────────────────────────────── -->
<div id="main">

  <!-- Running job bar -->
  <div class="job-running-bar" x-show="activeJobId && jobStatus==='running'" x-cloak>
    <div class="spinner"></div>
    <span>Tagging in progress — <a href="#" @click.prevent="view='tag'" style="color:inherit;text-decoration:underline">view live log</a></span>
    <button class="btn btn-sm btn-danger" style="margin-left:auto" @click="stopJob()">Stop</button>
  </div>

  <!-- ── Dashboard ─────────────────────────────────────────────────── -->
  <div class="view" :class="{active: view==='dashboard'}">
    <div class="view-header">
      <div class="view-title">Dashboard</div>
      <div class="view-sub">Overview of your tagged archive</div>
    </div>
    <div class="view-body">
      <div class="card-grid" style="margin-bottom:24px">
        <div class="stat-card">
          <div class="stat-num" x-text="stats.total ?? '—'"></div>
          <div class="stat-label">Files Tagged</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" x-text="stats.videos ?? '—'"></div>
          <div class="stat-label">Videos</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" x-text="stats.images ?? '—'"></div>
          <div class="stat-label">Images</div>
        </div>
        <div class="stat-card">
          <div class="stat-num" x-text="Object.keys(stats.cameras||{}).length || '—'"></div>
          <div class="stat-label">Cameras</div>
        </div>
      </div>

      <template x-if="Object.keys(stats.cameras||{}).length">
        <div class="card" style="margin-bottom:20px">
          <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:14px">Camera Breakdown</div>
          <template x-for="[cam, count] in Object.entries(stats.cameras||{})" :key="cam">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
              <div style="font-size:13px;min-width:160px" x-text="cam"></div>
              <div style="flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden">
                <div style="height:100%;background:var(--accent);border-radius:3px" :style="`width:${Math.round(count/stats.total*100)}%`"></div>
              </div>
              <div style="font-size:12px;color:var(--muted);min-width:40px;text-align:right" x-text="count"></div>
            </div>
          </template>
        </div>
      </template>

      <div class="card">
        <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:14px">Quick Actions</div>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn btn-primary" @click="view='tag'">▶ Tag New Footage</button>
          <button class="btn btn-ghost" @click="view='search'; loadRecent()">⌕ Search Archive</button>
        </div>
        <div style="margin-top:14px;font-size:12px;color:var(--muted)" x-show="stats.last_run">
          Last tagged: <span style="color:var(--text)" x-text="stats.last_run"></span>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Tag Footage ────────────────────────────────────────────────── -->
  <div class="view" :class="{active: view==='tag'}">
    <div class="view-header">
      <div class="view-title">Tag Footage</div>
      <div class="view-sub">Point at a folder on your NAS to analyse and tag all media files</div>
    </div>
    <div class="view-body">

      <div style="display:grid;grid-template-columns:1fr 320px;gap:24px;align-items:start" x-show="jobStatus !== 'running'">
        <!-- Left: form -->
        <div>
          <div class="form-row">
            <label>Folder Path</label>
            <div style="display:flex; gap:8px; align-items:center;">
              <input type="text" x-model="tagFolder" placeholder="/Volumes/Assort2025/Sheneller Projects/2026/MyProject" @keydown.enter="startTag()" @input="onFolderChange()" />
              <button @click="pickFolder('tagFolder', settings.nas_mount_path)" class="btn btn-ghost btn-sm" style="flex-shrink:0; white-space:nowrap;">📁 Browse</button>
            </div>
            <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:6px" x-show="folderHistory.length">
              <span style="font-size:11px;color:var(--muted);align-self:center">Recent:</span>
              <template x-for="f in folderHistory.slice(0,4)" :key="f">
                <span class="pill" @click="tagFolder=f; onFolderChange()" style="font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" x-text="f.split('/').slice(-2).join('/')"></span>
              </template>
            </div>
          </div>

          <div class="form-row-inline">
            <div class="form-row">
              <label>Vision Provider</label>
              <select x-model="tagProvider" @change="updateProviderInConfig()">
                <option value="gemini">Gemini 2.5 Flash</option>
                <option value="openai">GPT-4o Vision</option>
                <option value="ollama">Ollama (Local)</option>
              </select>
            </div>
          </div>

          <div class="form-row">
            <div class="toggle-wrap" @click="tagReprocess=!tagReprocess">
              <div class="toggle" :class="{on: tagReprocess}"></div>
              <span class="toggle-label">Re-process already tagged files</span>
            </div>
            <div style="font-size:11px;color:var(--muted);margin-top:6px;padding-left:46px">When off, existing XMP files and DB records are never overwritten</div>
          </div>

          <!-- ── Project Database ────────────────────────────── -->
          <div style="border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-bottom:18px;background:var(--surface2)">
            <div style="font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--muted);margin-bottom:12px;font-weight:600">Project Database</div>

            <div class="form-row" style="margin-bottom:10px">
              <label>Project DB filename</label>
              <input type="text" x-model="projectDbName"
                placeholder="auto-filled from folder name"
                style="font-family:monospace;font-size:12px" />
            </div>

            <div class="form-row" style="margin-bottom:10px">
              <label>Project DB save location</label>
              <div style="display:flex; gap:8px; align-items:center;">
                <input type="text" x-model="projectDbFolder"
                  placeholder="footage-tagger/project_dbs/"
                  style="font-family:monospace;font-size:12px" />
                <button @click="pickFolder('projectDbFolder', '~')" class="btn btn-ghost btn-sm" style="flex-shrink:0; white-space:nowrap;">📁 Browse</button>
              </div>
              <div style="font-size:11px;color:var(--muted);margin-top:4px">
                Where this project's <code style="color:var(--accent)">.db</code> file will be saved — easy to share or archive per project
              </div>
            </div>

            <div class="form-row" style="margin-bottom:6px">
              <div class="toggle-wrap" @click="saveToMain=!saveToMain">
                <div class="toggle" :class="{on: saveToMain}"></div>
                <span class="toggle-label">Save to main archive database</span>
              </div>
              <div style="font-size:11px;color:var(--muted);margin-top:5px;padding-left:46px">
                Turn <b>off</b> for test footage or one-off projects you don't want in your master archive
              </div>
            </div>

            <div x-show="!saveToMain && !projectDbName.trim()" class="alert alert-error" style="margin-top:8px;margin-bottom:0;font-size:11px">
              ⚠️ Both databases disabled — nothing will be saved. Enter a project DB name or enable main archive.
            </div>
            <div x-show="!saveToMain && projectDbName.trim()" style="font-size:11px;color:var(--warn);margin-top:6px">
              ℹ️ Results will only be saved to the project DB — not the main archive
            </div>
          </div>

          <div x-show="tagError" class="alert alert-error" x-text="tagError"></div>

          <button class="btn btn-primary" @click="startTag()"
            :disabled="!tagFolder.trim() || (!saveToMain && !projectDbName.trim())">
            ▶ Start Tagging
          </button>
        </div>

        <!-- Right: info panel -->
        <div class="card" style="font-size:12px;line-height:1.7;color:var(--muted)">
          <div style="font-weight:700;color:var(--text);margin-bottom:10px;font-size:13px">How it works</div>
          <p style="margin-bottom:10px">METANAS scans the folder recursively for MP4, MOV, JPG, and ARW files. For each file it:</p>
          <ol style="padding-left:16px">
            <li style="margin-bottom:6px">Extracts keyframes with scene detection</li>
            <li style="margin-bottom:6px">Sends frames to your chosen AI vision model</li>
            <li style="margin-bottom:6px">Transcribes audio (Whisper)</li>
            <li style="margin-bottom:6px">Writes XMP sidecar + embeds metadata into the file</li>
            <li>Saves everything to the searchable database</li>
          </ol>
          <div class="divider"></div>
          <div style="color:var(--accent);font-weight:600">Gemini 2.5 Flash ≈ $0.01–0.05 per project</div>
        </div>
      </div>

      <!-- Live log -->
      <div x-show="jobStatus === 'running' || logLines.length">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <div style="font-size:13px;font-weight:600">
            <template x-if="jobStatus==='running'">
              <span style="color:var(--accent)"><span class="spinner" style="display:inline-block;vertical-align:middle;margin-right:6px"></span>Running…</span>
            </template>
            <template x-if="jobStatus==='done'">
              <span style="color:var(--success)">✓ Complete</span>
            </template>
            <template x-if="jobStatus==='error'">
              <span style="color:var(--error)">✗ Error</span>
            </template>
            <template x-if="jobStatus==='stopped'">
              <span style="color:var(--warn)">⏹ Stopped</span>
            </template>
          </div>
          <div style="display:flex;gap:8px">
            <button class="btn btn-sm btn-danger" x-show="jobStatus==='running'" @click="stopJob()">Stop</button>
            <button class="btn btn-sm btn-ghost" x-show="jobStatus!=='running'" @click="resetTag()">← New Job</button>
          </div>
        </div>
        <div id="log-wrap" x-ref="logWrap">
          <template x-for="(l, i) in logLines" :key="i">
            <div class="log-line" :class="logClass(l)" x-text="l"></div>
          </template>
          <div x-show="jobStatus==='running'" class="log-line" style="color:var(--muted)">_</div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── Search ─────────────────────────────────────────────────────── -->
  <div class="view" :class="{active: view==='search'}">
    <div class="view-header">
      <div class="view-title">Search Archive</div>
    <!-- Filters Panel -->
    <div style="margin:20px 0 0 0; padding:0;">
      <button @click="showFilters = !showFilters; if (showFilters) loadFilterOptions();" class="btn btn-ghost btn-sm" style="margin-bottom:12px;">
        ⊞ Filters <span class="badge" style="position:absolute; right:-8px; top:-8px; background:var(--accent); color:var(--bg); border-radius:50%; width:20px; height:20px; display:flex; align-items:center; justify-content:center; font-size:10px; font-weight:700;" x-show="Object.values(filters).some(v => v)" x-text="Object.values(filters).filter(v => v).length"></span>
      </button>
      <div x-show="showFilters" style="background:var(--surface2); border:1px solid var(--border); border-radius:8px; padding:16px; display:grid; grid-template-columns:repeat(auto-fit, minmax(180px, 1fr)); gap:14px; margin-bottom:16px;">
        <div>
          <label>File Type</label>
          <select x-model="filters.file_type" @change="doSearch()">
            <option value="">All</option>
            <option value="video">Video</option>
            <option value="image">Image</option>
          </select>
        </div>
        <div>
          <label>Camera</label>
          <select x-model="filters.camera" @change="doSearch()">
            <option value="">All</option>
            <template x-for="cam in filterOptions.cameras || []" :key="cam">
              <option :value="cam" x-text="cam"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Shot Type</label>
          <select x-model="filters.shot_type" @change="doSearch()">
            <option value="">All</option>
            <template x-for="st in filterOptions.shot_types || []" :key="st">
              <option :value="st" x-text="st"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Camera Movement</label>
          <select x-model="filters.camera_movement" @change="doSearch()">
            <option value="">All</option>
            <template x-for="cm in filterOptions.camera_movements || []" :key="cm">
              <option :value="cm" x-text="cm"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Time of Day</label>
          <select x-model="filters.time_of_day" @change="doSearch()">
            <option value="">All</option>
            <template x-for="t in filterOptions.time_of_day || []" :key="t">
              <option :value="t" x-text="t"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Audio Type</label>
          <select x-model="filters.audio_type" @change="doSearch()">
            <option value="">All</option>
            <template x-for="a in filterOptions.audio_types || []" :key="a">
              <option :value="a" x-text="a"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Color Palette</label>
          <select x-model="filters.color_palette" @change="doSearch()">
            <option value="">All</option>
            <template x-for="cp in filterOptions.color_palettes || []" :key="cp">
              <option :value="cp" x-text="cp"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Setting/Location</label>
          <select x-model="filters.setting" @change="doSearch()">
            <option value="">All</option>
            <template x-for="s in filterOptions.settings || []" :key="s">
              <option :value="s" x-text="s"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Mood</label>
          <select x-model="filters.mood" @change="doSearch()">
            <option value="">All</option>
            <template x-for="m in filterOptions.moods || []" :key="m">
              <option :value="m" x-text="m"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Lighting</label>
          <select x-model="filters.lighting" @change="doSearch()">
            <option value="">All</option>
            <template x-for="l in filterOptions.lightings || []" :key="l">
              <option :value="l" x-text="l"></option>
            </template>
          </select>
        </div>
        <div>
          <label>File Extension</label>
          <select x-model="filters.file_ext" @change="doSearch()">
            <option value="">All</option>
            <template x-for="ex in filterOptions.extensions || []" :key="ex">
              <option :value="ex" x-text="ex"></option>
            </template>
          </select>
        </div>
        <div>
          <label>Frame Rate</label>
          <select x-model="filters.fps" @change="doSearch()">
            <option value="">All</option>
            <template x-for="f in filterOptions.fps_options || []" :key="f">
              <option :value="f" x-text="f + ' fps'"></option>
            </template>
          </select>
        </div>
        <div>
          <label>People in Shot</label>
          <select x-model="filters.has_people" @change="doSearch()">
            <option value="">All</option>
            <option value="yes">With People</option>
            <option value="no">No People</option>
          </select>
        </div>
        <div style="display:flex; align-items:flex-end;">
          <button @click="clearFilters()" class="btn btn-ghost btn-sm" style="width:100%;">Clear Filters</button>
        </div>
      </div>
    </div>

      <div class="view-sub">Full-text search across all tagged footage</div>
    </div>
    <div class="view-body">

      <!-- ── Database selector ─────────────────────────────── -->
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;padding:10px 14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;">
        <span style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;white-space:nowrap">Search in:</span>
        <div class="pill-row" style="margin:0;flex:1;flex-wrap:nowrap;overflow-x:auto">
          <span class="pill" :class="{active:searchDb===''}" @click="searchDb='';doSearch()" style="white-space:nowrap">
            🗄 Main Archive
          </span>
          <template x-for="db in projectDbs" :key="db.path">
            <span class="pill" :class="{active:searchDb===db.path}" @click="searchDb=db.path;doSearch()"
              style="white-space:nowrap" :title="db.path">
              📁 <span x-text="db.name.replace('.db','')"></span>
              <span style="opacity:.6;font-size:10px;margin-left:3px" x-text="'('+db.count+')'"></span>
            </span>
          </template>
        </div>
        <button class="btn btn-ghost btn-sm" @click="loadProjectDbs()" title="Refresh project DB list" style="flex-shrink:0">↻</button>
      </div>

      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input type="text" x-model="searchQuery" placeholder="golden hour boat, drone aerial, old lady smiling…" @keydown.enter="doSearch()" style="flex:1" />
        <button class="btn btn-primary" @click="doSearch()">Search</button>
      </div>
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:16px;">
        <div class="pill-row">
          <span class="pill" :class="{active:searchType===''}" @click="searchType='';doSearch()">All</span>
          <span class="pill" :class="{active:searchType==='video'}" @click="searchType='video';doSearch()">Videos</span>
          <span class="pill" :class="{active:searchType==='image'}" @click="searchType='image';doSearch()">Images</span>
        </div>
        <div class="pill-row" style="gap:4px;">
          <span class="pill" :class="{active:!smartSearch}" @click="smartSearch=false;doSearch()" style="font-size:11px;">🔍 Exact</span>
          <span class="pill" :class="{active:smartSearch}" @click="smartSearch=true;doSearch()" style="font-size:11px;">✦ Smart</span>
        </div>
      </div>
      <!-- Smart search expansion info -->
      <div x-show="smartSearch && expandedInfo" style="font-size:11px;color:var(--muted);margin-bottom:12px;padding:8px 12px;background:rgba(240,112,32,.06);border:1px solid rgba(240,112,32,.15);border-radius:8px;">
        ✦ AI expanded your search: <span x-text="expandedInfo" style="color:var(--accent);"></span>
      </div>

      <!-- ── Send-to destination ──────────────────────────────── -->
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:14px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;">
        <span style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;flex-shrink:0">→ Send to:</span>
        <input type="text" x-model="settings.send_folder"
          placeholder="Pick a destination folder…"
          style="flex:1;font-size:12px;font-family:monospace;padding:5px 10px;" />
        <button @click="pickFolder('settings.send_folder', '~')" class="btn btn-ghost btn-sm" style="flex-shrink:0;white-space:nowrap;">📁 Browse</button>
        <span x-show="settings.send_folder" style="font-size:11px;color:var(--success);flex-shrink:0">✓ Set</span>
        <span x-show="!settings.send_folder" style="font-size:11px;color:var(--warn);flex-shrink:0">Not set</span>
      </div>

      <div x-show="searchLoading" style="color:var(--muted);font-size:13px">Searching…</div>
      <div x-show="searchError" class="alert alert-error" x-text="searchError"></div>

      <div x-show="!searchLoading && !searchError && searchResults.length===0 && searchQuery" class="empty-state">
        <div class="empty-icon">⌕</div>
        <div class="empty-text">No results for "<span x-text="searchQuery"></span>"</div>
      </div>

      <div x-show="searchResults.length" style="font-size:11px;color:var(--muted);margin-bottom:12px">
        <span x-text="searchResults.length"></span> result<span x-show="searchResults.length!==1">s</span>
        <span x-show="searchQuery"> for "<span x-text="searchQuery" style="color:var(--text)"></span>"</span>
      </div>

      <div class="result-grid">
        <template x-for="r in searchResults" :key="r.file_path">
          <div class="result-card" :title="r.file_path">
<div x-show="r.file_type === 'video'" class="scrub-wrap" @mouseenter="loadThumbs(r)" @mousemove="scrubMove(r, $event)" style="margin-bottom:12px;">
              <template x-for="(thumb, ti) in r._thumbs || []" :key="ti">
                <img class="scrub-img" :class="{active: ti === r._activeThumb}" :src="'/api/thumbnail?path=' + encodeURIComponent(thumb)" style="object-fit:cover;">
              </template>
              <div x-show="!(r._thumbs && r._thumbs.length)" class="scrub-placeholder">
                <div style="font-size:16px;">🎬</div>
                <div>Preview available after retagging</div>
              </div>
              <div class="scrub-bar" :style="{width: (r._scrubPct || 0) + '%'}"></div>
              <div class="scrub-timecode" x-text="(r._scrubPct || 0) + '%'"></div>
            </div>
                        <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;margin-bottom:4px">
              <div class="result-filename" x-text="r.filename"></div>
              <span class="badge" :class="r.file_type==='video' ? 'badge-video' : 'badge-image'" x-text="r.file_type"></span>
            </div>
            <div class="result-folder" x-text="r.folder"></div>
            <div class="result-desc" x-text="r.description || '(no description)'"></div>
            <div style="font-size:11px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap;margin-bottom:6px">
              <span x-show="r.shot_type" x-text="'🎬 ' + r.shot_type"></span>
              <span x-show="r.camera_movement" x-text="'🎥 ' + r.camera_movement"></span>
              <span x-show="r.time_of_day && r.time_of_day !== 'unknown'" x-text="'🕐 ' + r.time_of_day"></span>
              <span x-show="r.audio_type && r.audio_type !== 'unknown'" x-text="'🔊 ' + r.audio_type"></span>
              <span x-show="r.camera_model && r.camera_model!=='Unknown'" x-text="'📷 ' + r.camera_model"></span>
              <span x-show="r.setting" x-text="'📍 ' + r.setting"></span>
            </div>
            <div class="tag-row">
              <template x-for="p in r.persons" :key="p">
                <span class="tag person" x-text="p"></span>
              </template>
              <template x-for="t in r.tags.slice(0,5)" :key="t">
                <span class="tag" x-text="t"></span>
              </template>
              <template x-for="t in (r.mood_tags||[]).slice(0,3)" :key="'m'+t">
                <span class="tag" style="background:var(--accent-dim,rgba(255,140,0,.15));color:var(--accent)" x-text="t"></span>
              </template>
            </div>
            <div class="result-actions">
              <button class="result-btn" title="Reveal in Finder" @click.stop="revealFile(r.file_path)">📂</button>
              <button class="result-btn" title="Open in Premiere Pro" @click.stop="openInPremiere(r.file_path)">🎬</button>
              <button class="result-btn" :title="settings.send_folder ? 'Send to ' + settings.send_folder : 'Set a destination folder above first'" :style="!settings.send_folder ? 'opacity:.45;cursor:not-allowed' : ''" @click.stop="settings.send_folder ? sendToFolder(r.file_path, r) : null">→ Send</button>
            </div>
            <div class="result-toast" x-show="r._toast" x-text="r._toast" style="font-size:11px;color:var(--success);margin-top:6px"></div>
          </div>
        </template>
      </div>
    </div>
  </div>

  <!-- ── Script Source ────────────────────────────────────────────── -->
  <div class="view" :class="{active: view==='script'}">
    <div class="view-header">
      <div class="view-title">Script Source</div>
      <div class="view-sub">Paste a VO script or shot list — AI breaks it into shots and finds matching clips from your archive</div>
    </div>
    <div class="view-body">

      <!-- Input area -->
      <div x-show="scriptState === 'idle' || scriptState === 'error'" style="margin-bottom:20px;">
        <div class="card" style="margin-bottom:16px;">
          <div style="font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px;font-weight:600">VO Script / Shot Description</div>
          <textarea x-model="scriptText" rows="8"
            placeholder="Paste your full VO script or shot list here. Be as descriptive as possible — include mood, location, camera style, subject matter, time of day etc.

Example:
We open on a sweeping aerial shot of the Kuala Lumpur skyline at golden hour, the KLCC towers gleaming. Cut to close-up street level footage of a busy hawker stall — steam rising, vendor hands at work. We then need a wide establishing shot of a traditional Malaysian home interior, warm natural light. Next a slow tracking shot following a couple walking through a night market, neon lights reflecting on wet pavement. Finally we close on an intimate close-up of local food being plated, shallow depth of field."
            style="resize:vertical; line-height:1.6; font-size:13px;"></textarea>
          <div style="margin-top:10px; display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
            <div style="flex:1; min-width:200px;">
              <label style="margin-bottom:4px;">Search database</label>
              <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;">
                <span class="pill" :class="{active:scriptSearchDb===''}" @click="scriptSearchDb=''" style="font-size:11px;">🗄 Main Archive</span>
                <template x-for="db in projectDbs" :key="db.path">
                  <span class="pill" :class="{active:scriptSearchDb===db.path}" @click="scriptSearchDb=db.path" style="font-size:11px;white-space:nowrap;" x-text="'📁 ' + db.name.replace('.db','')"></span>
                </template>
              </div>
            </div>
            <div>
              <label style="margin-bottom:4px;">Max clips per shot</label>
              <select x-model="scriptMaxPerShot" style="width:80px;">
                <option value="3">3</option>
                <option value="5">5</option>
                <option value="8">8</option>
                <option value="10">10</option>
              </select>
            </div>
          </div>

          <!-- ── Filter row ─────────────────────────────────── -->
          <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap; margin-top:14px;">
            <div>
              <label style="margin-bottom:4px;">Media type</label>
              <div style="display:flex;gap:6px;margin-top:4px;">
                <span class="pill" :class="{active:scriptMediaFilter==='all'}"   @click="scriptMediaFilter='all'"   style="font-size:11px;">All</span>
                <span class="pill" :class="{active:scriptMediaFilter==='video'}" @click="scriptMediaFilter='video'" style="font-size:11px;">🎬 Video</span>
                <span class="pill" :class="{active:scriptMediaFilter==='image'}" @click="scriptMediaFilter='image'" style="font-size:11px;">📷 Image</span>
              </div>
            </div>
            <div>
              <label style="margin-bottom:4px;">Detection mode</label>
              <div style="display:flex;gap:6px;margin-top:4px;">
                <span class="pill" :class="{active:scriptDetectionMode==='both'}"  @click="scriptDetectionMode='both'"  style="font-size:11px;">Scene + Audio</span>
                <span class="pill" :class="{active:scriptDetectionMode==='scene'}" @click="scriptDetectionMode='scene'" style="font-size:11px;">Scene only</span>
                <span class="pill" :class="{active:scriptDetectionMode==='audio'}" @click="scriptDetectionMode='audio'" style="font-size:11px;">Audio only</span>
              </div>
            </div>
            <div>
              <label style="margin-bottom:4px;">Results to show</label>
              <select x-model="scriptResultsLimit" style="width:80px;">
                <option value="5">5</option>
                <option value="10">10</option>
                <option value="15">15</option>
              </select>
            </div>
            <div>
              <label style="margin-bottom:4px;">Search mode</label>
              <div style="display:flex;gap:4px;margin-top:4px;">
                <span class="pill" :class="{active:!scriptSmartSearch}" @click="scriptSmartSearch=false" style="font-size:11px;">🔍 Exact</span>
                <span class="pill" :class="{active:scriptSmartSearch}" @click="scriptSmartSearch=true" style="font-size:11px;">✦ Smart</span>
              </div>
            </div>
          </div>
        </div>

        <div x-show="scriptError" class="alert alert-error" x-text="scriptError" style="margin-bottom:12px;"></div>

        <button class="btn btn-primary" @click="analyseScript()"
          :disabled="!scriptText.trim()"
          style="font-size:14px; padding:11px 28px;">
          ✦ Analyse &amp; Source Clips
        </button>
        <span style="font-size:11px;color:var(--muted);margin-left:14px;">Uses your configured AI model to parse the script</span>
      </div>

      <!-- Analysing spinner -->
      <div x-show="scriptState === 'analysing'" class="script-analyzing">
        <div class="analyzing-pulse"></div>
        <div style="font-weight:600;color:var(--text);">Analysing script…</div>
        <div style="font-size:12px;" x-text="scriptStatusMsg"></div>
      </div>

      <!-- Results -->
      <div x-show="scriptState === 'done'">
        <!-- Toolbar -->
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:10px;">
          <div>
            <span style="font-size:14px;font-weight:700;" x-text="scriptShots.length + ' shots identified'"></span>
            <span style="font-size:12px;color:var(--muted);margin-left:10px;"
              x-text="scriptShots.reduce((a,s)=>a+s.results.length,0) + ' clips matched'"></span>
          </div>
          <div style="display:flex;gap:8px;">
            <button class="btn btn-ghost btn-sm" @click="scriptState='idle'; scriptShots=[];">← New Script</button>
            <button class="btn btn-primary btn-sm" @click="exportScriptResults()">↓ Export List</button>
          </div>
        </div>

        <!-- Send-to bar (reused from search) -->
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);border-radius:8px;">
          <span style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;flex-shrink:0">→ Send to:</span>
          <input type="text" x-model="settings.send_folder" placeholder="Pick a destination folder…"
            style="flex:1;font-size:12px;font-family:monospace;padding:5px 10px;" />
          <button @click="pickFolder('settings.send_folder', '~')" class="btn btn-ghost btn-sm" style="flex-shrink:0;white-space:nowrap;">📁 Browse</button>
          <span x-show="settings.send_folder" style="font-size:11px;color:var(--success);flex-shrink:0">✓ Set</span>
          <span x-show="!settings.send_folder" style="font-size:11px;color:var(--warn);flex-shrink:0">Not set</span>
        </div>

        <!-- Shot groups -->
        <template x-for="(shot, si) in scriptShots" :key="si">
          <div class="script-shot">
            <div class="script-shot-header" @click="shot._open = !shot._open">
              <div class="shot-num" x-text="si + 1"></div>
              <div style="flex:1;min-width:0;">
                <div class="shot-label" x-text="shot.label"></div>
                <div class="shot-query" x-text="'Search: &quot;' + shot.query + '&quot;'"></div>
              </div>
              <div class="shot-count" x-text="shot.results.length + ' clips'"></div>
              <div class="shot-chevron" :class="{open: shot._open}">▶</div>
            </div>

            <div x-show="shot._open">
              <template x-if="shot.results.length === 0">
                <div style="padding:16px 20px;font-size:12px;color:var(--muted);">No matching clips found — try retagging more footage or broadening your script description.</div>
              </template>
              <template x-for="(clip, ci) in shot.results" :key="ci">
                <div class="script-clip-row">
                  <!-- Thumbnail placeholder -->
                  <div class="clip-thumb">
                    <template x-if="clip.file_type==='video'">
                      <div style="font-size:22px;display:flex;align-items:center;justify-content:center;width:100%;height:100%;">🎬</div>
                    </template>
                    <template x-if="clip.file_type==='image'">
                      <div style="font-size:22px;display:flex;align-items:center;justify-content:center;width:100%;height:100%;">🖼</div>
                    </template>
                  </div>
                  <!-- Info -->
                  <div class="clip-info">
                    <div class="clip-name" x-text="clip.filename"></div>
                    <div class="clip-meta">
                      <span x-show="clip.camera_model && clip.camera_model!=='Unknown'" x-text="clip.camera_model + ' · '"></span>
                      <span x-show="clip.shot_type" x-text="clip.shot_type + ' · '"></span>
                      <span x-show="clip.setting" x-text="clip.setting"></span>
                    </div>
                    <div class="clip-desc" x-text="clip.description"></div>
                  </div>
                  <!-- Score badge -->
                  <div style="display:flex;flex-direction:column;align-items:flex-end;gap:6px;flex-shrink:0;">
                    <span class="clip-score" x-text="'Match ' + (clip._score || '—')"></span>
                    <div style="display:flex;gap:6px;">
                      <button class="result-btn btn-sm" title="Reveal in Finder" @click="revealFile(clip.file_path)">📂</button>
                      <button class="result-btn btn-sm"
                        :title="settings.send_folder ? 'Send to ' + settings.send_folder : 'Set destination above'"
                        :style="!settings.send_folder ? 'opacity:.45;cursor:not-allowed' : ''"
                        @click="settings.send_folder ? sendToFolder(clip.file_path, clip) : null">→ Send</button>
                    </div>
                    <div x-show="clip._toast" x-text="clip._toast" style="font-size:10px;color:var(--success);"></div>
                  </div>
                </div>
              </template>
            </div>
          </div>
        </template>
      </div>

    </div>
  </div>

  <!-- ── History ────────────────────────────────────────────────────── -->
  <div class="view" :class="{active: view==='history'}">
    <div class="view-header">
      <div class="view-title">Job History</div>
      <div class="view-sub">Past tagging runs</div>
    </div>
    <div class="view-body">
      <div x-show="!historyRows.length" class="empty-state">
        <div class="empty-icon">≡</div>
        <div class="empty-text">No runs yet — tag some footage first</div>
      </div>
      <table class="history-table" x-show="historyRows.length">
        <thead>
          <tr>
            <th>Folder</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Summary</th>
          </tr>
        </thead>
        <tbody>
          <template x-for="h in historyRows" :key="h.job_id">
            <tr>
              <td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="h.folder" x-text="h.folder ? h.folder.split('/').slice(-2).join('/') : '—'"></td>
              <td x-text="h.started || '—'"></td>
              <td x-text="duration(h.started, h.ended)"></td>
              <td><span class="status-badge" :class="'status-' + (h.status||'error')" x-text="h.status || '—'"></span></td>
              <td style="max-width:300px;color:var(--muted);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" :title="h.summary" x-text="h.summary || '—'"></td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ── Settings ───────────────────────────────────────────────────── -->
  <div class="view" :class="{active: view==='settings'}">
    <div class="view-header">
      <div class="view-title">Settings</div>
      <div class="view-sub">API keys and configuration</div>
    </div>
    <div class="view-body" style="max-width:640px">
      <div x-show="settingsSaved" class="alert alert-success">Settings saved ✓</div>

      <div class="settings-section">
        <div class="settings-section-title">Vision API Keys</div>
        <div class="form-row">
          <label>Gemini API Key</label>
          <div style="display:flex;gap:8px;align-items:center;">
            <input type="password" x-model="settings.gemini_api_key"
              :placeholder="settings.gemini_api_key && settings.gemini_api_key.includes('••') ? 'Key saved — paste a new one to replace' : 'AIza…'"
              autocomplete="off" style="flex:1" />
            <span x-show="settings.gemini_api_key && settings.gemini_api_key.includes('••')"
              style="font-size:11px;color:var(--success);white-space:nowrap;flex-shrink:0">✓ Saved</span>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:5px">Get yours at <a href="https://aistudio.google.com/apikey" target="_blank" style="color:var(--accent)">aistudio.google.com/apikey</a>. Your key is stored locally and never sent anywhere except Google's API.</div>
        </div>
        <div class="form-row">
          <label>OpenAI API Key</label>
          <div style="display:flex;gap:8px;align-items:center;">
            <input type="password" x-model="settings.openai_api_key"
              :placeholder="settings.openai_api_key && settings.openai_api_key.includes('••') ? 'Key saved — paste a new one to replace' : 'sk-proj-…'"
              autocomplete="off" style="flex:1" />
            <span x-show="settings.openai_api_key && settings.openai_api_key.includes('••')"
              style="font-size:11px;color:var(--success);white-space:nowrap;flex-shrink:0">✓ Saved</span>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:5px">Get yours at <a href="https://platform.openai.com/api-keys" target="_blank" style="color:var(--accent)">platform.openai.com/api-keys</a>. Your key is stored locally and never sent anywhere except OpenAI's API.</div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Vision Provider</div>
        <div class="form-row">
          <label>Default Provider</label>
          <select x-model="settings.vision_provider">
            <option value="gemini">Gemini 2.5 Flash (recommended)</option>
            <option value="openai">GPT-4o Vision</option>
            <option value="ollama">Ollama (local, free)</option>
          </select>
        </div>
        <div class="form-row-inline">
          <div class="form-row">
            <label>Gemini Model</label>
            <input type="text" x-model="settings.gemini_vision_model" placeholder="gemini-2.5-flash" />
          </div>
          <div class="form-row">
            <label>OpenAI Model</label>
            <input type="text" x-model="settings.openai_vision_model" placeholder="gpt-4o" />
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Storage Paths</div>
        <div class="form-row">
          <label>NAS Mount Path</label>
          <div style="display:flex; gap:8px; align-items:flex-start;">
      <input type="text" x-model="settings.nas_mount_path" placeholder="/Volumes/YourNAS" />
      <button @click="pickFolder('settings.nas_mount_path', '~')" class="btn btn-ghost btn-sm" style="flex-shrink:0; white-space:nowrap; margin-top:20px;">📁 Browse</button>
    </div>

        </div>
        <div class="form-row">
          <label>Database Path</label>
          <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <input type="text" x-model="settings.db_path" placeholder="/Users/You/footage_metadata.db" style="flex:1;min-width:200px;" />
            <button @click="pickFile('settings.db_path', '~', ['db'])" class="btn btn-ghost btn-sm" style="flex-shrink:0; white-space:nowrap;">🗄 Select Existing DB</button>
            <button @click="pickDbFolder()" class="btn btn-ghost btn-sm" style="flex-shrink:0; white-space:nowrap;">📁 Choose Folder</button>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:4px">
            <b>Select Existing DB</b> to point to a .db file you already have, or <b>Choose Folder</b> to pick where a new <code>footage_metadata.db</code> will be created on first tag run.
          </div>
          <div x-show="settings.db_path && !dbFileExists" style="font-size:11px;color:var(--warn);margin-top:4px">
            ⚠ Database file does not exist yet — it will be created automatically when you first tag footage.
          </div>
        </div>
        <div class="form-row">
          <label>Send to Folder</label>
          <div style="display:flex; gap:8px; align-items:flex-start;">
      <input type="text" x-model="settings.send_folder" placeholder="/Users/You/Desktop/Premiere Bin" />
      <button @click="pickFolder('settings.send_folder', '~')" class="btn btn-ghost btn-sm" style="flex-shrink:0; white-space:nowrap; margin-top:20px;">📁 Browse</button>
    </div>
    <div class="form-row">
      <label>Thumbnails Path</label>
      <input type="text" x-model="settings.thumbnails_dir" placeholder="(auto — next to app.py)" />
      <div style="font-size:11px; color:var(--muted); margin-top:4px;">Where scrubbing preview frames are saved. Leave blank for default.</div>
    </div>


          <div style="font-size:11px;color:var(--muted);margin-top:5px">Files sent from Search results are copied here. Set this as a watched folder in Premiere Pro for instant bin access.</div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Processing Options</div>
        <div class="form-row-inline">
          <div class="form-row">
            <label>Whisper Model</label>
            <select x-model="settings.whisper_model">
              <option value="tiny">tiny (fastest)</option>
              <option value="base">base</option>
              <option value="small">small</option>
              <option value="medium">medium (recommended)</option>
              <option value="large">large (most accurate)</option>
            </select>
          </div>
          <div class="form-row">
            <label>Max Scenes / Clip</label>
            <input type="text" x-model="settings.max_scenes_per_clip" placeholder="8" />
          </div>
        </div>
        <div class="form-row">
          <div class="toggle-wrap" @click="settings.transcribe_audio=!settings.transcribe_audio">
            <div class="toggle" :class="{on: settings.transcribe_audio}"></div>
            <span class="toggle-label">Transcribe audio (Whisper)</span>
          </div>
        </div>
        <div class="form-row">
          <div class="toggle-wrap" @click="settings.process_images=!settings.process_images">
            <div class="toggle" :class="{on: settings.process_images}"></div>
            <span class="toggle-label">Process image files (JPG / ARW)</span>
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Person Recognition</div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:12px">
          Add a reference photo for anyone you want the AI to identify in footage. The image must be accessible on this machine.
        </div>
        <template x-for="(p, i) in settings.reference_persons||[]" :key="i">
          <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
            <input type="text" x-model="p.name" placeholder="Person name" style="width:150px;flex-shrink:0" />
            <input type="text" x-model="p.reference_image" placeholder="/path/to/reference_photo.jpg" style="flex:1;" />
            <button class="btn btn-sm btn-ghost" style="flex-shrink:0;white-space:nowrap;"
              @click="pickFile('settings.reference_persons['+i+'].reference_image', '~', ['jpg','jpeg','png','heic'])">📷 Browse</button>
            <button class="btn btn-sm btn-ghost" style="flex-shrink:0;color:var(--error);" @click="settings.reference_persons.splice(i,1)">✕</button>
          </div>
        </template>
        <button class="btn btn-sm btn-ghost" style="margin-top:4px;"
          @click="settings.reference_persons = [...(settings.reference_persons||[]), {name:'',reference_image:''}]">
          + Add Person
        </button>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Mac Power</div>
        <div class="form-row">
          <div class="toggle-wrap" @click="settings.caffeinate=!settings.caffeinate">
            <div class="toggle" :class="{on: settings.caffeinate}"></div>
            <span class="toggle-label">Caffeinate during tagging</span>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:6px;padding-left:46px">
            Prevents your Mac from sleeping while a tagging job is running. Automatically stops when the job finishes.
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">Metadata Output</div>
        <div style="font-size:12px;color:var(--muted);margin-bottom:14px">
          Choose how metadata is saved after each clip is tagged. Both options can be on at the same time.
        </div>

        <div class="form-row">
          <div class="toggle-wrap" @click="settings.write_xmp_sidecar=!settings.write_xmp_sidecar">
            <div class="toggle" :class="{on: settings.write_xmp_sidecar}"></div>
            <span class="toggle-label">Write XMP sidecar files</span>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:6px;padding-left:46px">
            Creates a small <code>.xmp</code> file next to each video in the same folder. Think of it as a Post-it note attached to the clip — readable by Adobe Bridge, Premiere Pro, and DaVinci Resolve without touching the original video file. Safe to turn off if you don't use Adobe tools or want a clean folder.
          </div>
        </div>

        <div class="form-row" style="margin-top:12px">
          <div class="toggle-wrap" @click="settings.embed_metadata=!settings.embed_metadata">
            <div class="toggle" :class="{on: settings.embed_metadata}"></div>
            <span class="toggle-label">Embed metadata into video files</span>
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:6px;padding-left:46px">
            Writes the description, keywords, shot type, and camera movement directly inside the video file's metadata container (using ExifTool). The video image and audio are never touched — only the invisible metadata layer. This means the clip carries its tags with it wherever you copy it, and apps like QuickTime, VLC, and Finder's Get Info will show the description. Recommended on.
          </div>
        </div>
      </div>

      <div class="settings-section">
        <div class="settings-section-title">License &amp; Version</div>
        <div x-data="{licStatus: null, updateInfo: null, checking: false}" x-init="
          fetch('/api/license-status').then(r=>r.json()).then(d=>{licStatus=d});
          fetch('/api/update-status').then(r=>r.json()).then(d=>{updateInfo=d});
        ">
          <template x-if="licStatus && licStatus.licensed">
            <div>
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                <span style="color:var(--success);font-size:18px">✓</span>
                <span style="font-weight:600;font-size:14px">METANAS is activated</span>
              </div>
              <div style="font-size:12px;color:var(--muted);margin-bottom:4px" x-text="licStatus.email ? 'Licensed to: ' + licStatus.email : ''"></div>
              <div style="font-size:11px;color:var(--muted)" x-show="licStatus.reason === 'offline_grace'">⚠ Running in offline mode — will re-verify when internet is available.</div>
              <button class="btn btn-ghost btn-sm" style="margin-top:12px;color:var(--error)"
                @click="if(confirm('Deactivate this license on this machine? You can re-activate at any time.')) { fetch('/api/deactivate-license',{method:'POST'}).then(()=>window.location.reload()) }">
                Deactivate on this machine
              </button>
            </div>
          </template>
          <template x-if="licStatus && !licStatus.licensed">
            <div>
              <div style="color:var(--error);font-size:13px;margin-bottom:12px">✗ No active license found on this machine.</div>
              <button class="btn btn-primary btn-sm" @click="window.location.href='/activate'">Enter License Key →</button>
            </div>
          </template>
          <div x-show="!licStatus" style="color:var(--muted);font-size:12px">Checking license…</div>

          <!-- Version info -->
          <div style="margin-top:20px;padding-top:16px;border-top:1px solid var(--border)">
            <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
              <span style="font-size:12px;color:var(--muted)">
                Version <span x-text="updateInfo ? updateInfo.current_version : '…'" style="color:var(--text);font-weight:600"></span>
              </span>
              <template x-if="updateInfo && updateInfo.available">
                <span style="font-size:11px;background:rgba(255,140,0,.15);color:#ff8c00;padding:2px 8px;border-radius:20px;font-weight:600">
                  v<span x-text="updateInfo.latest"></span> available
                </span>
              </template>
              <template x-if="updateInfo && !updateInfo.available">
                <span style="font-size:11px;color:var(--success)">✓ Up to date</span>
              </template>
            </div>
            <div x-show="updateInfo && updateInfo.available" style="margin-top:10px">
              <div style="font-size:12px;color:var(--muted);margin-bottom:8px" x-text="updateInfo && updateInfo.release_notes"></div>
              <div x-data="{updating: false, updateMsg: '', updateErr: ''}" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
                <button class="btn btn-primary btn-sm"
                  :disabled="updating"
                  @click="
                    updating = true; updateMsg = ''; updateErr = '';
                    fetch('/api/apply-update', {method:'POST'})
                      .then(r => r.json().then(d => ({ok: r.ok, data: d})))
                      .then(({ok, data}) => {
                        if (ok) {
                          updateMsg = data.message || 'Update applied! Restarting…';
                          setTimeout(() => { location.reload(); }, 4000);
                        } else {
                          updateErr = data.error || 'Update failed';
                          updating = false;
                        }
                      })
                      .catch(e => { updateErr = 'Network error: ' + e; updating = false; })
                  ">
                  <span x-show="!updating">Install v<span x-text="updateInfo && updateInfo.latest"></span></span>
                  <span x-show="updating">Updating…</span>
                </button>
                <span x-show="updateMsg" style="font-size:12px;color:var(--success)" x-text="updateMsg"></span>
                <span x-show="updateErr" style="font-size:12px;color:var(--error)" x-text="updateErr"></span>
              </div>
            </div>
            <button class="btn btn-ghost btn-sm" style="margin-top:10px"
              @click="checking=true; fetch('/api/check-updates',{method:'POST'}).then(()=>setTimeout(()=>{ fetch('/api/update-status').then(r=>r.json()).then(d=>{updateInfo=d;checking=false}) },5000))">
              <span x-show="!checking">↺ Check for updates</span>
              <span x-show="checking">Checking…</span>
            </button>
          </div>
        </div>
      </div>

      <button class="btn btn-primary" @click="saveSettings()">Save Settings</button>
    </div>
  </div>

</div><!-- #main -->

<script>
function app() {
  return {
    view: 'dashboard',
    stats: {},
    nasOk: false,

    // Tag
    tagFolder: '',
    tagProvider: 'gemini',
    tagReprocess: false,
    tagError: '',
    activeJobId: null,
    jobStatus: '',
    logLines: [],
    folderHistory: JSON.parse(localStorage.getItem('mnFolderHistory') || '[]'),

    // Search
    searchQuery: '',
    smartSearch: true,
    expandedInfo: '',
    searchType: '',
    searchResults: [],
    searchLoading: false,
    searchError: '',

    // Filters and search enhancements
    filterOptions: {},
    filters: {camera: '', shot_type: '', camera_movement: '', time_of_day: '', audio_type: '', color_palette: '', setting: '', mood: '', lighting: '', file_ext: '', file_type: '', fps: '', has_people: ''},
    showFilters: false,

    // Database state
    dbFileExists: true,

    // Project DB support
    projectDbName: '',
    projectDbFolder: 'footage-tagger/project_dbs/',
    saveToMain: true,
    projectDbs: [],
    searchDb: '',   // '' = main archive, otherwise path to a project DB

    // History
    historyRows: [],

    // Script sourcing
    scriptText: '',
    scriptState: 'idle',   // idle | analysing | done | error
    scriptStatusMsg: '',
    scriptError: '',
    scriptShots: [],
    scriptSearchDb: '',
    scriptMaxPerShot: '5',
    scriptMediaFilter: 'all',       // all | video | image
    scriptDetectionMode: 'both',    // both | scene | audio
    scriptResultsLimit: '10',       // 5 | 10 | 15
    scriptSmartSearch: true,        // AI query expansion for shot matching

    // Settings
    settings: {},
    settingsSaved: false,


    async init() {
      await this.loadSettings();
      this.tagProvider = this.settings.vision_provider || 'gemini';
      await this.loadStats();
      await this.loadProjectDbs();
      this.checkDbExists();
    },


    async loadStats() {
      try {
        const r = await fetch('/api/stats');
        const d = await r.json();
        this.stats = d;
        this.nasOk = d.nas_ok;
      } catch(e) {}
    },

    async loadSettings() {
      const r = await fetch('/api/settings');
      this.settings = await r.json();
      this.tagProvider = this.settings.vision_provider || 'gemini';
    },

    async saveSettings() {
      await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(this.settings)
      });
      this.settingsSaved = true;
      await this.loadStats();
      setTimeout(() => this.settingsSaved = false, 3000);
    },

    async updateProviderInConfig() {
      await fetch('/api/settings', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({vision_provider: this.tagProvider})
      });
    },

    // Auto-fill project DB name from folder path
    onFolderChange() {
      const folder = this.tagFolder.trim();
      if (folder) {
        const parts = folder.replace(/\/$/, '').split('/');
        const name  = parts[parts.length - 1] || 'project';
        this.projectDbName = name + '.db';
        // Auto-set project DB save location to same folder
        this.projectDbFolder = folder;
      }
    },

    async loadProjectDbs() {
      try {
        const r = await fetch('/api/project-dbs');
        this.projectDbs = await r.json();
      } catch(e) { this.projectDbs = []; }
    },

    async startTag() {
      this.tagError = '';
      const folder = this.tagFolder.trim();
      if (!folder) return;
      if (!this.saveToMain && !this.projectDbName.trim()) {
        this.tagError = 'Enable main database or enter a project DB name.';
        return;
      }

      const r = await fetch('/api/tag', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          folder,
          reprocess:      this.tagReprocess,
          save_to_main:   this.saveToMain,
          project_db:     this.projectDbName.trim(),
          project_folder: this.projectDbFolder.trim(),
        })
      });
      const d = await r.json();
      if (d.error) { this.tagError = d.error; return; }

      // Save folder to history
      this.folderHistory = [folder, ...this.folderHistory.filter(f => f !== folder)].slice(0, 10);
      localStorage.setItem('mnFolderHistory', JSON.stringify(this.folderHistory));

      this.activeJobId = d.job_id;
      this.jobStatus   = 'running';
      this.logLines    = [];
      this.streamLog(d.job_id);
      // Refresh project DB list after job completes
      setTimeout(() => this.loadProjectDbs(), 3000);
    },

    streamLog(jobId) {
      const es = new EventSource(`/api/stream/${jobId}`);
      es.onmessage = (e) => {
        if (!e.data || e.data.trim() === '') return;
        try {
          const d = JSON.parse(e.data);
          if (d.__done__) {
            this.jobStatus = d.status;
            es.close();
            this.loadStats();
            return;
          }
          if (d.line !== undefined) {
            this.logLines.push(d.line);
            this.$nextTick(() => {
              const wrap = this.$refs.logWrap;
              if (wrap) wrap.scrollTop = wrap.scrollHeight;
            });
          }
        } catch(err) {}
      };
      es.onerror = () => { es.close(); if (this.jobStatus === 'running') this.jobStatus = 'error'; };
    },

    async stopJob() {
      if (!this.activeJobId) return;
      await fetch(`/api/stop/${this.activeJobId}`, {method:'POST'});
      this.jobStatus = 'stopped';
    },

    resetTag() {
      this.activeJobId = null;
      this.jobStatus   = '';
      this.logLines    = [];
      this.tagError    = '';
    },

    logClass(line) {
      if (!line) return 'skip';
      const l = line.toLowerCase();
      if (l.includes('error') || l.includes('✗')) return 'err';
      if (l.includes('skip') || l.includes('already')) return 'skip';
      if (l.includes('💰') || l.includes('cost') || l.includes('$')) return 'cost';
      if (l.includes('warning') || l.includes('warn')) return 'warn';
      if (l.includes('✓') || l.includes('finished') || l.includes('done')) return 'ok';
      return '';
    },

    async doSearch() {
      this.searchLoading = true;
      this.searchError   = '';
      this.expandedInfo  = '';
      try {
        const params = new URLSearchParams();
        if (this.searchQuery) params.append('q', this.searchQuery);
        if (this.searchType) params.append('type', this.searchType);
        if (this.smartSearch && this.searchQuery) params.append('smart', 'true');
        if (this.filters.camera)          params.append('camera',          this.filters.camera);
        if (this.filters.shot_type)        params.append('shot_type',        this.filters.shot_type);
        if (this.filters.camera_movement)  params.append('camera_movement',  this.filters.camera_movement);
        if (this.filters.time_of_day)      params.append('time_of_day',      this.filters.time_of_day);
        if (this.filters.audio_type)       params.append('audio_type',       this.filters.audio_type);
        if (this.filters.color_palette)    params.append('color_palette',    this.filters.color_palette);
        if (this.filters.setting)          params.append('setting',          this.filters.setting);
        if (this.filters.mood)             params.append('mood',             this.filters.mood);
        if (this.filters.lighting)         params.append('lighting',         this.filters.lighting);
        if (this.filters.file_ext)         params.append('file_ext',         this.filters.file_ext);
        if (this.filters.file_type)        params.append('type',             this.filters.file_type);
        if (this.filters.fps)              params.append('fps',              this.filters.fps);
        if (this.filters.has_people)       params.append('has_people',       this.filters.has_people);
        if (this.searchDb) params.append('db', this.searchDb);
        const r = await fetch('/api/search?' + params);
        const d = await r.json();
        if (d.error) { this.searchError = d.error; this.searchResults = []; }
        else if (d.results) {
          // Smart search returns {results, expanded, original}
          this.searchResults = d.results;
          this.expandedInfo  = d.expanded || '';
        }
        else this.searchResults = d;
      } catch(e) { this.searchError = 'Search failed'; }
      this.searchLoading = false;
    },


    async loadThumbs(r) {
      if (r._thumbsLoaded) return;
      r._thumbsLoaded = true;
      try {
        const res = await fetch('/api/file-thumbnails?path=' + encodeURIComponent(r.file_path));
        r._thumbs = await res.json();
        r._activeThumb = 0;
      } catch(e) {
        r._thumbs = [];
      }
    },

    scrubMove(r, e) {
      const thumbs = r._thumbs || [];
      if (!thumbs.length) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
      r._activeThumb = Math.min(Math.floor(pct * thumbs.length), thumbs.length - 1);
      r._scrubPct = Math.round(pct * 100);
    },

    // Set a nested path like 'settings.db_path' or 'settings.reference_persons[0].reference_image'
    _setByPath(path, value) {
      // Normalise bracket notation to dots: settings.reference_persons[2].foo → [...,'2','foo']
      const parts = path.replace(/\[(\d+)\]/g, '.$1').split('.');
      let obj = this;
      for (let i = 0; i < parts.length - 1; i++) {
        obj = obj[parts[i]];
        if (obj === undefined || obj === null) return;
      }
      obj[parts[parts.length - 1]] = value;
    },

    async pickFolder(targetModel, startPath) {
      try {
        const res = await fetch('/api/pick-folder', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({start_path: startPath || '~'})
        });
        if (res.ok) {
          const data = await res.json();
          if (!data.path) return;
          this._setByPath(targetModel, data.path);
          // Auto-update project DB fields when the tag folder changes
          if (targetModel === 'tagFolder') {
            this.onFolderChange();
          }
        }
      } catch(e) {
        console.error('Folder picker error:', e);
      }
    },

    async pickFile(targetModel, startPath, fileTypes) {
      try {
        const res = await fetch('/api/pick-file', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({start_path: startPath || '~', file_types: fileTypes || []})
        });
        if (res.ok) {
          const data = await res.json();
          if (!data.path) return;
          this._setByPath(targetModel, data.path);
        }
        // 400 = cancelled — silently ignore
      } catch(e) {
        console.error('File picker error:', e);
      }
    },

    async pickDbFolder() {
      try {
        const res = await fetch('/api/pick-folder', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({start_path: this.settings.db_path ? this.settings.db_path.replace(/[^/]+$/, '') : '~'})
        });
        if (res.ok) {
          const data = await res.json();
          if (!data.path) return;
          // Append default filename to the chosen folder
          let folder = data.path.replace(/\/$/, '');
          this.settings.db_path = folder + '/footage_metadata.db';
          this.checkDbExists();
        }
      } catch(e) {
        console.error('DB folder picker error:', e);
      }
    },

    async checkDbExists() {
      if (!this.settings.db_path) { this.dbFileExists = false; return; }
      try {
        const res = await fetch('/api/check-path?path=' + encodeURIComponent(this.settings.db_path));
        if (res.ok) {
          const data = await res.json();
          this.dbFileExists = data.exists;
        }
      } catch(e) { this.dbFileExists = false; }
    },

    async loadFilterOptions() {
      try {
        const res = await fetch('/api/filter-options');
        if (res.ok) {
          this.filterOptions = await res.json();
        }
      } catch(e) {
        console.error('Filter load error:', e);
      }
    },

    clearFilters() {
      this.filters = {camera: '', shot_type: '', setting: '', mood: '', lighting: '', file_ext: '', file_type: '', fps: '', has_people: ''};
      this.doSearch();
    },

    async loadRecent() {
      this.searchQuery = '';
      await this.doSearch();
    },

    async analyseScript() {
      if (!this.scriptText.trim()) return;
      this.scriptState     = 'analysing';
      this.scriptError     = '';
      this.scriptShots     = [];
      this.scriptStatusMsg = 'Sending script to AI model…';
      try {
        const res = await fetch('/api/script-source', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            script:         this.scriptText,
            db:             this.scriptSearchDb,
            max_per_shot:   parseInt(this.scriptMaxPerShot),
            media_filter:   this.scriptMediaFilter,
            detection_mode: this.scriptDetectionMode,
            results_limit:  parseInt(this.scriptResultsLimit),
            smart_search:   this.scriptSmartSearch
          })
        });
        const d = await res.json();
        if (d.error) {
          this.scriptError = d.error;
          this.scriptState = 'error';
          return;
        }
        // Open the first shot by default
        if (d.shots && d.shots.length) {
          d.shots[0]._open = true;
        }
        this.scriptShots = d.shots || [];
        this.scriptState = 'done';
      } catch(e) {
        this.scriptError = 'Failed to reach server. Is METANAS running?';
        this.scriptState = 'error';
      }
    },

    exportScriptResults() {
      const lines = [];
      this.scriptShots.forEach((shot, i) => {
        lines.push(`\n=== Shot ${i+1}: ${shot.label} ===`);
        lines.push(`Query: "${shot.query}"`);
        if (!shot.results.length) {
          lines.push('  (no matches)');
        } else {
          shot.results.forEach(c => {
            lines.push(`  ${c.filename}  |  ${c.camera_model || ''}  |  ${c.description ? c.description.slice(0,80) : ''}`);
          });
        }
      });
      const blob = new Blob([lines.join('\n')], {type: 'text/plain'});
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'script_sourcing_' + new Date().toISOString().slice(0,10) + '.txt';
      a.click();
    },

    async loadHistory() {
      const r = await fetch('/api/history');
      this.historyRows = await r.json();
    },

    async revealFile(fp) {
      const r = await fetch('/api/reveal', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({file_path: fp})
      });
      const d = await r.json();
      if (d.error) alert(d.error);
    },

    async openInPremiere(fp) {
      const r = await fetch('/api/open-premiere', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({file_path: fp})
      });
      const d = await r.json();
      if (d.error) alert('Could not open in Premiere Pro: ' + d.error);
    },

    async sendToFolder(fp, resultObj) {
      if (!this.settings.send_folder) {
        alert('No send folder set. Go to Settings and add a Send to Folder path.');
        return;
      }
      const r = await fetch('/api/send-to-folder', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({file_path: fp})
      });
      const d = await r.json();
      if (d.error) { alert(d.error); return; }
      resultObj._toast = '✓ Copied to ' + this.settings.send_folder.split('/').pop();
      setTimeout(() => resultObj._toast = '', 3000);
    },

    duration(start, end) {
      if (!start || !end) return '—';
      const s = new Date(start), e = new Date(end);
      const diff = Math.round((e - s) / 1000);
      if (diff < 60) return diff + 's';
      if (diff < 3600) return Math.round(diff/60) + 'm ' + (diff%60) + 's';
      return Math.floor(diff/3600) + 'h ' + Math.round((diff%3600)/60) + 'm';
    }
  };
}

function updateBanner() {
  return {
    info: { available: false },
    async check() {
      // Poll every 30s until update info comes in (background thread needs ~8s)
      for (let i = 0; i < 10; i++) {
        await new Promise(r => setTimeout(r, 3000));
        try {
          const res = await fetch('/api/update-status');
          const d   = await res.json();
          if (d.available) { this.info = d; break; }
        } catch(e) {}
      }
    }
  };
}
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print()
    print("  ███╗   ███╗███████╗████████╗ █████╗ ███╗   ██╗ █████╗ ███████╗")
    print("  ████╗ ████║██╔════╝╚══██╔══╝██╔══██╗████╗  ██║██╔══██╗██╔════╝")
    print("  ██╔████╔██║█████╗     ██║   ███████║██╔██╗ ██║███████║███████╗")
    print("  ██║╚██╔╝██║██╔══╝     ██║   ██╔══██║██║╚██╗██║██╔══██║╚════██║")
    print("  ██║ ╚═╝ ██║███████╗   ██║   ██║  ██║██║ ╚████║██║  ██║███████║")
    print("  ╚═╝     ╚═╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═══╝╚═╝  ╚═╝╚══════╝")
    print()
    print("  Metadata Tagger for NAS Footage")
    print(f"  Open in your browser: http://localhost:5151")
    print(f"  On your local network: http://<your-mac-ip>:5151")
    print()
    app.run(host="0.0.0.0", port=5151, debug=False, threaded=True)
