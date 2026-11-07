#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
# METANAS — Auto-Retry Wrapper
# Runs the METANAS CLI and automatically retries when the NAS disconnects.
# Exit code 75 (EX_TEMPFAIL) means "NAS dropped — try again later".
#
# Usage:
#   ./run_with_retry.sh metanas ingest /Volumes/NAS/Projects/2026/MyProject --resume
#   ./run_with_retry.sh python3 footage_tagger.py --config config.yaml
#
# The script will:
#   1. Run the command
#   2. If it exits with code 75, wait for the NAS to come back
#   3. Re-run (already-processed files are automatically skipped)
#   4. Repeat up to MAX_RETRIES times
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────
MAX_RETRIES="${MAX_RETRIES:-10}"           # max auto-restarts
NAS_CHECK_PATH="${NAS_CHECK_PATH:-}"       # auto-detected from args if empty
NAS_POLL_INTERVAL="${NAS_POLL_INTERVAL:-30}"  # seconds between NAS checks
NAS_POLL_TIMEOUT="${NAS_POLL_TIMEOUT:-7200}"  # max wait (2 hours default)
COOLDOWN="${COOLDOWN:-60}"                 # seconds to wait after NAS comes back
EX_TEMPFAIL=75

# ── Helpers ───────────────────────────────────────────────────────────────
log() { echo "[retry] $(date '+%H:%M:%S') $*"; }

# Try to detect the NAS mount path from the command arguments
detect_nas_path() {
    for arg in "$@"; do
        # Look for /Volumes/ paths (macOS NAS mounts)
        if [[ "$arg" == /Volumes/* ]]; then
            # Use the mount point (first two path components)
            echo "$arg" | cut -d'/' -f1-3
            return
        fi
    done
    echo ""
}

wait_for_nas() {
    local nas_path="$1"
    if [[ -z "$nas_path" ]]; then
        log "No NAS path to check — waiting ${COOLDOWN}s then retrying blindly"
        sleep "$COOLDOWN"
        return 0
    fi

    log "Waiting for NAS at: $nas_path"
    local waited=0
    while [[ $waited -lt $NAS_POLL_TIMEOUT ]]; do
        if [[ -d "$nas_path" ]] && ls "$nas_path" &>/dev/null; then
            log "NAS is back! Waiting ${COOLDOWN}s cooldown before restart…"
            sleep "$COOLDOWN"
            return 0
        fi
        sleep "$NAS_POLL_INTERVAL"
        waited=$((waited + NAS_POLL_INTERVAL))
        log "  Still waiting… (${waited}s / ${NAS_POLL_TIMEOUT}s)"
    done

    log "NAS did not come back within ${NAS_POLL_TIMEOUT}s — giving up."
    return 1
}

# ── Main ──────────────────────────────────────────────────────────────────
if [[ $# -eq 0 ]]; then
    echo "Usage: $0 <command> [args...]"
    echo "Example: $0 metanas ingest /Volumes/NAS/Projects/MyProject --resume"
    exit 1
fi

# Auto-detect NAS path if not set
if [[ -z "$NAS_CHECK_PATH" ]]; then
    NAS_CHECK_PATH=$(detect_nas_path "$@")
    if [[ -n "$NAS_CHECK_PATH" ]]; then
        log "Auto-detected NAS path: $NAS_CHECK_PATH"
    fi
fi

attempt=0
while [[ $attempt -le $MAX_RETRIES ]]; do
    if [[ $attempt -gt 0 ]]; then
        log "═══ Retry $attempt / $MAX_RETRIES ═══"
    else
        log "═══ Starting: $* ═══"
    fi

    # Run the command
    set +e
    "$@"
    exit_code=$?
    set -e

    if [[ $exit_code -eq 0 ]]; then
        log "✓ Completed successfully."
        exit 0
    elif [[ $exit_code -eq $EX_TEMPFAIL ]]; then
        attempt=$((attempt + 1))
        if [[ $attempt -gt $MAX_RETRIES ]]; then
            log "✗ Max retries ($MAX_RETRIES) reached. Giving up."
            exit $EX_TEMPFAIL
        fi
        log "NAS disconnected (exit code $EX_TEMPFAIL). Will retry once it's back."
        if ! wait_for_nas "$NAS_CHECK_PATH"; then
            exit $EX_TEMPFAIL
        fi
    else
        log "✗ Command failed with exit code $exit_code (not a NAS disconnect)."
        exit $exit_code
    fi
done
