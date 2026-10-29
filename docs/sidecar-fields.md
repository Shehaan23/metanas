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
| `metanas:fileHash` | metanas | string | xxhash64 hex digest of the source file, used for `--resume` |
| `metanas:camera` | metanas | string | Camera name from the SOP folder (e.g. A7S3, ZVE1, Drone) |
| `metanas:day` | metanas | integer | Shoot day from the SOP folder (0 if unknown) |
| `metanas:shotType` | metanas | string | One of: `talking`, `broll`, `mixed`, `unusable` |
| `metanas:needsManualSync` | metanas | boolean | `true` if lip motion detected without matching audio |
| `metanas:syncSpans` | metanas | JSON string | Array of `[start_s, end_s]` pairs needing manual sync review |
| `metanas:audioRmsDbfs` | metanas | number | Overall audio RMS in dBFS |
| `metanas:silenceRatio` | metanas | number | Fraction of 0.5s windows below -50 dBFS (0.0–1.0) |
| `metanas:usable` | metanas | boolean | `true` if clip passes quality gate (has video, >0.5s, >=480p) |
| `metanas:transcript` | metanas | JSON string | Word-level transcript: `[{"start":0,"end":12.5,"word":"...","speaker":"unknown"}]` |
| `metanas:metanasVersion` | metanas | string | App version that produced this sidecar |
| `metanas:processedAt` | metanas | string | ISO 8601 UTC timestamp of processing |

## Notes

- The `metanas:transcript` field stores word-level timestamps as a JSON string inside a single XMP property. With faster_whisper (current engine), this is a single entry covering the whole clip. When WhisperX is integrated, this will contain per-word entries with speaker labels.
- The `metanas:syncSpans` field is a JSON array of `[start, end]` pairs in seconds. Empty array `[]` when no sync issues detected.
- `metanas:shotType` classification rules:
  - **talking**: known face on screen + speech for >50% of clip
  - **broll**: no speech detected
  - **mixed**: both face and speech present but speech <50%
  - **unusable**: fails quality gate
- `metanas:usable` threshold: must have a video codec, duration > 0.5s, and height >= 480px.
