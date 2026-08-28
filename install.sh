#!/usr/bin/env bash
# install.sh - Bootstrap Mac for Ry's Finder Services automations
# Run once after a fresh macOS setup or a new machine clone
# Place this script in the same folder as the *.workflow bundles

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}▶ $*${NC}"; }
warn()    { echo -e "${YELLOW}⚠ $*${NC}"; }
missing() { echo -e "${RED}✗ $*${NC}"; }

# 1. Homebrew
if ! command -v brew &>/dev/null; then
  info "Installing Homebrew…"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

  # Add brew to PATH for Apple Silicon
  if [[ -f /opt/homebrew/bin/brew ]]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
  fi
else
  info "Homebrew already installed — updating…"
  brew update --quiet
fi

# 2. Install workflow bundles
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES_DIR="$HOME/Library/Services"

info "Installing workflow bundles to ~/Library/Services…"
mkdir -p "$SERVICES_DIR"

shopt -s nullglob
WORKFLOWS=("$SCRIPT_DIR"/*.workflow)

if [[ ${#WORKFLOWS[@]} -eq 0 ]]; then
  warn "No .workflow files found next to install.sh — skipping workflow install."
else
  for wf in "${WORKFLOWS[@]}"; do
    name="$(basename "$wf")"
    dest="$SERVICES_DIR/$name"
    if [[ -d "$dest" ]]; then
      echo "  updating $name…"
      rm -rf "$dest"
    else
      echo "  installing $name…"
    fi
    cp -R "$wf" "$dest"
  done

  # Register the new services with macOS without requiring a logout/login
  # pbs (pasteboard server) manages the Services menu - -update rescans ~/Library/Services
  /System/Library/CoreServices/pbs -update
  echo "  ✓ Services menu refreshed"
fi

# 3. CLI tools
# Required by the workflows:
#   ffmpeg        -> Compress Movie, Convert to MP4, Extract First Frame
#   imagemagick   -> Convert to JPEG/PNG/SVG, Resize Images, Trim Images
#   gifsicle      -> Enable/Disable Looping, Optimize Images
#   potrace       -> Convert to SVG  (bitmap tracing)
#   optipng       -> Optimize Images (PNG lossless)
#   pngquant      -> Optimize Images (PNG quantisation)
#   jpegoptim     -> Optimize Images (JPEG strip & compress)
#   webp          -> Optimize Images (cwebp lossy WebP)

FORMULAE=(
  ffmpeg
  imagemagick
  gifsicle
  potrace
  optipng
  pngquant
  jpegoptim
  webp
)

info "Installing Homebrew formulae…"
for formula in "${FORMULAE[@]}"; do
  if brew list --formula "$formula" &>/dev/null; then
    echo "  ✓ $formula already installed"
  else
    echo "  installing $formula…"
    brew install "$formula"
  fi
done

# 4. Verify binaries at expected paths
info "Verifying binary paths used in workflows…"

declare -A BINS=(
  [ffmpeg]="/opt/homebrew/bin/ffmpeg"
  [magick]="/opt/homebrew/bin/magick"
  [gifsicle]="/opt/homebrew/bin/gifsicle"
  [potrace]="/opt/homebrew/bin/potrace"
  [optipng]="/opt/homebrew/bin/optipng"
  [pngquant]="/opt/homebrew/bin/pngquant"
  [jpegoptim]="/opt/homebrew/bin/jpegoptim"
  [cwebp]="/opt/homebrew/bin/cwebp"
)

ALL_OK=true
for name in "${!BINS[@]}"; do
  path="${BINS[$name]}"
  if [[ -x "$path" ]]; then
    echo "  ✓ $name → $path"
  else
    # Intel Macs install to /usr/local/bin - check there too
    alt="/usr/local/bin/$name"
    if [[ -x "$alt" ]]; then
      warn "$name found at $alt (Intel path) — workflows hardcode /opt/homebrew; consider symlinking."
    else
      missing "$name not found at $path"
      ALL_OK=false
    fi
  fi
done

# 5. Adobe Photoshop check (Convert to GIF workflow)
echo ""
if ls -d /Applications/Adobe\ Photoshop\ *.app &>/dev/null 2>&1; then
  PS_APP=$(ls -d /Applications/Adobe\ Photoshop\ *.app 2>/dev/null | sort -r | head -1)
  echo "  ✓ Photoshop → $PS_APP"
else
  warn "Adobe Photoshop not found — the 'Convert to GIF' workflow requires it."
  warn "Install via Creative Cloud: https://creativecloud.adobe.com"
fi

# 6. Done
echo ""
if $ALL_OK; then
  info "All done. Your Mac is ready for the Finder Services automations."
else
  missing "Some binaries are missing — re-run this script or install them manually."
  exit 1
fi
