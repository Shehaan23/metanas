#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  METANAS — macOS Build Script
#  Builds a signed, notarized DMG ready for distribution.
#
#  Usage:  ./build_mac.sh
#  Run from the repo root (~/Desktop/metanas)
#
#  Requirements:
#    • Xcode command-line tools
#    • Developer ID signing identity
#    • Notary profile (xcrun notarytool store-credentials)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="METANAS"
BUNDLE_ID="com.assortcreative.metanas"
SIGN_IDENTITY="Developer ID Application: Shehaan Thahir (XQR7RJMA5Q)"
NOTARY_PROFILE="metanas-notary"
MIN_MACOS="12.0"
COPYRIGHT="© 2026 Assort Creative Pvt Ltd"

# Extract version from app.py
VERSION=$(grep -m1 'APP_VERSION' "$REPO_DIR/footage-tagger/app.py" | sed 's/.*"\(.*\)".*/\1/')
if [ -z "$VERSION" ]; then
  echo "❌ Could not read APP_VERSION from footage-tagger/app.py"
  exit 1
fi

echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   METANAS macOS Build                    ║"
echo "  ║   Version: $VERSION                        ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""

# ── Paths ────────────────────────────────────────────────────────────────────
BUILD_DIR="$REPO_DIR/build"
APP_DIR="$BUILD_DIR/$APP_NAME.app"
CONTENTS="$APP_DIR/Contents"
MACOS="$CONTENTS/MacOS"
RESOURCES="$CONTENTS/Resources"
TAGGER_DEST="$RESOURCES/footage-tagger"
DMG_NAME="${APP_NAME}_v${VERSION}.dmg"
DMG_PATH="$BUILD_DIR/$DMG_NAME"

# ── Clean previous build ────────────────────────────────────────────────────
echo "  🧹 Cleaning previous build…"
rm -rf "$BUILD_DIR"
mkdir -p "$MACOS" "$RESOURCES" "$TAGGER_DEST"

# ── 1. Copy icon assets from installed app ──────────────────────────────────
echo "  🎨 Copying icon assets…"

# Try to find existing icon assets (in order of preference)
ICON_SOURCE=""
for candidate in \
  "/Applications/$APP_NAME.app/Contents/Resources/METANAS.icns" \
  "$HOME/Desktop/build_fresh/$APP_NAME.app/Contents/Resources/METANAS.icns" \
  "$HOME/Downloads/03 — METANAS/Builds/$APP_NAME.app/Contents/Resources/METANAS.icns"; do
  if [ -f "$candidate" ]; then
    ICON_SOURCE="$(dirname "$candidate")"
    break
  fi
done

if [ -n "$ICON_SOURCE" ]; then
  # Copy .icns file
  cp "$ICON_SOURCE/METANAS.icns" "$RESOURCES/"
  # Copy .png if it exists
  [ -f "$ICON_SOURCE/METANAS.png" ] && cp "$ICON_SOURCE/METANAS.png" "$RESOURCES/"
  # Copy iconset folder if it exists
  [ -d "$ICON_SOURCE/METANAS.iconset" ] && cp -R "$ICON_SOURCE/METANAS.iconset" "$RESOURCES/"
  echo "  ✓ Icons copied from $(dirname "$ICON_SOURCE")"
else
  echo "  ⚠ No existing METANAS.icns found — app will use default macOS icon"
  echo "    Place METANAS.icns in $RESOURCES/ and re-run if you want a custom icon"
fi

# ── 2. Copy application code ───────────────────────────────────────────────
echo "  📦 Copying application code…"

# Main app files into footage-tagger/
cp "$REPO_DIR/footage-tagger/app.py"             "$TAGGER_DEST/"
cp "$REPO_DIR/footage-tagger/footage_tagger.py"  "$TAGGER_DEST/"
cp "$REPO_DIR/footage-tagger/requirements.txt"   "$TAGGER_DEST/"
cp "$REPO_DIR/footage-tagger/install.sh"         "$TAGGER_DEST/"
cp "$REPO_DIR/footage-tagger/start.sh"           "$TAGGER_DEST/"

# Copy search.py if it exists
[ -f "$REPO_DIR/footage-tagger/search.py" ] && \
  cp "$REPO_DIR/footage-tagger/search.py" "$TAGGER_DEST/"

