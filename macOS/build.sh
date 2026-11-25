#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
app='Relay 2.app'
target="$app/Contents/MacOS/Relay"
mkdir -p "$app/Contents/MacOS"
xcrun swiftc macOS/RelayMenu.swift -o "$target.new"
mv "$target.new" "$target"
codesign --force --sign - "$app"
