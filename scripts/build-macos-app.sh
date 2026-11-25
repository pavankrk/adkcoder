#!/usr/bin/env bash
set -euo pipefail

# Build a clickable macOS "ADK Coder.app" bundle in dist/ (opens natively with no terminal window).
ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="${1:-$ROOT_DIR/dist/ADK Coder.app}"
BIN_PATH="$ROOT_DIR/.venv/bin/adk-coder-app"

if [[ ! -x "$BIN_PATH" ]]; then
  echo "Error: $BIN_PATH not found. Create .venv and install adk-coder[local,desktop] first." >&2
  exit 1
fi

rm -rf "$APP_DIR"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"

cat > "$APP_DIR/Contents/Info.plist" << 'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>
  <string>ADK Coder</string>
  <key>CFBundleDisplayName</key>
  <string>ADK Coder</string>
  <key>CFBundleIdentifier</key>
  <string>dev.adkcoder.desktop</string>
  <key>CFBundleVersion</key>
  <string>2.1.0</string>
  <key>CFBundleShortVersionString</key>
  <string>2.1.0</string>
  <key>CFBundleExecutable</key>
  <string>ADK Coder</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>LSMinimumSystemVersion</key>
  <string>12.0</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST

cat > "$APP_DIR/Contents/MacOS/ADK Coder" << LAUNCHER
#!/usr/bin/env bash
export PATH="/opt/homebrew/bin:/usr/local/bin:\$HOME/.local/bin:\$PATH"
export ADK_CODER_DESKTOP=1
exec "$BIN_PATH" --workspace "$ROOT_DIR" "\$@"
LAUNCHER

chmod +x "$APP_DIR/Contents/MacOS/ADK Coder"
echo "Built macOS App Bundle: $APP_DIR"
echo "You can drag it into /Applications or launch with: open \"$APP_DIR\""
