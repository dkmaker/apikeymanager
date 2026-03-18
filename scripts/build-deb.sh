#!/usr/bin/env bash
# build-deb.sh — Build the op-keysync .deb package locally
#
# Usage:
#   ./scripts/build-deb.sh [VERSION]
#
# Requires: dpkg-deb (from dpkg package), gzip
# No debhelper or build system needed — uses dpkg-deb --build directly.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(grep -m1 "^op-keysync" "$REPO_ROOT/debian/changelog" | grep -oP '\(\K[^)]+' | cut -d- -f1)}"
PKG_VERSION="${VERSION}-1"
ARCH="all"
PKG_NAME="op-keysync_${PKG_VERSION}_${ARCH}"
BUILD_DIR="$REPO_ROOT/build"
STAGE="$BUILD_DIR/$PKG_NAME"

echo "▶ Building op-keysync $PKG_VERSION ..."

# ── Clean and create staging tree ─────────────────────────────────────────────
rm -rf "$BUILD_DIR"
mkdir -p \
    "$STAGE/DEBIAN" \
    "$STAGE/usr/bin" \
    "$STAGE/usr/share/op-keysync" \
    "$STAGE/usr/share/applications"

# ── Copy files ─────────────────────────────────────────────────────────────────
cp "$REPO_ROOT/app/op-keysync.py"        "$STAGE/usr/share/op-keysync/"
cp "$REPO_ROOT/app/op-keysync-shell.sh"  "$STAGE/usr/share/op-keysync/"
cp "$REPO_ROOT/app/op-keysync.desktop"   "$STAGE/usr/share/applications/"
cp "$REPO_ROOT/scripts/op-keysync"       "$STAGE/usr/bin/"

# ── Permissions ────────────────────────────────────────────────────────────────
chmod 755 "$STAGE/usr/bin/op-keysync"
chmod 755 "$STAGE/usr/share/op-keysync/op-keysync.py"
chmod 644 "$STAGE/usr/share/op-keysync/op-keysync-shell.sh"
chmod 644 "$STAGE/usr/share/applications/op-keysync.desktop"

# ── DEBIAN control files ────────────────────────────────────────────────────────
# Compute installed size (in KB)
INSTALLED_SIZE=$(du -sk "$STAGE/usr" | cut -f1)

# Write the binary control file directly
cat > "$STAGE/DEBIAN/control" <<EOF
Package: op-keysync
Version: ${PKG_VERSION}
Architecture: all
Installed-Size: ${INSTALLED_SIZE}
Maintainer: op-keysync maintainer <noreply@github.com>
Homepage: https://github.com/dkmaker/apikeymanager
Section: utils
Priority: optional
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, python3-dbus, python3-cryptography, socat, wl-clipboard, libnotify-bin
Recommends: 1password-cli
Description: 1Password API key sync tray app for GNOME
 Fetches API keys from a 1Password vault and injects them as environment
 variables into local shell sessions, entirely in RAM, never written to disk.
 .
 Features:
  - Automatic purge on screen lock and after 60 minutes idle
  - Re-fetches from 1Password on unlock and activity resume
  - Clipboard copy with 20-second auto-clear
  - SSH sessions are denied the keys entirely
  - GNOME tray icon showing sync and lock state
EOF

cp "$REPO_ROOT/debian/postinst"  "$STAGE/DEBIAN/"
cp "$REPO_ROOT/debian/postrm"    "$STAGE/DEBIAN/"
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/postrm"

# ── Build .deb ─────────────────────────────────────────────────────────────────
DEB_FILE="$BUILD_DIR/${PKG_NAME}.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$DEB_FILE"

echo ""
echo "✅ Built: $DEB_FILE"
echo ""
echo "Install locally with:"
echo "  sudo apt install \"$DEB_FILE\""
