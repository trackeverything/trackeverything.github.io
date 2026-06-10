#!/usr/bin/env bash
#
# Convert .mov clips to web-optimized .mp4 for broad browser playback.
# Output is H.264 + yuv420p + faststart (no audio), matching merge_videos.sh encoding.
#
# Usage:
#   scripts/convert_mov_to_mp4.sh assets/pstudio_1.mov assets/twirl_1.mov
#   scripts/convert_mov_to_mp4.sh assets/*.mov
#   scripts/convert_mov_to_mp4.sh                    # converts all .mov under assets/
#   CRF=18 PRESET=slow scripts/convert_mov_to_mp4.sh # override quality via env
#
# Each input foo.mov is written to foo.mp4 in the same directory unless OUTPUT_DIR is set.

set -euo pipefail

CRF="${CRF:-20}"
PRESET="${PRESET:-medium}"
OUTPUT_DIR="${OUTPUT_DIR:-}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "error: ffmpeg not found on PATH" >&2
  exit 1
fi

INPUTS=()
if [[ $# -gt 0 ]]; then
  INPUTS=("$@")
elif [[ -d assets ]]; then
  while IFS= read -r file; do
    INPUTS+=("$file")
  done < <(find assets -type f -name '*.mov' | sort)
else
  echo "error: no input files and no assets/ directory found" >&2
  exit 1
fi

if [[ ${#INPUTS[@]} -eq 0 ]]; then
  echo "error: no .mov files to convert" >&2
  exit 1
fi

convert_one() {
  local input="$1"
  local base output

  if [[ ! -f "$input" ]]; then
    echo "error: input file '$input' does not exist" >&2
    exit 1
  fi

  base="${input%.*}"
  if [[ -n "$OUTPUT_DIR" ]]; then
    mkdir -p "$OUTPUT_DIR"
    output="$OUTPUT_DIR/$(basename "$base").mp4"
  else
    output="${base}.mp4"
  fi

  echo "Converting: $input -> $output"

  ffmpeg -y -hide_banner -loglevel error -stats \
    -i "$input" \
    -an \
    -c:v libx264 -preset "$PRESET" -crf "$CRF" -pix_fmt yuv420p \
    -profile:v high -level 4.1 \
    -movflags +faststart \
    "$output"

  echo "Done: $output"
  ffprobe -v error -show_entries format=duration:stream=width,height,codec_name \
          -of default=noprint_wrappers=1 "$output" || true
  echo
}

for input in "${INPUTS[@]}"; do
  convert_one "$input"
done

echo "Converted ${#INPUTS[@]} file(s)."