# Legacy: some versions expect app.py at Resources root too
cp "$REPO_DIR/footage-tagger/app.py" "$RESOURCES/app.py"

chmod +x "$TAGGER_DEST/install.sh" "$TAGGER_DEST/start.sh"

echo "  ✓ Code copied"

# ── 3. Write Info.plist ─────────────────────────────────────────────────────
echo "  📋 Writing Info.plist (v$VERSION)…"

cat > "$CONTENTS/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>              <string>$APP_NAME</string>
  <key>CFBundleDisplayName</key>       <string>$APP_NAME</string>
  <key>CFBundleIdentifier</key>        <string>$BUNDLE_ID</string>
  <key>CFBundleVersion</key>           <string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key>        <string>$APP_NAME</string>
  <key>CFBundlePackageType</key>       <string>APPL</string>
  <key>CFBundleSignature</key>         <string>????</string>
  <key>LSMinimumSystemVersion</key>    <string>$MIN_MACOS</string>
  <key>LSUIElement</key>               <false/>
  <key>NSHighResolutionCapable</key>   <true/>
  <key>NSHumanReadableCopyright</key>  <string>$COPYRIGHT</string>
  <key>CFBundleIconFile</key>          <string>METANAS</string>
</dict>
</plist>
PLIST

echo "  ✓ Info.plist written"

# ── 4. Write launcher script ───────────────────────────────────────────────
echo "  🚀 Writing launcher script…"

cat > "$MACOS/$APP_NAME" << 'LAUNCHER'
#!/bin/bash
# METANAS — macOS App Launcher
# This script lives inside METANAS.app/Contents/MacOS/

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RESOURCES="$SCRIPT_DIR/../Resources"
TAGGER="$RESOURCES/footage-tagger"
METANAS_HOME="$HOME/.metanas"
VENV="$METANAS_HOME/.venv"
PORT=5151
LOG="$METANAS_HOME/metanas.log"

dialog() {
  osascript -e "display dialog \"$1\" buttons {\"$2\"} default button \"$2\" with title \"METANAS\""
}

alert() {
  osascript -e "display alert \"METANAS\" message \"$1\""
}

if lsof -ti tcp:$PORT &>/dev/null; then
  open "http://localhost:$PORT"
  exit 0
fi

if [ ! -f "$VENV/bin/python3" ]; then
  SETUP_CMD=$(mktemp /tmp/metanas_setup_XXXX.command)
  cat > "$SETUP_CMD" << HEREDOC
#!/bin/bash
clear
echo ''
echo '  ╔══════════════════════════════════════════╗'
echo '  ║   METANAS — First-time Setup             ║'
echo '  ║   This takes about 3-5 minutes.          ║'
echo '  ║   Do NOT close this window.              ║'
echo '  ╚══════════════════════════════════════════╝'
echo ''
cd '$TAGGER'
bash install.sh
echo ''
echo '  Setup complete! Launching METANAS...'
sleep 1
open '$SCRIPT_DIR/../../../METANAS.app'
HEREDOC
  chmod +x "$SETUP_CMD"
  open "$SETUP_CMD"
  exit 0
fi

