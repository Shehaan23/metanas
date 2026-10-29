# METANAS CLI

Headless commands for tagging and indexing footage from the terminal. The CLI calls the same engine as the GUI — same models, same config, same XMP format — and adds new fields for shot classification, audio analysis, and an index file.

## Install

```bash
# From the repo root:
sudo ln -sf "$(pwd)/footage-tagger/metanas" /usr/local/bin/metanas

# Verify:
metanas --version
```

The wrapper uses the venv at `~/.metanas/.venv/` (same one the GUI installs). If it's missing, run `footage-tagger/install.sh` first.

### Optional: xxhash (recommended)

```bash
~/.metanas/.venv/bin/pip install xxhash
```

Without xxhash, `--resume` hashing falls back to blake2b (slower, still works).

## Usage

### `metanas ingest`

Tag and transcribe every video in a footage folder.

```
metanas ingest <footage_folder> [options]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `<footage_folder>` | required | The `_Footage` folder, or any subfolder |
| `--out <path>` | `<project>_Premiere/brief` | Where clips.json, thumbs/, failures.json go |
| `--resume` | off | Skip clips whose sidecar exists and hash matches |
| `--no-embed` | off | Skip embedding metadata into the video file (keeps source bytes identical) |
| `--cameras A7S3,ZVE1` | all | Only process these camera folders |
| `--workers N` | CPU count | Parallel workers (Ollama forces 1) |
| `--quiet` | off | Suppress per-clip progress |
| `--dry-run` | off | List clips and exit |

**Examples:**

```bash
# Full run on Hiriketiya footage
metanas ingest "/Volumes/Assort2025/Sheneller Projects/040524a_Hiriketiya/040524a_Hiriketiya_Footage"

# Resume after interruption
metanas ingest "/Volumes/Assort2025/..." --resume

# Only A7S3 and drone footage
metanas ingest "/Volumes/Assort2025/..." --cameras A7S3,Drone

# Dry run to see what would be processed
metanas ingest "/Volumes/Assort2025/..." --dry-run
```

### `metanas index`

Rebuild `clips.json` from existing XMP sidecars without reprocessing.

```
metanas index <footage_folder> [--out <path>]
```

**Example:**

```bash
metanas index "/Volumes/Assort2025/.../040524a_Hiriketiya_Footage"
```

## Progress line format

One line per clip, flushed immediately (format: `shotType/contentType`):

```
[12/340] A7S3_day1/C0042.MP4  interview/talking  02:14  ok
[13/340] Drone_day1/DJI_0007.MP4  aerial/broll  00:48  ok
[14/340] ZVE1_day2/C0101.MP4  close-up/talking  01:03  SYNC NEEDED 12-41
[15/340] I360_day1/VID_0003.insv  -/-  -  FAILED unsupported codec
```

Followed by a summary: clips processed, skipped, failed, sync-flagged, talking/broll minutes, and wall time.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | All clips processed successfully |
| 1 | Some clips failed (see failures.json) |
| 2 | Bad arguments or another ingest holds the lock |

## Outputs

| File | Location | Purpose |
|------|----------|---------|
| `*.xmp` | Beside each clip | XMP sidecar (source of truth) |
| `clips.json` | `--out` dir | Derived index of all clips |
| `thumbs/` | `--out` dir | Scene thumbnails (480px JPEG) |
| `failures.json` | `--out` dir | Clips that errored |
| `.metanas.lock` | `--out` dir | PID lock (removed on exit) |

## Locking

Only one ingest can run per output folder at a time. The lock file contains the PID and start time. If the previous run crashed, the stale lock is automatically removed. The GUI also checks for this lock before starting a run.

## Configuration

Reads `~/.metanas/config.yaml` — the same file the GUI uses. No separate CLI config needed. Vision provider, API keys, whisper model, reference persons, and all other settings are shared.

### Camera aliases

Add a `camera_aliases` map to resolve older folder names to SOP names:

```yaml
camera_aliases:
  Sony: A7S3
  Action: ACT5
  Insta360: I360
```

The SOP name is written to `metanas:camera`; the raw folder token to `metanas:cameraRaw`.

### Speaker labels

`faster_whisper` (the current transcription engine) does not provide speaker diarisation. For per-speaker labels, create a separate Python 3.12 venv with WhisperX — the CLI detects and uses it automatically when available.

## Sidecar fields

See [docs/sidecar-fields.md](docs/sidecar-fields.md) for the complete field list (original + new).
