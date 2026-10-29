# METANAS XMP Sidecar Fields

Every processed clip gets an XMP sidecar (`.xmp`) written beside it. This file documents every field, old and new.

## Namespaces

| Prefix | URI |
|--------|-----|
| `dc` | `http://purl.org/dc/elements/1.1/` |
| `tiff` | `http://ns.adobe.com/tiff/1.0/` |
| `xmpDM` | `http://ns.adobe.com/xmp/1.0/DynamicMedia/` |
| `xmp` | `http://ns.adobe.com/xap/1.0/` |
| `metanas` | `http://metanas.app/ns/1.0/` |

## Original fields (GUI engine)

These are written by both the GUI and the CLI. Format is unchanged.

| Field | Namespace | Type | Description |
|-------|-----------|------|-------------|
| `dc:description` | dc | Alt string | AI-generated description combining setting, lighting, mood, subjects, transcript |
| `dc:subject` | dc | Bag of strings | Tag bag: persons, tags, mood_tags, shot_type, camera_movement, mood, time_of_day, audio_type, color_palette, camera_model, setting words |
| `tiff:Model` | tiff | string | Camera model (e.g. "Sony A7S III") |
| `xmpDM:cameraModel` | xmpDM | string | Camera model (duplicate of tiff:Model for Premiere compat) |
| `xmpDM:logComment` | xmpDM | string | Structured metadata: `PERSONS: ... | SHOT: ... | MOVEMENT: ... | TIME: ... | AUDIO: ... | CAMERA: ... | LIGHTING: ... | MOOD: ... | PALETTE: ...` |
| `xmp:Rating` | xmp | integer | Numeric rating (default 0) |

## New fields (CLI only)

Added by `metanas ingest` under the `metanas:` namespace. The original fields above are always present and unchanged.

| Field | Namespace | Type | Description |
|-------|-----------|------|-------------|
| `metanas:clipId` | metanas | string | Unique ID: `<camera>_day<N>_<filename_without_ext>` |
| `metanas:fileHash` | metanas | string | xxhash64 hex digest of the file **after** embed (so `--resume` matches) |
| `metanas:camera` | metanas | string | SOP-normalised camera name (e.g. A7S3, ZVE1, Drone). See `camera_aliases` in config. |
| `metanas:cameraRaw` | metanas | string | Raw folder token before alias mapping (e.g. `Sony`, `Action`) |
| `metanas:day` | metanas | integer | Shoot day from the SOP folder (0 if unknown) |
| `metanas:shotType` | metanas | string | Framing: `wide`, `medium`, `close-up`, `aerial`, `pov`, `interview`, `broll`, `mixed` |
| `metanas:contentType` | metanas | string | Timeline bucket: `talking`, `broll`, `mixed`, `unusable` |
| `metanas:speechRatio` | metanas | number | Fraction of clip duration covered by speech (0.0–1.0) |
| `metanas:needsManualSync` | metanas | boolean | `true` if person visibly speaking but audio absent/silent for >3 s |
| `metanas:syncSpans` | metanas | JSON string | Array of `[start_s, end_s]` pairs needing manual sync review |
| `metanas:audioRmsDbfs` | metanas | number | Overall audio RMS in dBFS |
| `metanas:silenceRatio` | metanas | number | Fraction of 0.5s windows below -50 dBFS (0.0–1.0) |
| `metanas:usable` | metanas | boolean | `true` if clip passes quality gate (has video, >0.5s, >=480p) |
| `metanas:thumbs` | metanas | JSON string | Array of relative paths to 480px keyframe JPEGs under `thumbs/` |
| `metanas:transcript` | metanas | JSON string | Word-level transcript: `[{"start":0,"end":12.5,"word":"...","speaker":"unknown"}]` |
| `metanas:metanasVersion` | metanas | string | App version that produced this sidecar |
| `metanas:processedAt` | metanas | string | ISO 8601 UTC timestamp of processing |

## Notes

- **`fileHash` is post-embed.** The hash is computed after exiftool embeds metadata into the MP4, so `--resume` compares the stored hash against the file as it stands on disk. Use `--no-embed` to skip the embed (keeps source bytes identical); the hash is then the original file hash.
- **`shotType` vs `contentType`.** `shotType` describes camera framing (wide, close-up, aerial, etc.) from the AI vision analysis. `contentType` describes the editorial role for the Premiere timeline (talking = Talking timeline, broll = B-roll timeline).
- `metanas:contentType` classification rules:
  - **talking**: known face on screen AND `speechRatio` > 0.5
  - **broll**: `speechRatio` < 0.1, or no audio track
  - **mixed**: anything between
  - **unusable**: fails quality gate
- `metanas:needsManualSync` flags clips where a person is visibly speaking but audio is absent or below -50 dBFS for >3 seconds. This is the case where the camera mic was off and external audio must be laid in by hand.
- `metanas:speechRatio` = total span of transcript words / clip duration. Can be used to tune the talking/broll threshold later without reprocessing.
- **Camera aliases.** Add a `camera_aliases` map in `~/.metanas/config.yaml` to map older folder names to SOP names: e.g. `Sony: A7S3`, `Action: ACT5`. The SOP name goes into `metanas:camera`; the raw folder token goes into `metanas:cameraRaw`.
- The `metanas:transcript` field stores word-level timestamps as JSON. With faster_whisper (current), this is a single entry covering the whole clip. WhisperX provides per-word entries with speaker labels.
- `metanas:usable` threshold: must have a video codec, duration > 0.5s, and height >= 480px.
- `metanas:thumbs` stores relative paths (from the `--out` directory) to 480px-wide JPEG keyframes saved during ingest.
