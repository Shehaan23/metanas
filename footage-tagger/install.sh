#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  METANAS — First-time setup
#  Called automatically by the METANAS launcher on first launch.
# ─────────────────────────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"

# Everything lives outside the app bundle — survives updates, avoids macOS
# app-bundle write-protection.
METANAS_HOME="$HOME/.metanas"
VENV="$METANAS_HOME/.venv"

mkdir -p "$METANAS_HOME"

echo ""
echo "  ────────────────────────────────────────────"
echo "   METANAS — Setting up for the first time"
echo "  ────────────────────────────────────────────"
echo ""

# ── 1. Check Python 3 ─────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "  ✗  Python 3 not found."
  echo ""
  echo "  Please install Python 3 from:"
  echo "  https://www.python.org/downloads/macos/"
  echo ""
  echo "  Then re-launch METANAS.app"
  echo ""
  read -n 1 -s -r -p "  Press any key to exit…"
  exit 1
fi
PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "  ✓  Python $PY_VER found"

# ── 2. Install ExifTool (no Homebrew required) ────────────────────────────────
echo ""
if command -v exiftool &>/dev/null; then
  echo "  ✓  ExifTool already installed"
else
  echo "  Installing ExifTool (required for metadata embedding)…"

  # Find the latest .pkg filename from the exiftool.org page
  EXIF_PKG=$(curl -sf https://exiftool.org/ | grep -o 'ExifTool-[0-9.]*\.pkg' | head -1)

  if [ -z "$EXIF_PKG" ]; then
    # Fallback to a known stable version if site is unreachable
    EXIF_PKG="ExifTool-13.25.pkg"
  fi

  EXIF_URL="https://exiftool.org/$EXIF_PKG"
  EXIF_TMP="/tmp/$EXIF_PKG"

  echo "  Downloading $EXIF_PKG…"
  if curl -L --silent --show-error "$EXIF_URL" -o "$EXIF_TMP"; then
    echo "  Installing (you may be asked for your Mac password)…"
    if sudo installer -pkg "$EXIF_TMP" -target / ; then
      echo "  ✓  ExifTool installed"
      rm -f "$EXIF_TMP"
    else
      echo "  ⚠  ExifTool install failed — you can install it later from exiftool.org"
      echo "     METANAS will still tag and write XMP sidecars without it."
    fi
  else
    echo "  ⚠  Could not download ExifTool — check your internet connection."
    echo "     You can install it later from: exiftool.org"
    echo "     METANAS will still tag and write XMP sidecars without it."
  fi
fi

# ── 3. Install ffmpeg (required for keyframe extraction) ─────────────────────
echo ""
if command -v ffmpeg &>/dev/null; then
  echo "  ✓  ffmpeg already installed"
else
  echo "  Installing ffmpeg (required for AI vision analysis)…"
  # Try Homebrew first (works on both Intel and Apple Silicon)
  if command -v brew &>/dev/null; then
    if brew install ffmpeg --quiet; then
      echo "  ✓  ffmpeg installed via Homebrew"
    else
      echo "  ⚠  ffmpeg install failed — install manually: brew install ffmpeg"
    fi
  else
    # Download standalone static build (no Homebrew needed)
    ARCH=$(uname -m)
    if [ "$ARCH" = "arm64" ]; then
      FFMPEG_URL="https://www.osxexperts.net/ffmpeg7arm.zip"
    else
      FFMPEG_URL="https://www.osxexperts.net/ffmpeg7intel.zip"
    fi
    FFMPEG_TMP="/tmp/ffmpeg_static.zip"
    echo "  Downloading ffmpeg static build for $ARCH…"
    if curl -L --silent --show-error "$FFMPEG_URL" -o "$FFMPEG_TMP"; then
      sudo mkdir -p /usr/local/bin
      sudo unzip -o -j "$FFMPEG_TMP" "ffmpeg" -d /usr/local/bin/ 2>/dev/null
      sudo unzip -o -j "$FFMPEG_TMP" "ffprobe" -d /usr/local/bin/ 2>/dev/null
      sudo chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe 2>/dev/null
      rm -f "$FFMPEG_TMP"
      if command -v ffmpeg &>/dev/null; then
        echo "  ✓  ffmpeg installed"
      else
        echo "  ⚠  ffmpeg install failed — install manually: brew install ffmpeg"
      fi
    else
      echo "  ⚠  Could not download ffmpeg. Install manually: brew install ffmpeg"
    fi
  fi
fi

# ── 5. Create / repair virtual environment at ~/.metanas/.venv ─────────────────
echo ""
echo "  Setting up Python environment…"

# On Apple Silicon (arm64) we must force arm64 mode so pip downloads arm64
# wheels. Without this, packages like pydantic-core get x86_64 binaries which
# crash when spawned as a subprocess of the native arm64 METANAS.app.
ARCH=$(uname -m)
if [ "$ARCH" = "arm64" ]; then
  PYTHON_CMD="arch -arm64 python3"
  PIP_CMD="arch -arm64 "$VENV/bin/pip""
  echo "  (Apple Silicon detected — using arm64 mode)"
else
  PYTHON_CMD="python3"
  PIP_CMD=""$VENV/bin/pip""
fi

# Remove any partial/broken venv from a previous failed install
if [ -d "$VENV" ] && [ ! -f "$VENV/bin/python3" ]; then
  echo "  (Removing incomplete previous environment…)"
  rm -rf "$VENV"
fi

if [ ! -d "$VENV" ]; then
  if $PYTHON_CMD -m venv "$VENV"; then
    echo "  ✓  Environment created at ~/.metanas/.venv"
  else
    echo ""
    echo "  ✗  Failed to create Python environment."
    echo "     Try running:  python3 -m venv ~/.metanas/.venv"
    echo ""
    read -n 1 -s -r -p "  Press any key to exit…"
    exit 1
  fi
else
  echo "  ✓  Existing environment found"
fi

# ── 4. Install / update Python packages ──────────────────────────────────────
echo ""
echo "  Installing packages — this takes 3–5 minutes…"
echo "  (flask, openai, google-genai, whisper, scenedetect, numpy…)"
echo ""

$PIP_CMD install --upgrade pip --quiet

if $PIP_CMD install -r "$DIR/requirements.txt"; then
  echo ""
  echo "  ✓  All packages installed"
else
  echo ""
  echo "  ✗  Some packages failed to install."
  echo "     Check your internet connection and try again by"
  echo "     re-launching METANAS.app"
  echo ""
  # Remove venv so next launch re-tries
  rm -rf "$VENV"
  read -n 1 -s -r -p "  Press any key to exit…"
  exit 1
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "  ────────────────────────────────────────────"
echo "   ✓  METANAS is ready!"
echo "  ────────────────────────────────────────────"
echo ""
echo "  The app will open automatically."
echo "  First step: go to Settings and add your"
echo "  Gemini or OpenAI API key."
echo ""
