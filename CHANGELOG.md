# METANAS Changelog

All notable changes to the METANAS application are documented in this file.

**Format:** Each release lists the version number, date, platform(s), and a summary of changes grouped by category. The version numbering follows `MAJOR.MINOR.PATCH`.

**Maintainer:** Sheneller Ventures / Assort Creative Pvt Ltd
**Repository:** [github.com/Shehaan23/metanas](https://github.com/Shehaan23/metanas)
**Website:** [metanas.io](https://metanas.io)

---

## [14.0.0] — 2026-04-22 | Mac, Windows

### Added
- **Custom Preset Tags** — New input field in the Tag Footage panel lets users manually enter project-specific tags (comma-separated) before starting a tagging job. These tags are appended to every clip's metadata alongside the AI-generated tags, ensuring consistent project taxonomy across all files.
- **Dual API with Automatic Failover** — Configure a primary and secondary vision provider (Gemini / OpenAI) in Settings. If the primary API returns an error (429 rate limit, 503 overloaded, network timeout), the system automatically retries the clip with the secondary provider. No clips are left untagged due to a single API outage.
- **Smart Retry with Exponential Backoff** — When an API call fails, METANAS now waits progressively longer before retrying: 5s → 15s → 30s → 60s. A minimum 4-second interval between API calls (~15 req/min) prevents rate-limit storms. Clips that still fail after all retries are queued and retried again at the end of the batch.
- **Parallel Multi-Worker Processing** — Configurable thread pool (1–8 workers) processes multiple clips concurrently. Each worker gets its own SQLite connection for thread safety. Default: 4 workers. Adjustable in Settings under "Max Workers".
- **Real-Time Tagged Clips Panel** — The Tag Footage progress view now shows a live thumbnail grid of clips as they're tagged, updated every 8 seconds. Users can see results building in real-time instead of waiting for the entire batch to finish.
- **Per-Clip Cost & ETA Logging** — Restored the running Gemini API cost (`💰`) and ETA lines after each clip in the live log, which were present in v13.6 but dropped during the v14 refactor.

### Changed
- `footage_tagger.py` refactored from sequential loop to `concurrent.futures.ThreadPoolExecutor` with per-thread database connections.
- API throttling globals (`MIN_API_INTERVAL = 4s`, `LAST_API_CALL_TIME`) added to rate-limit outbound calls even under parallel load.
- `--custom-tags` CLI argument added to `footage_tagger.py`; `app.py` passes it from the frontend form.
- Secondary Provider dropdown and Max Workers input added to the Settings panel.

### Fixed
- Thread-safety: each parallel worker now opens and closes its own `sqlite3.connect()` instead of sharing a single connection across threads.

### Build
- Mac: `.app` bundle based on v13.6 codebase (4230 lines), packaged as zip for DMG creation.
- Windows: `.exe` via PyInstaller + Inno Setup installer.
- `Info.plist` updated: `CFBundleVersion` → `14.0.0`, `CFBundleIdentifier` → `com.assortcreative.metanas`, copyright → `© 2026 Assort Creative Pvt Ltd`.

---

## [13.6.0] — 2026-04 | Mac, Windows

### Added
- **History Redesign** — Replaced the raw log-dump history view with clean run-summary cards for every job. Each card shows stat pills at a glance: files tagged, skipped, errors, and Gemini cost.
- **Smart Error Grouping** — Hundreds of duplicate errors in a job collapse into a single counted row with an expandable file list.
- **Full Log Modal** — New "View Full Log" modal with live search, syntax colouring, copy-to-clipboard, and log download.
- **ARW Preview Thumbnails** — Sony `.ARW` RAW files now display thumbnail previews in Search results (previously only videos had preview blocks).

### Changed
- Windows cross-platform parity across the entire app — all Mac-only features now work identically on Windows.
- Single-file Windows installer build kit (PyInstaller + Inno Setup).

### Build
- App codebase: ~4200 lines (`app.py`).
- Both Mac `.app` bundle and Windows `.exe` installer shipped.

---

## [13.5.0] — 2026-03 | Mac, Windows

### Added
- **Self-Update System** — METANAS checks `version.json` on GitHub at launch. If a newer version exists, an orange banner appears with "Update Now" — clicking it downloads and hot-swaps `app.py` from the repo, then restarts the Flask server. No reinstall required.
- **Dual Database Write** — Tag Footage can now write simultaneously to the main archive database and a project-specific database. Project DBs can be saved to custom folders.
- **Script Source Enhancements** — Improved filters and search within the Script Source / Shot List feature.
- **Thumbnail Path Fix** — Resolved issues where thumbnails pointed to paths inside the app bundle instead of `~/.metanas/thumbnails`.

### Changed
- Config migration: `db_path` and `thumbnails_path` are automatically moved from old bundle-internal paths to `~/.metanas/` on launch.
- `APP_VERSION` bumped to `13.5.0`.

---

## [13.4.0] — 2026-03 | Mac, Windows

### Added
- **AI-Powered Smart Search** — New toggle in Search that uses Gemini to expand natural-language queries into comprehensive FTS5 search terms. For example, searching "beach sunset" also matches "coast", "shore", "golden hour", "dusk", etc.
- **Search Query Expansion Display** — When Smart Search is active, the expanded query is shown below the search bar so users can see what was actually searched.

### Changed
- Search endpoint updated to support `smart=true` parameter.
- Gemini query expansion with caching to avoid redundant API calls.

---

## [13.3.0] — 2026-03 | Mac

### Added
- **Script Source** — New dedicated view for shot-list / script-source workflow. Users can search tagged footage by shot type, camera movement, setting, and other metadata fields to find clips matching specific script requirements.
- **Project Database Support** — Ability to tag footage into separate project-specific `.db` files instead of (or in addition to) the main archive.
- **Filter Options API** — Dynamic filter dropdowns populated from actual values in the database (camera models, shot types, settings, etc.).

### Fixed
- Gemini SDK compatibility fix for `google-genai` API changes.
- Script source filters returning incorrect results when combined.

### Build
- First version pushed to GitHub repository ([github.com/Shehaan23/metanas](https://github.com/Shehaan23/metanas)).
- `APP_VERSION` tracking introduced in codebase.

---

## [13.2.0] — 2026-02 | Mac

### Fixed
- **Gemini SDK Breaking Change** — Updated `analyse_frame_with_gemini()` to work with the latest `google-genai` SDK, which changed how image data is passed to the API.
- Script source view returning empty results due to SQL query issue.

### Build
- GitHub release workflow established — zip distribution via Gumroad with release notes.

---

## [13.1.0] — 2026-02 | Mac

### Added
- **Gumroad License Verification** — App requires activation with a Gumroad license key on first launch. License is verified against the Gumroad API with machine ID binding. Includes grace period for offline use.
- **License Activation Page** — Dedicated `/activate` route with clean UI for entering and validating license codes.
- **Machine Binding** — Each license is locked to a specific machine via hardware ID. Prevents sharing of a single license across multiple computers.

### Changed
- All API routes gated behind `is_licensed()` check (except `/activate` and static assets).
- License status persisted in `~/.metanas/config.yaml`.

---

## [13.0.0] — 2026-01 | Mac

This is the first version built as a macOS `.app` bundle with a full web-based UI. Prior versions (v1.x) were command-line Python scripts.

### Added
- **macOS `.app` Bundle** — Double-click launcher with automatic first-time setup via Terminal. Installs Python venv, ExifTool, ffmpeg, and all pip dependencies automatically.
- **Flask Web UI** — Full browser-based interface at `localhost:5151` with dark theme, replacing the previous CLI-only workflow.
- **Dashboard** — Overview page showing total tagged files, recent activity, and database stats.
- **Tag Footage** — Browse to any folder, click "Start", and watch the live log as clips are processed. Supports both video files and images.
- **Search Archive** — SQLite FTS5 full-text search across all tagged metadata. Filter by file type, camera model, shot type, setting, mood, lighting, time of day, and more.
- **AI Vision Analysis** — Gemini (primary) and OpenAI vision APIs analyse extracted keyframes to generate: description, shot type, camera movement, setting, lighting, mood, color palette, time of day, subjects, and tags.
- **Scene Detection** — PySceneDetect extracts keyframes at scene boundaries for multi-scene clips. Single-scene clips use a fallback mid-frame extraction.
- **Audio Transcription** — Faster-Whisper (medium model) transcribes dialogue and audio, stored as searchable metadata.
- **Person Recognition** — Optional face-matching against a reference folder of known individuals. Identified persons are tagged in metadata.
- **XMP Sidecar Generation** — Every tagged file gets an `.xmp` sidecar with all metadata in Adobe-compatible XMP format, readable by Premiere Pro and Bridge.
- **Embedded Metadata** — ExifTool embeds tags directly into the file's metadata (MP4, MOV, JPG, etc.) alongside the sidecar.
- **Thumbnail Generation** — Keyframe thumbnails cached at `~/.metanas/thumbnails/` for search result previews.
- **Settings Panel** — Configure API keys (Gemini / OpenAI), vision provider, database path, thumbnail directory, and caffeinate (sleep prevention) toggle.
- **Caffeinate Integration** — Optional macOS `caffeinate` to prevent sleep during long tagging jobs.
- **Reveal in Finder / Open in Premiere** — Right-click actions on search results to locate files or open them directly in Adobe Premiere Pro.
- **Send to Folder** — Copy or move tagged files to a destination folder from search results.
- **Cost Tracking** — Running estimate of Gemini API cost per job based on token usage (input/output pricing for Gemini 2.5 Flash).

### Technical
- `app.py`: Flask server with Alpine.js reactive frontend (single-file architecture — all HTML/CSS/JS inline).
- `footage_tagger.py`: Standalone AI pipeline called as subprocess with `--config` and `--folder` arguments.
- `search.py`: Dedicated search module for Script Source functionality.
- `install.sh`: First-time setup script — checks Python 3, installs ExifTool + ffmpeg, creates venv, installs pip packages.
- `start.sh`: Manual launch script for development/debugging.
- SQLite database at `~/.metanas/footage_metadata.db` with FTS5 virtual table and triggers for full-text indexing.
- Config at `~/.metanas/config.yaml`.

---

## [1.0.0] — 2025 | CLI only

### Added
- Initial Python script for AI-powered video metadata tagging.
- Gemini vision API integration for keyframe analysis.
- Basic metadata extraction and XMP sidecar generation.
- Command-line interface — run directly with `python3 footage_tagger.py`.
- SQLite database for storing tagged metadata.

---

## Version Lineage

This table shows the build lineage to prevent base-version confusion:

| Version | Base    | Platform    | Lines (app.py) | Key Addition                          |
|---------|---------|-------------|----------------|---------------------------------------|
| 1.0.0   | —       | CLI         | —              | Initial script                        |
| 13.0.0  | 1.0     | Mac         | ~2000          | Flask web UI + .app bundle            |
| 13.1.0  | 13.0    | Mac         | ~2200          | Gumroad license verification          |
| 13.2.0  | 13.1    | Mac         | ~2400          | Gemini SDK fix                        |
| 13.3.0  | 13.2    | Mac         | ~2842          | Script Source + project DBs + GitHub  |
| 13.4.0  | 13.3    | Mac, Win    | ~3100          | AI Smart Search                       |
| 13.5.0  | 13.4    | Mac, Win    | ~3468          | Self-update + dual DB write           |
| 13.6.0  | 13.5    | Mac, Win    | ~4200          | History redesign + ARW previews       |
| 14.0.0  | **13.6** | Mac, Win   | ~4350          | Parallel processing + failover + tags |

> **Important:** Always build new versions on top of the latest release (rightmost column). The v14.0.0 build was initially started from v13.3 by mistake — this table prevents that from happening again.

---

*Maintained by Sheneller Ventures / Assort Creative Pvt Ltd*
*Last updated: 2026-04-22*
