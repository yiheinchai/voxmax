#!/bin/sh
# Builds "Hotspot Guard.app": the SwiftUI front end plus the Python engine it drives.
# Needs macOS 26 with Xcode 26 (or its command line tools). Output goes to app/build/.
set -eu
cd "$(dirname "$0")"

APP="build/Hotspot Guard.app"
ENGINE_SRC=".."

swift build -c release

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources/engine"
cp ".build/release/HotspotGuard" "$APP/Contents/MacOS/HotspotGuard"
cp "$ENGINE_SRC/hotspot_guard.py" "$ENGINE_SRC/firewall.py" "$ENGINE_SRC/hotspot-guard.ini" \
   "$APP/Contents/Resources/engine/"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Hotspot Guard</string>
    <key>CFBundleDisplayName</key><string>Hotspot Guard</string>
    <key>CFBundleIdentifier</key><string>local.hotspot-guard</string>
    <key>CFBundleExecutable</key><string>HotspotGuard</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>0.1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>26.0</string>
    <key>NSHighResolutionCapable</key><true/>
    <key>NSPrincipalClass</key><string>NSApplication</string>
</dict>
</plist>
PLIST

# Ad-hoc signature: enough to run on this Mac. Not notarised, so it is for local use only.
codesign --force --sign - "$APP"
echo "Built $APP"
