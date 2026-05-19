#!/usr/bin/env bash
#
# Merge every .mp4 in an input directory into a single web-optimized clip.
# - Each input is scaled-to-fit a common 16:9 frame (no distortion) and padded with black.
# - All inputs are normalized to a common fps and SAR before concat so the filter graph is sound.
# - A short fade-in / fade-out is applied per clip for a softer transition between sources.
# - Output is H.264 + AAC silent track + yuv420p + faststart, which plays in every modern browser.
#
# Usage:
#   scripts/merge_videos.sh                                   # uses defaults
#   scripts/merge_videos.sh assets assets/teaser/teaser.mp4   # explicit in/out
#   TARGET_W=1280 TARGET_H=720 FPS=30 scripts/merge_videos.sh # override via env
#
# Sort order: alphabetical by filename. Rename inputs (e.g. `01_*.mp4`, `02_*.mp4`)
# to control sequencing.

set -euo pipefail

INPUT_DIR="${1:-assets}"
OUTPUT="${2:-assets/teaser/teaser.mp4}"

TARGET_W="${TARGET_W:-1920}"
TARGET_H="${TARGET_H:-1080}"
FPS="${FPS:-30}"
FADE="${FADE:-0.25}"   # seconds of fade-in / fade-out per clip; set to 0 to disable
CRF="${CRF:-20}"        # H.264 quality (lower = better; 18-23 is the typical web range)
PRESET="${PRESET:-medium}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "error: ffmpeg not found on PATH" >&2
  exit 1
fi
if ! command -v ffprobe >/dev/null 2>&1; then
  echo "error: ffprobe not found on PATH" >&2
  exit 1
fi
if [[ ! -d "$INPUT_DIR" ]]; then
  echo "error: input directory '$INPUT_DIR' does not exist" >&2
  exit 1
fi

# Collect inputs (sorted, top-level only — does not recurse).
shopt -s nullglob
mapfile -t INPUTS < <(find "$INPUT_DIR" -maxdepth 1 -type f -iname '*.mp4' | sort)
shopt -u nullglob

if [[ ${#INPUTS[@]} -eq 0 ]]; then
  echo "error: no .mp4 files found in '$INPUT_DIR'" >&2
  exit 1
fi

echo "Merging ${#INPUTS[@]} clip(s) -> $OUTPUT"
for f in "${INPUTS[@]}"; do
  echo "  - $f"
done

mkdir -p "$(dirname "$OUTPUT")"

# Build the filter graph:
#   For each input:
#     scale to fit inside TARGET_W x TARGET_H (preserve aspect, no upscale-then-crop),
#     pad to exactly TARGET_W x TARGET_H with black,
#     set SAR=1, fps=$FPS, format=yuv420p,
#     optional fade-in at start and fade-out at the end of the clip.
#   Then concat all normalized streams.
FILTER=""
CONCAT_INPUTS=""
INPUT_ARGS=()

for i in "${!INPUTS[@]}"; do
  src="${INPUTS[$i]}"
  INPUT_ARGS+=("-i" "$src")

  # Per-clip duration (seconds, float). Used to position the fade-out.
  dur=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration \
        -of default=noprint_wrappers=1:nokey=1 "$src")
  # Fall back to format duration if stream duration is missing.
  if [[ -z "$dur" || "$dur" == "N/A" ]]; then
    dur=$(ffprobe -v error -show_entries format=duration \
          -of default=noprint_wrappers=1:nokey=1 "$src")
  fi

  fade_chain=""
  if awk "BEGIN{exit !($FADE > 0 && $dur > 2*$FADE)}"; then
    fade_out_start=$(awk "BEGIN{printf \"%.3f\", $dur - $FADE}")
    fade_chain=",fade=t=in:st=0:d=$FADE,fade=t=out:st=$fade_out_start:d=$FADE"
  fi

  FILTER+="[$i:v]scale=w=${TARGET_W}:h=${TARGET_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
  FILTER+="pad=${TARGET_W}:${TARGET_H}:(ow-iw)/2:(oh-ih)/2:color=black,"
  FILTER+="setsar=1,fps=${FPS},format=yuv420p${fade_chain}[v$i];"
  CONCAT_INPUTS+="[v$i]"
done

FILTER+="${CONCAT_INPUTS}concat=n=${#INPUTS[@]}:v=1:a=0[vout]"

# A silent stereo audio track keeps the file compatible with browsers / players that
# expect both streams. Generated via lavfi; trimmed to the merged video length by -shortest.
ffmpeg -y \
  "${INPUT_ARGS[@]}" \
  -f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000" \
  -filter_complex "$FILTER" \
  -map "[vout]" -map "${#INPUTS[@]}:a" \
  -c:v libx264 -preset "$PRESET" -crf "$CRF" -pix_fmt yuv420p \
  -profile:v high -level 4.1 \
  -movflags +faststart \
  -c:a aac -b:a 128k -shortest \
  "$OUTPUT"

echo
echo "Done: $OUTPUT"
ffprobe -v error -show_entries format=duration:stream=width,height,codec_name \
        -of default=noprint_wrappers=1 "$OUTPUT" || true
