#!/usr/bin/env sh
# Install tofu-overlay from a GitHub release (single-file binary) or from PyPI.
#
#   curl -fsSL https://raw.githubusercontent.com/KamorionLabs/tofu-overlay/main/scripts/install.sh | sh -s -- 0.1.0
#
# Environment:
#   TOFU_OVERLAY_VERSION   version to install (or first argument); "latest" resolves the latest release
#   TOFU_OVERLAY_BIN_DIR   install directory (default /usr/local/bin, falls back to ~/.local/bin)
#   TOFU_OVERLAY_METHOD    binary (default) | pypi
#   GITHUB_TOKEN           optional, required while the repository is private
set -eu

REPO="KamorionLabs/tofu-overlay"
VERSION="${1:-${TOFU_OVERLAY_VERSION:-latest}}"
METHOD="${TOFU_OVERLAY_METHOD:-binary}"
BIN_DIR="${TOFU_OVERLAY_BIN_DIR:-/usr/local/bin}"
AUTH=""
[ -n "${GITHUB_TOKEN:-}" ] && AUTH="Authorization: token ${GITHUB_TOKEN}"

resolve_latest() {
  curl -fsSL ${AUTH:+-H "$AUTH"} "https://api.github.com/repos/${REPO}/releases/latest" \
    | sed -n 's/.*"tag_name": *"v\([^"]*\)".*/\1/p' | head -1
}

if [ "$VERSION" = "latest" ]; then
  VERSION="$(resolve_latest)"
  [ -n "$VERSION" ] || { echo "cannot resolve latest release" >&2; exit 1; }
fi

if [ "$METHOD" = "pypi" ]; then
  if command -v uv >/dev/null 2>&1; then
    uv tool install --python 3.12 "kmr-tofu-overlay==${VERSION}"
  elif command -v pipx >/dev/null 2>&1; then
    pipx install "kmr-tofu-overlay==${VERSION}"
  else
    echo "pypi method needs uv or pipx" >&2; exit 1
  fi
  exit 0
fi

OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH="$(uname -m)"
case "$OS/$ARCH" in
  linux/x86_64) ASSET="tofu-overlay-linux-amd64" ;;
  *) echo "no prebuilt binary for $OS/$ARCH; use TOFU_OVERLAY_METHOD=pypi" >&2; exit 1 ;;
esac

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Public repository: plain release download URLs. Private repository (GITHUB_TOKEN set):
# browser download URLs return 404, assets must be fetched through the API asset URL.
download_asset() {
  name="$1"; dest="$2"
  if [ -n "$AUTH" ]; then
    url="$(curl -fsSL -H "$AUTH" "https://api.github.com/repos/${REPO}/releases/tags/v${VERSION}" \
      | tr ',' '\n' | grep -B0 -A0 '"url": *"https://api.github.com/repos/[^"]*/releases/assets/[0-9]*"' \
      | sed -n 's/.*"url": *"\([^"]*\)".*/\1/p' | while read -r u; do
          curl -fsSL -H "$AUTH" "$u" | grep -q "\"name\": *\"$name\"" && echo "$u" && break
        done)"
    [ -n "$url" ] || { echo "asset $name not found in release v${VERSION}" >&2; exit 1; }
    curl -fsSL -H "$AUTH" -H "Accept: application/octet-stream" -o "$dest" "$url"
  else
    curl -fsSL -o "$dest" "https://github.com/${REPO}/releases/download/v${VERSION}/${name}"
  fi
}
download_asset "$ASSET" "$TMP/$ASSET"
download_asset "SHA256SUMS" "$TMP/SHA256SUMS"
EXPECTED="$(grep " $ASSET\$" "$TMP/SHA256SUMS" | cut -d' ' -f1)"
ACTUAL="$(sha256sum "$TMP/$ASSET" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$TMP/$ASSET" | cut -d' ' -f1)"
[ "$EXPECTED" = "$ACTUAL" ] || { echo "checksum mismatch for $ASSET" >&2; exit 1; }
chmod +x "$TMP/$ASSET"
if [ -w "$BIN_DIR" ] || { [ ! -e "$BIN_DIR" ] && mkdir -p "$BIN_DIR" 2>/dev/null; }; then
  mv "$TMP/$ASSET" "$BIN_DIR/tofu-overlay"
elif command -v sudo >/dev/null 2>&1; then
  sudo mv "$TMP/$ASSET" "$BIN_DIR/tofu-overlay"
else
  BIN_DIR="$HOME/.local/bin"; mkdir -p "$BIN_DIR"; mv "$TMP/$ASSET" "$BIN_DIR/tofu-overlay"
fi
echo "installed $("$BIN_DIR/tofu-overlay" version) at $BIN_DIR/tofu-overlay"
