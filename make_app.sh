#!/bin/sh
# Build "Nemotron Agent.app" (a menu bar app with the orb icon). install.sh runs this for you.
#   ./make_app.sh            -> /Applications/Nemotron Agent.app
#   ./make_app.sh ~/Applications
# The app is a small launcher: it runs src/app.py from this folder, so keep the folder where it is.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-/Applications}"
APP="$DEST/Nemotron Agent.app"
[ -x "$HERE/.venv/bin/python" ] || { echo "Run ./install.sh first."; exit 1; }
mkdir -p "$DEST"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$HERE/assets/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Nemotron Agent</string>
  <key>CFBundleDisplayName</key><string>Nemotron Agent</string>
  <key>CFBundleIdentifier</key><string>ai.nemotron-agent.app</string>
  <key>CFBundleExecutable</key><string>nemotron-agent</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>LSArchitecturePriority</key><array><string>arm64</string></array>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Hold the backtick key and speak a task; the audio is turned into text on this Mac.</string>
  <key>NSAppleEventsUsageDescription</key><string>The agent checks which app is in front so keys go to the right app.</string>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/nemotron-agent" <<SH
#!/bin/sh
# Starts the agent from its project folder. Output and crashes go to ~/Library/Logs/NemotronAgent.log
cd "$HERE"
pkill -f "Python -u src/app.py" 2>/dev/null
export HF_HUB_OFFLINE=1
# arch -arm64: a script launched from an app bundle can start in Intel (Rosetta) mode, and child processes
# inherit it; then pyautogui's rubicon thinks it is on x86 and crashes on import (objc_msgSendSuper_stret).
exec /usr/bin/arch -arm64 "$HERE/.venv/bin/python" -u src/app.py >> "\$HOME/Library/Logs/NemotronAgent.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/nemotron-agent"
touch "$APP"   # refresh the Finder icon cache
echo "built: $APP"
