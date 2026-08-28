#!/usr/bin/env bash
# mov-to-gif.sh
# Convert After Effects MOV files to animated GIFs via Photoshop Save for Web.
#
# Requirements:
#   - Adobe Photoshop (any recent version in /Applications)
#
# Usage:
#   mov-to-gif.sh [options] file.mov [file.mov …]
#
# Options:
#   -c COLORS     Colour count 2–256                          (default: 128)
#   -r REDUCTION  Color reduction: selective|perceptual|      (default: selective)
#                   adaptive|web
#   -d DITHER     Diffusion dither amount 0–100               (default: 88)
#   -w WIDTH      Resize output to WIDTH px wide, height auto  (default: 650)
#   -T            Enable transparency (off by default)
#   -h            Show this help and exit

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
OPT_COLORS=256
OPT_REDUCTION="adaptive"
OPT_DITHER=100
OPT_WIDTH=650
OPT_TRANSPARENCY=false

# ── Usage ─────────────────────────────────────────────────────────────────────
usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | sed -n '/^Usage/,/^$/p'
  exit 0
}

# ── Argument parsing ──────────────────────────────────────────────────────────
while getopts ":c:r:d:w:Th" opt; do
  case $opt in
    c) OPT_COLORS="$OPTARG"      ;;
    r) OPT_REDUCTION="$OPTARG"   ;;
    d) OPT_DITHER="$OPTARG"      ;;
    w) OPT_WIDTH="$OPTARG"       ;;
    T) OPT_TRANSPARENCY=true     ;;
    h) usage                     ;;
    :) echo "Option -$OPTARG requires an argument." >&2; exit 1 ;;
    ?) echo "Unknown option: -$OPTARG" >&2; exit 1             ;;
  esac
done
shift $(( OPTIND - 1 ))

if [[ $# -eq 0 ]]; then
  echo "Error: no input files specified. Run with -h for help." >&2
  exit 1
fi

# ── Find Photoshop ────────────────────────────────────────────────────────────
PS_APP=$(ls -d /Applications/Adobe\ Photoshop\ */Adobe\ Photoshop\ *.app 2>/dev/null | sort -r | head -1 || true)
if [[ -z "$PS_APP" ]]; then
  echo "Error: Adobe Photoshop not found in /Applications." >&2
  exit 1
fi
PS_NAME=$(basename "$PS_APP" .app)

# ── Temp directory (auto-cleaned on exit) ─────────────────────────────────────
WORK_DIR=$(mktemp -d /tmp/mov-to-gif-XXXXXX)
trap 'rm -rf "$WORK_DIR"' EXIT

# ── Photoshop MOV → GIF export ────────────────────────────────────────────────
ps_export() {
  local input="$1" output="$2"
  local transparency_val
  local jsx_file="$WORK_DIR/export.jsx"

  transparency_val="$( [[ $OPT_TRANSPARENCY == true ]] && echo "true" || echo "false" )"

  # Escape paths for use inside a JS double-quoted string
  local js_input js_output
  js_input="${input//\\/\\\\}"; js_input="${js_input//\"/\\\"}"
  js_output="${output//\\/\\\\}"; js_output="${js_output//\"/\\\"}"

  cat > "$jsx_file" <<JSEOF
(function () {
  app.displayDialogs = DialogModes.NO;

  var inputFile  = new File("${js_input}");
  var outputFile = new File("${js_output}");

  if (!inputFile.exists) {
    throw new Error("Input file not found: " + inputFile.fsName);
  }

  var doc = app.open(inputFile);

  var targetWidth  = ${OPT_WIDTH};
  var originalWidth  = doc.width.as("px");
  var originalHeight = doc.height.as("px");
  var targetHeight = Math.round(targetWidth * (originalHeight / originalWidth));

  doc.resizeImage(UnitValue(targetWidth, "px"), UnitValue(targetHeight, "px"), doc.resolution, ResampleMethod.BICUBIC);

  var opts          = new ExportOptionsSaveForWeb();
  opts.format       = SaveDocumentType.COMPUSERVEGIF;
  opts.colors       = ${OPT_COLORS};
  opts.dither       = Dither.DIFFUSION;
  opts.ditherAmount = ${OPT_DITHER};
  opts.transparency = ${transparency_val};
  opts.interlaced   = false;

  // ColorReduction enum was removed in Photoshop 2026; guard against its absence.
  // When omitted, Photoshop defaults to Selective — which is what we want anyway.
  if (typeof ColorReduction !== 'undefined') {
    var reductionMap = {
      selective:  ColorReduction.SELECTIVE,
      perceptual: ColorReduction.PERCEPTUAL,
      adaptive:   ColorReduction.ADAPTIVE,
      web:        ColorReduction.WEB
    };
    opts.colorReduction = reductionMap["${OPT_REDUCTION}"] || ColorReduction.SELECTIVE;
  }

  doc.exportDocument(outputFile, ExportType.SAVEFORWEB, opts);
  doc.close(SaveOptions.DONOTSAVECHANGES);
})();
JSEOF

  osascript -e "tell application \"${PS_NAME}\" to do javascript file \"${jsx_file}\""
}

# ── Main ──────────────────────────────────────────────────────────────────────
echo "Photoshop: $PS_NAME"
echo "Settings:  ${OPT_COLORS} colours · ${OPT_REDUCTION} reduction · ${OPT_DITHER}% dither · ${OPT_WIDTH}px wide · transparency=${OPT_TRANSPARENCY}"
echo ""

for input_arg in "$@"; do
  input="$(cd "$(dirname "$input_arg")" && pwd)/$(basename "$input_arg")"

  if [[ ! -f "$input" ]]; then
    echo "Warning: '${input_arg}' not found — skipping."
    continue
  fi

  output="${input%.*}.gif"
  echo "▶ $(basename "$input")"

  ps_export "$input" "$output"

  if [[ ! -f "$output" ]]; then
    echo "  Error: Photoshop did not produce an output file." >&2
    continue
  fi

  size=$(stat -f%z "$output")
  echo "  Done: $(awk "BEGIN { printf \"%.0fKB\", ${size} / 1024 }") → $(basename "$output")"
  echo ""
done

echo "Done."
