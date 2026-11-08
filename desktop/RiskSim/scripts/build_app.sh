#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
APP="$PWD/dist/RiskSim.app"

swift build -c release
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp .build/release/RiskSimApp "$APP/Contents/MacOS/RiskSimApp"
chmod +x "$APP/Contents/MacOS/RiskSimApp"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>ru</string>
  <key>CFBundleDisplayName</key><string>RiskSim</string>
  <key>CFBundleExecutable</key><string>RiskSimApp</string>
  <key>CFBundleIdentifier</key><string>com.risksim.desktop</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>RiskSim</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.8.0</string>
  <key>CFBundleVersion</key><string>0.8.0</string>
  <key>LSMinimumSystemVersion</key><string>15.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.business</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSPrincipalClass</key><string>NSApplication</string>
</dict>
</plist>
PLIST

if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "$APP" >/dev/null
fi
printf '%s\n' "$APP"
