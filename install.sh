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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SERVICES_DIR="$HOME/Library/Services"

mkdir -p "$SERVICES_DIR"
SERVICES_DIR="$(cd "$SERVICES_DIR" && pwd -P)"

shopt -s nullglob
WORKFLOWS=("$SCRIPT_DIR"/*.workflow)

if [[ ${#WORKFLOWS[@]} -eq 0 ]]; then
  warn "No .workflow files found next to install.sh - skipping workflow install"

elif [[ "$SCRIPT_DIR" == "$SERVICES_DIR" ]]; then
  # Cloning the repo straight into ~/Library/Services means the bundles are
  # already installed - copying one onto itself would destroy it
  info "Running from ~/Library/Services - bundles are already in place"

else
  info "Installing workflow bundles to ~/Library/Services…"

  for wf in "${WORKFLOWS[@]}"; do
    name="$(basename "$wf")"
    dest="$SERVICES_DIR/$name"

    if [[ -e "$dest" && "$wf" -ef "$dest" ]]; then
      echo "  skipping ${name} - source and destination are the same"
      continue
    fi

    if [[ -d "$dest" ]]; then
      echo "  updating ${name}…"
      rm -rf "$dest"

    else
      echo "  installing ${name}…"
    fi

    cp -R "$wf" "$dest"
  done
fi

if [[ ${#WORKFLOWS[@]} -gt 0 ]]; then
  # Register the services with macOS without requiring a logout/login
  # pbs (pasteboard server) manages the Services menu - -update rescans ~/Library/Services
  /System/Library/CoreServices/pbs -update
  echo "  ✓ Services menu refreshed"
fi

# 3. Install helper scripts
# Finder denies services read access to ~/Library/CloudStorage, so anything a
# workflow shells out to has to sit on local disk
BIN_DIR="$HOME/bin"
HELPERS=("$SCRIPT_DIR"/bin/*)

if [[ ${#HELPERS[@]} -eq 0 ]]; then
  warn "No helper scripts found in bin/ - skipping helper install"

else
  info "Installing helper scripts to ~/bin…"
  mkdir -p "$BIN_DIR"

  for helper in "${HELPERS[@]}"; do
    name="$(basename "$helper")"
    dest="$BIN_DIR/$name"

    if [[ -e "$dest" && "$helper" -ef "$dest" ]]; then
      echo "  skipping ${name} - source and destination are the same"
      continue
    fi

    echo "  installing ${name}…"
    cp "$helper" "$dest"
    chmod +x "$dest"
  done
fi

# 4. CLI tools
# Required by the workflows:
#   ffmpeg        -> Compress Movie, Convert to MP4, Extract First Frame
#   imagemagick   -> Convert to JPEG/PNG/SVG, Resize Images, Trim Images
#   gifsicle      -> Enable/Disable Looping, Optimize Images
#   potrace       -> Convert to SVG  (bitmap tracing)
#   optipng       -> Optimize Images (PNG lossless)
#   pngquant      -> Optimize Images (PNG quantisation)
#   jpegoptim     -> Optimize Images (JPEG strip & compress)
#   webp          -> Optimize Images (cwebp lossy WebP)
#
# Remove Colours additionally needs python3 (Xcode command line tools) and, for
# the colour picker window, Google Chrome - it falls back to the default browser

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
    echo "  installing ${formula}…"
    brew install "$formula"
  fi
done

# 5. Verify binaries at expected paths
info "Verifying binary paths used in workflows…"

# A plain list, not an associative array - macOS ships bash 3.2, which has none
BINS=(
  ffmpeg
  magick
  gifsicle
  potrace
  optipng
  pngquant
  jpegoptim
  cwebp
)

ALL_OK=true

for name in "${BINS[@]}"; do
  path="/opt/homebrew/bin/$name"

  if [[ -x "$path" ]]; then
    echo "  ✓ $name -> $path"
    continue
  fi

  # Intel Macs install to /usr/local/bin - check there too
  alt="/usr/local/bin/$name"

  if [[ -x "$alt" ]]; then
    warn "$name found at $alt (Intel path) - workflows hardcode /opt/homebrew; consider symlinking"

  else
    missing "$name not found at $path"
    ALL_OK=false
  fi
done

# 6. Adobe Photoshop check (Convert to GIF workflow)
echo ""

# Adobe nests the bundle inside a versioned folder, so the app is one level
# down - this is the same pattern the Convert to GIF workflow globs for
PS_APPS=(/Applications/Adobe\ Photoshop\ */Adobe\ Photoshop\ *.app)

if [[ ${#PS_APPS[@]} -gt 0 ]]; then
  # Globs expand in ascending order, so the newest version sorts last
  echo "  ✓ Photoshop -> ${PS_APPS[${#PS_APPS[@]} - 1]}"

else
  warn "Adobe Photoshop not found - the 'Convert to GIF' workflow requires it"
  warn "Install via Creative Cloud: https://creativecloud.adobe.com"
fi

# 7. Done
echo ""
if $ALL_OK; then
  info "All done. Your Mac is ready for the Finder Services automations."
else
  missing "Some binaries are missing — re-run this script or install them manually."
  exit 1
fi