(
  export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

  if ! command -v ffmpeg &>/dev/null; then
    if command -v brew &>/dev/null; then
      brew install ffmpeg --quiet 2>/dev/null
    else
      ARCH=$(uname -m)
      [ "$ARCH" = "arm64" ] && URL="https://www.osxexperts.net/ffmpeg7arm.zip" \
                             || URL="https://www.osxexperts.net/ffmpeg7intel.zip"
      TMP=$(mktemp /tmp/ffmpeg_XXXX.zip)
      curl -fsSL "$URL" -o "$TMP" 2>/dev/null && \
        sudo mkdir -p /usr/local/bin && \
        sudo unzip -o -j "$TMP" ffmpeg  -d /usr/local/bin/ 2>/dev/null && \
        sudo unzip -o -j "$TMP" ffprobe -d /usr/local/bin/ 2>/dev/null && \
        sudo chmod +x /usr/local/bin/ffmpeg /usr/local/bin/ffprobe 2>/dev/null
      rm -f "$TMP"
    fi
  fi

  if ! command -v exiftool &>/dev/null; then
    EXIF_PKG=$(curl -sf https://exiftool.org/ | grep -o 'ExifTool-[0-9.]*\.pkg' | head -1)
    [ -z "$EXIF_PKG" ] && EXIF_PKG="ExifTool-13.25.pkg"
    TMP="/tmp/$EXIF_PKG"
    curl -fsSL "https://exiftool.org/$EXIF_PKG" -o "$TMP" 2>/dev/null && \
      sudo installer -pkg "$TMP" -target / 2>/dev/null
    rm -f "$TMP"
  fi
) &

REQUIREMENTS_HASH_FILE="$METANAS_HOME/.requirements_hash"
CURRENT_HASH=$(md5 -q "$TAGGER/requirements.txt" 2>/dev/null || echo "none")
STORED_HASH=$(cat "$REQUIREMENTS_HASH_FILE" 2>/dev/null || echo "")
if [ "$CURRENT_HASH" != "$STORED_HASH" ]; then
  if [ "$(uname -m)" = "arm64" ]; then
    arch -arm64 "$VENV/bin/pip" install -r "$TAGGER/requirements.txt" --quiet 2>/dev/null
  else
    "$VENV/bin/pip" install -r "$TAGGER/requirements.txt" --quiet 2>/dev/null
  fi
  echo "$CURRENT_HASH" > "$REQUIREMENTS_HASH_FILE"
fi

cd "$TAGGER"

nohup "$VENV/bin/python3" app.py >> "$LOG" 2>&1 &
FLASK_PID=$!

for i in $(seq 1 16); do
  sleep 0.5
  if lsof -ti tcp:$PORT &>/dev/null; then
    break
  fi
  if ! kill -0 $FLASK_PID 2>/dev/null; then
    osascript << APPLESCRIPT
      tell application "Terminal"
        activate
        do script "echo 'METANAS failed to start. Log:' && tail -30 '$LOG'"
      end tell
APPLESCRIPT
    exit 1
  fi
done

open "http://localhost:$PORT"
LAUNCHER

chmod +x "$MACOS/$APP_NAME"
echo "  ✓ Launcher written"

# ── 5. Code sign ────────────────────────────────────────────────────────────
echo "  🔏 Signing with: $SIGN_IDENTITY"

codesign --deep --force --options runtime \
  --sign "$SIGN_IDENTITY" \
  --timestamp \
  "$APP_DIR"

# Verify
if codesign --verify --deep --strict "$APP_DIR" 2>/dev/null; then
  echo "  ✓ Signature verified"
else
  echo "  ❌ Signature verification failed!"
  exit 1
fi

# ── 6. Create DMG ──────────────────────────────────────────────────────────
echo "  💿 Creating DMG: $DMG_NAME"

# Create a temporary folder for the DMG contents
DMG_STAGING="$BUILD_DIR/dmg_staging"
mkdir -p "$DMG_STAGING"
cp -R "$APP_DIR" "$DMG_STAGING/"

# Add a symlink to /Applications for drag-to-install
ln -s /Applications "$DMG_STAGING/Applications"

# Create the DMG
hdiutil create -volname "$APP_NAME" \
  -srcfolder "$DMG_STAGING" \
  -ov -format UDZO \
  "$DMG_PATH" \
  -quiet

rm -rf "$DMG_STAGING"

# Sign the DMG itself
codesign --force --sign "$SIGN_IDENTITY" --timestamp "$DMG_PATH"
echo "  ✓ DMG created and signed"

# ── 7. Notarize ─────────────────────────────────────────────────────────────
echo "  📮 Submitting for notarization…"
echo "     (this can take 2-10 minutes)"

xcrun notarytool submit "$DMG_PATH" \
  --keychain-profile "$NOTARY_PROFILE" \
  --wait

# Check result
if xcrun notarytool info "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" 2>&1 | grep -q "Accepted"; then
  echo "  ✓ Notarization accepted"
else
  echo "  ⚠ Check notarization status manually:"
  echo "    xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE"
fi

# Staple the notarization ticket to the DMG
xcrun stapler staple "$DMG_PATH"
echo "  ✓ Notarization ticket stapled"

# ── Done ────────────────────────────────────────────────────────────────────
echo ""
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   ✓  Build complete!                     ║"
echo "  ║                                          ║"
echo "  ║   DMG: build/$DMG_NAME"
echo "  ║   Version: $VERSION                        ║"
echo "  ╚══════════════════════════════════════════╝"
echo ""
echo "  Upload $DMG_PATH to Gumroad."
echo ""
