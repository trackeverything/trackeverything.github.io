#!/usr/bin/env python3
"""Build the TrackEverything teaser videos from the clips already in assets/.

  hero          assets/teaser/hero_loop.mp4     30s, 1440x540, silent, no text,
                                                seamlessly looping. For the top of
                                                the website.
  teaser-plain  assets/teaser/teaser_plain.mp4  54s, 1920x1080, silent, no text.
  teaser-text   assets/teaser/teaser_text.mp4   59s, 1920x1080, silent, with a
                                                title, beat captions and an end card.
  twitter       assets/teaser/twitter.mp4       ~35s, 1920x1080, silent. A mostly
                                                visual cut for a tweet: 3D | 2D
                                                diptychs, then a long-video finale
                                                and a short end card.

Newer renders sit on white; older ones sit on black, sometimes letterboxed
inside a white frame. The twitter cut detects that per clip and extends the
matching background, so the plate doesn't show up as a stripe.

The hero and the long teasers composite onto black and draw captions with
ffmpeg's drawtext filter (needs a build with libfreetype). The twitter cut
draws type with Pillow instead, so it builds on the stock Homebrew ffmpeg.

Usage:
  scripts/make_teaser.py twitter              # the tweet
  scripts/make_teaser.py twitter --fast       # draft quality, much quicker
  scripts/make_teaser.py                      # hero + both long teasers
"""

from __future__ import annotations

import argparse
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "assets", "teaser")

FPS = 30
XFADE = 0.30            # dissolve between montage segments
HERO_XFADE = 0.50       # slower dissolves for the ambient loop

# Dense point clouds are close to noise, so x264 spends a lot of bits on them.
# The hero autoplays above the fold, so it renders at 1440x540 (the site's
# widest container is 1160px, so that is still oversampled) at a leaner CRF.
# The teasers are watched once from a slide or a tweet and stay at full 1080p.
HERO_SIZE = (1440, 540)
TEASER_SIZE = (1920, 1080)
HERO_CRF = 25
TEASER_CRF = 23

FONT_BOLD = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FONT_REG = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"

WHITE = "0xF2F5F8"
MUTED = "0x9AA6B4"
ACCENT = "0x8AB8E8"     # --accent (#1c4e80) lifted for legibility on black

# 1920x1080 canvas with a 1920x720 content band => 180px letterbox bands.
BAND_TOP = 76           # baseline-ish y for a caption in the upper band
BAND_MAIN = 928         # headline y in the lower band
BAND_SUB = 1012         # subtitle y in the lower band


# --------------------------------------------------------------------------- #
# edit list primitives
# --------------------------------------------------------------------------- #

@dataclass
class Cue:
    """A timed text overlay. Times are relative to the start of its segment."""
    text: str
    t0: float
    t1: float
    y: int = BAND_MAIN
    size: int = 54
    color: str = WHITE
    bold: bool = True
    fade: float = 0.45


def head_cue(text: str, t0: float, t1: float) -> Cue:
    """Beat headline, set in the lower letterbox band."""
    return Cue(text, t0, t1, y=BAND_MAIN, size=54, color=WHITE, bold=True)


def sub_cue(text: str, t0: float, t1: float) -> Cue:
    """Supporting line under a beat headline."""
    return Cue(text, t0, t1, y=BAND_SUB, size=28, color=MUTED, bold=False)


@dataclass
class Seg:
    src: str
    ss: float = 0.0
    dur: float = 3.4
    speed: float = 1.0          # >1 plays faster
    blur: int = 0               # temporal frames to average (motion blur on speedups)
    cues: list[Cue] = field(default_factory=list)


@dataclass
class Wall:
    """A full-bleed grid of single-view clips playing at once.

    There is no letterbox band to put captions in, so a gradient scrim is laid
    over the bottom of the grid. Carrying the title needs a taller, stronger and
    flatter one than a single line of caption does.
    """
    srcs: list[str]
    cols: int
    rows: int
    dur: float
    ss: float = 0.0
    retime: bool = True
    scrim_h: int = 260
    scrim_alpha: float = 0.72
    scrim_gamma: float = 2.0
    cues: list[Cue] = field(default_factory=list)


@dataclass
class Card:
    """A black title card."""
    dur: float
    cues: list[Cue] = field(default_factory=list)


@dataclass
class Pair:
    """3D tracks on the left, 2D tracks on the right, filling a 16:9 frame.

    The left plate is contained and padded with the clip's own background
    (white or black). The right plate is contained too, so the 2D view is
    not cropped; the spare band uses the same background as the 3D plate.
    """
    left: str
    right: str
    ss: float = 0.0
    dur: float = 3.0
    speed: float = 1.0          # <1 slows the shot down
    title: bool = False


@dataclass
class Collage:
    """A grid of tracked views playing at once.

    These are the 2D track renders, which already fill the frame. The 3D
    plates are left out: newer ones are white and older ones are black, and a
    grid of both would show as stripes. Each tile is contained, not cropped.
    `speeds` matches the site: 0.5× for the in-the-wild clips that were
    encoded fast (pandas, hands), 1× for the other som clips, 2× DAVIS,
    4× MeViS.
    """
    srcs: list[str]
    starts: list[float]
    cols: int
    rows: int
    dur: float
    speeds: list[float] = field(default_factory=list)
    title: bool = False


@dataclass
class Full:
    """One clip filling the frame. Cover-crop by default; contain keeps every pixel."""
    src: str
    ss: float = 0.0
    dur: float = 4.0
    speed: float = 1.0
    caption: bool = False
    contain: bool = False


@dataclass
class EndCard:
    """White closing card: name, line, URL."""
    dur: float = 3.8


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #

def ffmpeg_has_filter(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return f" {name} " in out


def run(cmd: list[str], verbose: bool) -> None:
    if verbose:
        print("  $", " ".join(shlex.quote(c) for c in cmd))
    subprocess.run(cmd, check=True)


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def probe_fps(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=r_frame_rate", "-of", "default=noprint_wrappers=1:nokey=1",
         path], capture_output=True, text=True, check=True).stdout.strip()
    num, _, den = out.partition("/")
    return float(num) / float(den or 1)


def qual_speed(path: str) -> float:
    """Playback rate for a temporally-subsampled qualitative clip.

    The good_cases / wandb_ours renders are subsampled from real footage, so at
    1x they crawl and read as choppy. site.js speeds them up on playback for
    exactly this reason -- MeViS at 4x, DAVIS/PStudio at 2x -- which restores
    something near real-time motion and lifts the effective frame rate with it.
    Deriving the factor from the measured rate rather than from the filename
    also catches the 4 fps clips that live outside good_cases (the cow).
    """
    fps = probe_fps(path)
    return 4.0 if fps < 8.0 else 2.0


def drawtext(cue: Cue, tmp: str) -> str:
    """A drawtext filter with a symmetric alpha ramp in and out.

    The caption goes through a sidecar file rather than `text=`, so that ':',
    '%' and quotes in a caption survive ffmpeg's option parsing intact. (A bare
    '%' in `text=` is dropped with a "Stray %" warning and takes the whole
    caption with it.)
    """
    path = os.path.join(tmp, f"cue_{abs(hash((cue.text, cue.t0))):x}.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(cue.text)

    f = max(cue.fade, 1e-3)
    a = (f"if(lt(t,{cue.t0}),0,"
         f"if(lt(t,{cue.t0 + f}),(t-{cue.t0})/{f},"
         f"if(lt(t,{cue.t1 - f}),1,"
         f"if(lt(t,{cue.t1}),({cue.t1}-t)/{f},0))))")
    parts = [
        f"fontfile={FONT_BOLD if cue.bold else FONT_REG}",
        f"textfile={path}",
        "expansion=none",
        f"fontsize={cue.size}",
        f"fontcolor={cue.color}",
        f"alpha='{a}'",
        "x=(w-text_w)/2",
        f"y={cue.y}",
        f"enable='between(t,{cue.t0},{cue.t1})'",
        # A soft shadow keeps captions legible if they overlap bright content.
        "shadowcolor=0x000000AA", "shadowx=0", "shadowy=2",
    ]
    return "drawtext=" + ":".join(parts)


def scrim(y0: int, h: int, w: int, max_alpha: float = 0.72,
          gamma: float = 2.0, steps: int = 12) -> list[str]:
    """Stacked translucent boxes approximating a bottom-up dark gradient.

    Used where a caption sits over full-bleed footage instead of a letterbox
    band. It is drawn for the whole segment, so the surrounding dissolves fade
    it in and out along with the picture. A lower `gamma` darkens the upper part
    of the ramp, which is what a two-line title needs.
    """
    band = h / steps
    out = []
    for i in range(steps):
        alpha = max_alpha * ((i + 1) / steps) ** gamma
        out.append(f"drawbox=x=0:y={int(y0 + i * band)}:w={w}:"
                   f"h={int(band) + 1}:color=black@{alpha:.3f}:t=fill")
    return out


def fit_chain(w: int, h: int) -> str:
    return (f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def enc_args(crf: int, preset: str) -> list[str]:
    return ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-g", str(FPS * 2), "-movflags", "+faststart"]


# --------------------------------------------------------------------------- #
# segment renderers
# --------------------------------------------------------------------------- #

def render_seg(seg: Seg, w: int, h: int, out: str, with_text: bool, tmp: str,
               crf: int, preset: str, verbose: bool) -> float:
    src = os.path.join(ROOT, seg.src)
    avail = (probe_duration(src) - seg.ss) / seg.speed
    dur = min(seg.dur, avail - 0.05)
    if dur < seg.dur - 0.02:
        print(f"    note: {seg.src} clamped {seg.dur:.2f}s -> {dur:.2f}s")

    chain = []
    if seg.speed != 1.0:
        chain.append(f"setpts=PTS/{seg.speed}")
        if seg.blur > 1:
            chain.append(f"tmix=frames={seg.blur}")
    chain.append(f"fps={FPS}")
    chain.append(fit_chain(w, h))
    chain.append("format=yuv420p")
    if with_text:
        chain += [drawtext(c, tmp) for c in seg.cues]

    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{seg.ss}", "-i", src,
         "-an", "-vf", ",".join(chain), "-t", f"{dur:.3f}",
         *enc_args(crf, preset), out], verbose)
    return dur


def render_wall(wall: Wall, w: int, h: int, out: str, with_text: bool, tmp: str,
                crf: int, preset: str, verbose: bool) -> float:
    n = wall.cols * wall.rows
    srcs = wall.srcs[:n]
    assert len(srcs) == n, f"wall needs {n} clips, got {len(srcs)}"
    cw, ch = w // wall.cols, h // wall.rows

    inputs, chain, labels = [], [], []
    for i, s in enumerate(srcs):
        path = os.path.join(ROOT, s)
        speed = qual_speed(path) if wall.retime else 1.0
        # Sped up, every tile but the two PStudio ones is shorter than the beat,
        # so loop it. Tiles are staggered and small, so the wrap reads as a
        # gallery refreshing rather than as the trail reset of a single shot.
        inputs += ["-stream_loop", "-1"]
        if wall.ss:
            inputs += ["-ss", f"{wall.ss}"]
        inputs += ["-i", path]
        chain.append(f"[{i}:v]setpts=PTS/{speed},fps={FPS},"
                     f"trim=end={wall.dur:.3f},setpts=PTS-STARTPTS,"
                     f"{fit_chain(cw, ch)},format=yuv420p[c{i}]")
        labels.append(f"[c{i}]")
    layout = "|".join(
        f"{(i % wall.cols) * cw}_{(i // wall.cols) * ch}" for i in range(n))
    tail = f"{''.join(labels)}xstack=inputs={n}:layout={layout}:fill=black[grid]"
    last = "[grid]"
    if with_text and wall.cues:
        # No letterbox band here, so lay a gradient scrim under the caption.
        post = ",".join(scrim(h - wall.scrim_h, wall.scrim_h, w,
                              wall.scrim_alpha, wall.scrim_gamma) +
                        [drawtext(c, tmp) for c in wall.cues])
        tail += f";[grid]{post}[vout]"
        last = "[vout]"
    chain.append(tail)

    run(["ffmpeg", "-v", "error", "-y", *inputs, "-an",
         "-filter_complex", ";".join(chain), "-map", last,
         "-t", f"{wall.dur:.3f}", *enc_args(crf, preset), out], verbose)
    return wall.dur


def render_card(card: Card, w: int, h: int, out: str, tmp: str,
                crf: int, preset: str, verbose: bool) -> float:
    chain = ["format=yuv420p"] + [drawtext(c, tmp) for c in card.cues]
    run(["ffmpeg", "-v", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=black:s={w}x{h}:r={FPS}:d={card.dur}",
         "-an", "-vf", ",".join(chain), "-t", f"{card.dur:.3f}",
         *enc_args(crf, preset), out], verbose)
    return card.dur


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #

def assemble(parts: list[str], durs: list[float], xfade: float, out: str,
             fade_in: float, fade_out: float, crf: int, preset: str,
             verbose: bool) -> float:
    """Chain the parts together with cross dissolves; fade the head and tail."""
    inputs = []
    for p in parts:
        inputs += ["-i", p]

    chain, acc, prev = [], durs[0], "[0:v]"
    for i in range(1, len(parts)):
        label = f"[x{i}]"
        chain.append(f"{prev}[{i}:v]xfade=transition=fade:"
                     f"duration={xfade}:offset={acc - xfade:.3f}{label}")
        acc = acc + durs[i] - xfade
        prev = label

    total = acc
    post = [f"fade=t=in:st=0:d={fade_in}"] if fade_in else []
    if fade_out:
        post.append(f"fade=t=out:st={total - fade_out:.3f}:d={fade_out}")
    post.append("format=yuv420p")
    chain.append(f"{prev}{','.join(post)}[vout]")

    run(["ffmpeg", "-v", "error", "-y", *inputs,
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
         "-filter_complex", ";".join(chain),
         "-map", "[vout]", "-map", f"{len(parts)}:a",
         *enc_args(crf, preset), "-c:a", "aac", "-b:a", "96k", "-shortest",
         out], verbose)
    return total


def fold_loop(src: str, fold: float, out: str, crf: int, preset: str,
              verbose: bool) -> None:
    """Make `src` loop seamlessly, returning a clip `fold` seconds shorter.

    The output is src[fold : D-fold] followed by a dissolve from the tail
    src[D-fold : D] into the head src[0 : fold]. The dissolve therefore spans
    the wrap point, and its final frame is 100% head -- which is the same frame
    the output started on, so playback loops with no visible cut.

    Two details make the seam exact, and both cost a single frame:

    * `blend` with an explicit ramp rather than `xfade`. xfade's mix reaches
      100% of the incoming stream only *past* its last output frame, so its
      final frame keeps a few percent of the outgoing stream and the wrap
      ghosts. Here the ramp is normalised over `fold - 1/FPS` so the last frame
      is exactly 1.0.
    * The head is trimmed from 1/FPS, not 0. `trim=end=fold` stops just short of
      `fold`, so a head starting at 0 ends one frame before the frame the body
      starts on. Shifting it by one frame lines the two up.
    """
    total = probe_duration(src)
    step = 1.0 / FPS
    ramp = f"min(1,max(0,T/{fold - step:.6f}))"
    chain = (f"[0:v]trim=start={fold:.4f}:end={total - fold:.4f},"
             f"setpts=PTS-STARTPTS[main];"
             f"[1:v]trim=start={total - fold:.4f},setpts=PTS-STARTPTS[tail];"
             f"[2:v]trim=start={step:.4f}:end={fold + step:.4f},"
             f"setpts=PTS-STARTPTS[head];"
             f"[tail][head]blend=all_expr='A*(1-{ramp})+B*{ramp}'[mix];"
             f"[main][mix]concat=n=2:v=1:a=0[vout]")
    run(["ffmpeg", "-v", "error", "-y", "-i", src, "-i", src, "-i", src,
         "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
         "-filter_complex", chain, "-map", "[vout]", "-map", "3:a",
         *enc_args(crf, preset), "-c:a", "aac", "-b:a", "96k", "-shortest",
         out], verbose)


# --------------------------------------------------------------------------- #
# twitter cut — plates, type, diptychs
# --------------------------------------------------------------------------- #

# Site palette, so the end card matches the paper page.
INK = (20, 24, 29)          # --text
SOFT = (67, 80, 95)         # --text-soft
MUTED_RGB = (96, 108, 122)
ACCENT_RGB = (28, 78, 128)  # --accent

XFADE_TW = 0.32


def _libs():
    try:
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError:
        print("error: the twitter cut needs Pillow and numpy\n"
              "       pip install pillow numpy", file=sys.stderr)
        raise SystemExit(1)
    return np, Image, ImageDraw, ImageFont, ImageFilter


def _font_file(candidates):
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def load_fonts():
    """Serif italic for the name (the site sets it in italic), sans for the rest."""
    _, _, _, ImageFont, _ = _libs()
    serif = _font_file([
        "/System/Library/Fonts/Supplemental/Iowan Old Style.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    ])
    sans = _font_file([
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ])
    sans_bold = _font_file([
        "/System/Library/Fonts/HelveticaNeue.ttc",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ])
    if not serif or not sans:
        print("error: could not find a serif and a sans font for the twitter cut",
              file=sys.stderr)
        raise SystemExit(1)

    def face(path, size, index):
        if path.endswith(".ttc"):
            return ImageFont.truetype(path, size, index=index)
        return ImageFont.truetype(path, size)

    # Iowan Old Style face 3 is Bold Italic. Helvetica Neue: 1 bold, 10 medium.
    # Single-face files (Liberation) ignore the index.
    class Faces:
        pass

    faces = Faces()
    faces.serif_path = serif
    faces.serif_index = 3 if serif.endswith(".ttc") else 0
    faces.sans_path = sans
    faces.bold_path = sans_bold or sans
    faces.idx_bold = 1 if faces.bold_path.endswith(".ttc") else 0
    faces.idx_med = 10 if sans.endswith(".ttc") else 0
    faces.idx_reg = 0
    faces.face = face
    return faces


def read_frame(path, t):
    _, Image, _, _, _ = _libs()
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", path,
         "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
        capture_output=True, check=True).stdout
    return Image.open(io.BytesIO(raw)).convert("RGB")


def _dilate(mask, radius):
    """Binary dilation that does not wrap around the frame edges."""
    np, *_ = _libs()
    if radius <= 0:
        return mask
    h, w = mask.shape
    padded = np.pad(mask, radius, constant_values=False)
    out = np.zeros_like(mask)
    r2 = radius * radius
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > r2:
                continue
            out |= padded[dy + radius:dy + radius + h, dx + radius:dx + radius + w]
    return out


def plate_crop(path, ss, dur):
    """How to seat a 3D render on the left half of the frame.

    Newer clips are a point cloud on a white field: pad with white and the
    field just continues. Older clips are a black render dropped onto a white
    matte (white bars around a black window). Those get the matte cropped off
    and the empty black bars trimmed, then pad with black so the window
    continues instead of leaving a white stripe.

    The crop is the union across the shot, so an orbiting cloud is not clipped
    by a box measured on a single frame.
    """
    np, *_ = _libs()
    src_dur = probe_duration(path)
    times = []
    for u in (0.08, 0.30, 0.55, 0.78, 0.94):
        t = ss + dur * u
        if 0.05 < t < src_dur - 0.05:
            times.append(t)
    if not times:
        times = [min(ss + 0.1, max(0.0, src_dur - 0.1))]

    frames = [np.asarray(read_frame(path, t)) for t in times]
    h, w = frames[0].shape[:2]

    def white_of(a):
        return (a[:, :, 0] >= 232) & (a[:, :, 1] >= 232) & (a[:, :, 2] >= 232)

    boxes = []
    for a in frames:
        fg = ~_dilate(white_of(a), 3)
        ys, xs = np.where(fg)
        if len(xs) == 0:
            continue
        boxes.append((int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1))
    if not boxes:
        return "white", None

    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    # The corner of a bounding box is often still background: the extreme
    # pixels sit mid-edge, not in the corner. A black render is a rectangle,
    # so those corners are black. A white-field cloud is irregular, so the
    # corners fall on white. That is the test.
    inset = 8
    mid = frames[len(frames) // 2]
    sx0, sy0 = min(x0 + inset, w - 2), min(y0 + inset, h - 2)
    sx1, sy1 = max(x1 - inset, sx0 + 1), max(y1 - inset, sy0 + 1)
    sub = mid[sy0:sy1, sx0:sx1]
    sh, sw = sub.shape[:2]
    corners = [sub[2, 2], sub[2, -3], sub[-3, 2], sub[-3, -3]]
    content_luma = float(np.median([p.mean() for p in corners]))
    area = (x1 - x0) * (y1 - y0) / float(w * h)
    if not (content_luma < 42 and area < 0.96):
        samp = [frames[0][2, 2], frames[0][2, -3], frames[0][-3, 2], frames[0][-3, -3]]
        edge_luma = float(np.median([p.mean() for p in samp]))
        return ("white" if edge_luma > 180 else "black"), None

    # Drop the anti-aliased white fringe, then trim rows and columns that are
    # empty black (or a leftover white hairline) across every sampled frame.
    x0, y0, x1, y1 = x0 + 4, y0 + 4, x1 - 4, y1 - 4
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)

    row_keep = np.zeros(y1 - y0, dtype=bool)
    col_keep = np.zeros(x1 - x0, dtype=bool)
    for a in frames:
        window = a[y0:y1, x0:x1]
        luma = window.mean(axis=2)
        empty = (luma < 16) | (luma > 242)
        row_keep |= empty.mean(axis=1) < 0.985
        col_keep |= empty.mean(axis=0) < 0.985
    if row_keep.any():
        ys = np.where(row_keep)[0]
        y0, y1 = y0 + int(ys[0]), y0 + int(ys[-1]) + 1
    if col_keep.any():
        xs = np.where(col_keep)[0]
        x0, x1 = x0 + int(xs[0]), x0 + int(xs[-1]) + 1

    # A little black margin so the cloud doesn't kiss the crop, then walk
    # back in over any white fringe that margin (or antialiasing) reintroduced.
    # A fringe only counts if it is bright in every sample, so a white shirt
    # in one frame is left alone.
    m = 8
    x0, y0 = max(0, x0 - m), max(0, y0 - m)
    x1, y1 = min(w, x1 + m), min(h, y1 + m)

    def col_luma(a, x):
        return float(np.median(a[y0:y1, x].mean(axis=1)))

    def row_luma(a, y):
        return float(np.median(a[y, x0:x1].mean(axis=1)))

    for _ in range(48):
        if x1 - x0 < 48 or y1 - y0 < 48:
            break
        moved = False
        if all(col_luma(a, x0) > 170 for a in frames):
            x0 += 1
            moved = True
        if x1 - x0 > 48 and all(col_luma(a, x1 - 1) > 170 for a in frames):
            x1 -= 1
            moved = True
        if y1 - y0 > 48 and all(row_luma(a, y0) > 170 for a in frames):
            y0 += 1
            moved = True
        if y1 - y0 > 48 and all(row_luma(a, y1 - 1) > 170 for a in frames):
            y1 -= 1
            moved = True
        if not moved:
            break
    if x1 - x0 < 32 or y1 - y0 < 32:
        return "black", None
    return "black", (x0, y0, x1 - x0, y1 - y0)


def _text_width(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box


def draw_centered(base, draw, text, font, y, fill, shadow=False):
    _, Image, _, _, ImageFilter = _libs()
    tw, box = _text_width(draw, text, font)
    x = (base.width - tw) / 2 - box[0]
    y = y - box[1]
    if shadow:
        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        ImageDraw = _libs()[2]
        sd = ImageDraw.Draw(layer)
        sd.text((x, y + 2), text, font=font, fill=(0, 0, 0, 150))
        layer = layer.filter(ImageFilter.GaussianBlur(radius=5))
        base.alpha_composite(layer)
        draw = ImageDraw.Draw(base)
    draw.text((x, y), text, font=font, fill=fill)
    return draw


def title_overlay(path, w, h, faces):
    """Opening title, centered. A soft middle scrim keeps the type readable."""
    np, Image, ImageDraw, _, _ = _libs()
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ys = np.arange(h)
    alpha = (185 * np.exp(-0.5 * ((ys - h / 2) / 230) ** 2)).astype("uint8")
    arr = np.zeros((h, w, 4), dtype="uint8")
    arr[:, :, 3] = alpha[:, None]
    im.alpha_composite(Image.fromarray(arr))
    draw = ImageDraw.Draw(im)
    serif = faces.face(faces.serif_path, 108, faces.serif_index)
    for size in range(108, 78, -2):
        serif = faces.face(faces.serif_path, size, faces.serif_index)
        tw, _ = _text_width(draw, "TrackEverything", serif)
        if tw <= w - 160:
            break
    line_a = "long-horizon dense 3D tracking"
    line_b = "with de-duplicating 3D representations"
    sub = faces.face(faces.sans_path, 36, faces.idx_med)
    for size in range(40, 26, -1):
        sub = faces.face(faces.sans_path, size, faces.idx_med)
        wa, _ = _text_width(draw, line_a, sub)
        wb, _ = _text_width(draw, line_b, sub)
        if max(wa, wb) <= w - 140:
            break
    mid = h // 2
    draw = draw_centered(im, draw, "TrackEverything", serif, mid - 90,
                         (255, 255, 255, 255), shadow=True)
    draw = draw_centered(im, draw, line_a, sub, mid + 30,
                         (255, 255, 255, 240), shadow=True)
    draw_centered(im, draw, line_b, sub, mid + 86,
                  (255, 255, 255, 235), shadow=True)
    im.save(path)


def caption_overlay(path, w, h, faces):
    """One line on the long-video shot. Bottom scrim, then it leaves."""
    np, Image, ImageDraw, _, _ = _libs()
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    grad_h = 280
    alpha = (150 * np.linspace(0, 1, grad_h) ** 1.3).astype("uint8")
    arr = np.zeros((h, w, 4), dtype="uint8")
    arr[h - grad_h:, :, 3] = alpha[:, None]
    im.alpha_composite(Image.fromarray(arr))
    draw = ImageDraw.Draw(im)
    bold = faces.face(faces.bold_path, 52, faces.idx_bold)
    med = faces.face(faces.sans_path, 36, faces.idx_med)
    draw = draw_centered(im, draw, "Tracking Everything in 1000–2000 Frames", bold,
                         h - 188, (255, 255, 255, 255), shadow=True)
    draw_centered(im, draw, "(under 40G memory)", med, h - 112,
                  (255, 255, 255, 230), shadow=True)
    im.save(path)


def endcard_image(path, w, h, faces):
    _, Image, ImageDraw, _, _ = _libs()
    im = Image.new("RGB", (w, h), (255, 255, 255))
    rgba = im.convert("RGBA")
    draw = ImageDraw.Draw(rgba)
    serif = faces.face(faces.serif_path, 118, faces.serif_index)
    med = faces.face(faces.sans_path, 40, faces.idx_med)
    url = faces.face(faces.bold_path, 48, faces.idx_bold)
    reg = faces.face(faces.sans_path, 28, faces.idx_reg)
    aff = faces.face(faces.sans_path, 30, faces.idx_med)
    draw = draw_centered(rgba, draw, "TrackEverything", serif, 318, INK + (255,))
    draw = draw_centered(rgba, draw, "Long-horizon dense 3D tracking", med, 468,
                         SOFT + (255,))
    draw.line([(w // 2 - 100, 548), (w // 2 + 100, 548)], fill=ACCENT_RGB + (255,), width=3)
    draw = draw_centered(rgba, draw, "trackeverything.github.io", url, 600,
                         ACCENT_RGB + (255,))
    draw = draw_centered(
        rgba, draw,
        "Ayush Jain   ·   Sreeharsha Paruchuri   ·   Ishita Gupta   ·   Fan Zhang",
        reg, 748, MUTED_RGB + (255,))
    draw = draw_centered(
        rgba, draw,
        "Tanner Schmidt   ·   Jakob Engel   ·   Katerina Fragkiadaki   ·   Adam W. Harley",
        reg, 792, MUTED_RGB + (255,))
    draw_centered(rgba, draw, "Carnegie Mellon University     ·     Meta", aff, 860,
                  SOFT + (255,))
    rgba.convert("RGB").save(path)


def _overlay_chain(base, specs):
    """Fade each full-frame RGBA input in and out over `base`.

    specs: list of (input_index, t0, t1, fade). Times are on the segment clock.
    """
    parts = []
    prev = base
    for n, (idx, t0, t1, fade) in enumerate(specs):
        label = f"[ov{n}]"
        out = f"[o{n}]"
        fades = []
        if t0 > 0.02:
            fades.append(f"fade=t=in:st={t0:.3f}:d={fade:.3f}:alpha=1")
        fades.append(
            f"fade=t=out:st={max(t0 + fade, t1 - fade):.3f}:d={fade:.3f}:alpha=1")
        parts.append(f"[{idx}:v]format=rgba,{','.join(fades)}{label}")
        parts.append(f"{prev}{label}overlay=0:0:format=auto{out}")
        prev = out
    return parts, prev


def render_pair(pair, w, h, out, with_text, tmp, faces, crf, preset, verbose):
    left = os.path.join(ROOT, pair.left)
    right = os.path.join(ROOT, pair.right)
    avail = (min(probe_duration(left), probe_duration(right)) - pair.ss) / pair.speed
    dur = min(pair.dur, avail - 0.04)
    if dur < 0.4:
        raise RuntimeError(f"{pair.left} is too short from {pair.ss}")
    if dur < pair.dur - 0.05:
        print(f"    note: {os.path.basename(pair.left)} clamped "
              f"{pair.dur:.2f}s -> {dur:.2f}s")

    pad, crop = plate_crop(left, pair.ss, dur)
    pw, ph = w // 2, h
    crop_f = ""
    if crop:
        x, y, cw, ch = crop
        crop_f = f"crop={cw}:{ch}:{x}:{y},"
        print(f"        plate {pad}  crop {cw}x{ch}+{x}+{y}")
    else:
        print(f"        plate {pad}")

    rate = f"setpts=PTS/{pair.speed}," if pair.speed != 1.0 else ""
    chain = [
        f"[0:v]{rate}fps={FPS},setpts=PTS-STARTPTS,{crop_f}"
        f"scale={pw}:{ph}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color={pad},setsar=1,format=yuv420p[l]",
        f"[1:v]{rate}fps={FPS},setpts=PTS-STARTPTS,"
        f"scale={pw}:{ph}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={pw}:{ph}:(ow-iw)/2:(oh-ih)/2:color={pad},setsar=1,format=yuv420p[r]",
        "[l][r]hstack=inputs=2[base]",
    ]
    src_t = dur * pair.speed
    inputs = ["-ss", f"{pair.ss:.3f}", "-t", f"{src_t:.3f}", "-i", left,
              "-ss", f"{pair.ss:.3f}", "-t", f"{src_t:.3f}", "-i", right]
    last = "[base]"
    if with_text and pair.title:
        png = os.path.join(tmp, "title.png")
        title_overlay(png, w, h, faces)
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", png]
        extra, last = _overlay_chain("[base]", [(2, 0.0, min(3.05, dur - 0.40), 0.28)])
        chain += extra
    chain.append(f"{last}format=yuv420p[vout]")

    run(["ffmpeg", "-v", "error", "-y", *inputs, "-an",
         "-filter_complex", ";".join(chain), "-map", "[vout]",
         "-t", f"{dur:.3f}", *enc_args(crf, preset), out], verbose)
    return dur


def render_collage(collage, w, h, out, crf, preset, verbose,
                   with_text=False, tmp=None, faces=None):
    """Tile tracked views edge to edge. Each tile is contained, so nothing is cropped."""
    n = collage.cols * collage.rows
    srcs = collage.srcs[:n]
    if len(srcs) != n:
        raise RuntimeError(f"collage needs {n} clips, got {len(srcs)}")
    cw, ch = w // collage.cols, h // collage.rows
    inputs, chain, labels = [], [], []
    for i, src in enumerate(srcs):
        path = os.path.join(ROOT, src)
        ss = collage.starts[i] if i < len(collage.starts) else 0.0
        speed = collage.speeds[i] if i < len(collage.speeds) else 1.0
        avail = (probe_duration(path) - ss) / speed
        if avail < 0.4:
            ss, speed = 0.0, collage.speeds[i] if i < len(collage.speeds) else 1.0
        rate = f"setpts=PTS/{speed}," if speed != 1.0 else ""
        inputs += ["-stream_loop", "-1", "-ss", f"{ss:.3f}", "-i", path]
        chain.append(
            f"[{i}:v]{rate}fps={FPS},setpts=PTS-STARTPTS,"
            f"scale={cw}:{ch}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p,"
            f"trim=end={collage.dur:.3f},setpts=PTS-STARTPTS[c{i}]")
        labels.append(f"[c{i}]")
        print(f"        {os.path.basename(src):42s} {speed:.1f}x")
    layout = "|".join(
        f"{(i % collage.cols) * cw}_{(i // collage.cols) * ch}" for i in range(n))
    chain.append(f"{''.join(labels)}xstack=inputs={n}:layout={layout}:fill=black[grid]")
    last = "[grid]"
    if with_text and collage.title and faces is not None:
        png = os.path.join(tmp, "title.png")
        title_overlay(png, w, h, faces)
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{collage.dur:.3f}", "-i", png]
        extra, last = _overlay_chain(
            "[grid]", [(n, 0.15, min(4.4, collage.dur - 0.35), 0.35)])
        chain += extra
    chain.append(f"{last}format=yuv420p[vout]")
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-an",
         "-filter_complex", ";".join(chain), "-map", "[vout]",
         "-t", f"{collage.dur:.3f}", *enc_args(crf, preset), out], verbose)
    return collage.dur


def render_static_dynamic(out, crf, preset, verbose):
    """3×2 collage of the static/dynamic clips, first page of that section.

    Playback follows the site: DAVIS at 2×, MeViS at 4×. The fountain dancer
    and the kittens (row 2, column 3) look hurried at that MeViS rate, so
    those two run at 2×. The grid matches the page order and the clips loop.
    """
    SD = "assets/static_dynamic"
    tiles = [
        (f"{SD}/davis_great_breakdance-flare.mp4", 2.0),
        (f"{SD}/mevis_good_410dae675d9a.mp4", 4.0),
        (f"{SD}/mevis_great_b8ce22e26dde.mp4", 4.0),
        (f"{SD}/davis_great_dance-jump.mp4", 2.0),
        (f"{SD}/mevis_great_7fd5537074bd.mp4", 4.0),
        (f"{SD}/mevis_good_d6c1a055ae91.mp4", 2.0),
    ]
    dur = 8.25
    cols, rows = 3, 2
    cw, ch = 640, 480
    inputs, chain, labels = [], [], []
    for i, (src, speed) in enumerate(tiles):
        path = os.path.join(ROOT, src)
        inputs += ["-stream_loop", "-1", "-i", path]
        chain.append(
            f"[{i}:v]setpts=PTS/{speed},fps={FPS},"
            f"scale={cw}:{ch}:flags=lanczos,setsar=1,format=yuv420p,"
            f"trim=end={dur:.3f},setpts=PTS-STARTPTS[c{i}]")
        labels.append(f"[c{i}]")
        print(f"        {os.path.basename(src):42s} {speed:.1f}x")
    layout = "|".join(
        f"{(i % cols) * cw}_{(i // cols) * ch}" for i in range(cols * rows))
    chain.append(f"{''.join(labels)}xstack=inputs={len(tiles)}:layout={layout}:fill=black[vout]")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-an",
         "-filter_complex", ";".join(chain), "-map", "[vout]",
         "-t", f"{dur:.3f}", *enc_args(crf, preset), out], verbose)
    return dur


def render_full(full, w, h, out, with_text, tmp, faces, crf, preset, verbose):
    src = os.path.join(ROOT, full.src)
    avail = (probe_duration(src) - full.ss) / full.speed
    dur = min(full.dur, avail - 0.05)
    rate = f"setpts=PTS/{full.speed}," if full.speed != 1.0 else ""
    if full.contain:
        fit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
               f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
    else:
        fit = (f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
               f"crop={w}:{h},setsar=1")
    chain = [
        f"[0:v]{rate}fps={FPS},setpts=PTS-STARTPTS,{fit}[base]",
    ]
    inputs = ["-ss", f"{full.ss:.3f}", "-t", f"{dur * full.speed:.3f}", "-i", src]
    last = "[base]"
    if with_text and full.caption:
        png = os.path.join(tmp, "caption.png")
        caption_overlay(png, w, h, faces)
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", png]
        # Land after the dissolve in, leave before the dissolve out.
        extra, last = _overlay_chain(
            "[base]", [(1, 0.55, dur - 0.45, 0.40)])
        chain += extra
    chain.append(f"{last}format=yuv420p[vout]")
    run(["ffmpeg", "-v", "error", "-y", *inputs, "-an",
         "-filter_complex", ";".join(chain), "-map", "[vout]",
         "-t", f"{dur:.3f}", *enc_args(crf, preset), out], verbose)
    return dur


def render_endcard(card, w, h, out, tmp, faces, crf, preset, verbose):
    png = os.path.join(tmp, "endcard.png")
    endcard_image(png, w, h, faces)
    run(["ffmpeg", "-v", "error", "-y",
         "-loop", "1", "-framerate", str(FPS), "-i", png,
         "-an", "-vf", "format=yuv420p", "-t", f"{card.dur:.3f}",
         *enc_args(crf, preset), out], verbose)
    return card.dur


# --------------------------------------------------------------------------- #
# the edits
# --------------------------------------------------------------------------- #

R = "assets/rerun_ours"
L = "assets/long_video"
G = "assets/good_cases"
W = "assets/wandb_ours"      # the "Ours" column of the baseline comparison

WALL_CLIPS = [
    f"{W}/ours_d32e4f23eb40.mp4",   f"{G}/mevis_9f542dded87c.mp4",
    f"{G}/davis_car-turn.mp4",      f"{G}/mevis_f610df51c78a.mp4",
    f"{G}/davis_stroller.mp4",      f"{G}/mevis_e262e26da29e.mp4",
    f"{G}/davis_hockey.mp4",        f"{G}/pstudio_tracking_1.mp4",
    f"{G}/davis_train.mp4",         f"{G}/mevis_a9402f575b5c.mp4",
    f"{G}/davis_car-roundabout.mp4", f"{G}/pstudio_tracking_2.mp4",
]


def teaser_edit() -> list:
    """~58s, 1920x1080. Cues are only drawn in the -text build."""
    return [
        # --- open on the wall, which carries the title ----------------------
        Wall(WALL_CLIPS, cols=4, rows=3, dur=6.2,
             scrim_h=380, scrim_alpha=0.88, scrim_gamma=1.3, cues=[
                 Cue("TrackEverything", 0.8, 5.7, y=892, size=76),
                 Cue("Dense 3D Point Tracking in Long Videos", 1.2, 5.7,
                     y=1000, size=30, color=MUTED, bold=False),
             ]),

        # --- hook -----------------------------------------------------------
        Seg(f"{R}/tennis.mp4", 0.0, 4.8, cues=[
            Cue("left: persistent 3D scene tracks   ·   right: input view",
                0.6, 4.2, y=BAND_TOP, size=27, color=MUTED, bold=False),
        ]),
        Seg(f"{R}/baseball.mp4", 1.2, 4.0, cues=[
            head_cue("every point, every frame", 0.5, 3.6),
        ]),
        Seg(f"{R}/pandas_1.mp4", 2.4, 4.2),

        # --- range of scenes ------------------------------------------------
        Seg(f"{R}/tigers.mp4", 1.2, 3.6),
        Seg(f"{R}/ours-breakdance.mp4", 1.8, 3.6),
        Seg(f"{R}/ours_horsejump-high_30fps.mp4", 0.0, 2.2),
        Seg(f"{R}/ours_tennis.mp4", 0.2, 3.4, cues=[
            head_cue("tracking happens in a persistent 3D scene", 0.4, 3.0),
            sub_cue("repeat views of a surface fuse; occluded points persist",
                    0.8, 3.0),
        ]),
        Seg(f"{R}/basketball.mp4", 1.0, 3.4),
        Seg(f"{R}/ours_swing.mp4", 0.2, 3.2),
        Seg(f"{R}/cars.mp4", 0.2, 3.2),

        # --- long video -----------------------------------------------------
        # Long clips play at 1x on the site. The 37s uptown dance is the one
        # worth featuring; 1.5x keeps the motion readable in a short beat.
        Seg(f"{L}/uptown_6.mp4", 2.0, 7.0, speed=1.5, cues=[
            head_cue("1000+ frames, in a single pass", 0.7, 6.4),
            sub_cue("prior dense 3D trackers run out of memory past ~96 frames",
                    1.1, 6.4),
        ]),
        Seg(f"{L}/pod_3.mp4", 2.0, 6.0, cues=[
            head_cue("under 40 GB of GPU memory", 0.6, 5.4),
            sub_cue("cost scales with scene content, not video length", 1.0, 5.4),
        ]),

        # --- close ----------------------------------------------------------
        Seg(f"{R}/tennis.mp4", 1.6, 3.4, cues=[
            Cue("+20% APD over open-source methods that track all points",
                0.5, 3.0, y=BAND_MAIN, size=48, color=ACCENT),
        ]),
        Card(5.0, cues=[
            Cue("TrackEverything", 0.3, 5.0, y=392, size=86),
            Cue("Dense 3D Point Tracking in Long Videos", 0.5, 5.0,
                y=506, size=36, color=WHITE, bold=False),
            Cue("via 3D Scene Representations", 0.5, 5.0,
                y=552, size=36, color=WHITE, bold=False),
            Cue("Ayush Jain · Sreeharsha Paruchuri · Ishita Gupta · Fan Zhang",
                0.9, 5.0, y=640, size=25, color=MUTED, bold=False),
            Cue("Tanner Schmidt · Jakob Engel · Katerina Fragkiadaki · "
                "Adam W. Harley",
                0.9, 5.0, y=676, size=25, color=MUTED, bold=False),
            Cue("Carnegie Mellon University   ·   Meta", 1.1, 5.0,
                y=726, size=25, color=MUTED, bold=False),
            Cue("trackeverything.github.io", 1.4, 5.0,
                y=778, size=32, color=ACCENT, bold=False),
        ]),
    ]


def hero_edit() -> list:
    """~29s, 1920x720, no text, folded into a seamless loop."""
    return [
        Seg(f"{R}/tennis.mp4", 0.0, 3.8),
        Seg(f"{R}/baseball.mp4", 1.2, 3.6),
        Seg(f"{R}/pandas_1.mp4", 2.4, 3.6),
        Seg(f"{R}/tigers.mp4", 1.2, 3.4),
        Seg(f"{R}/ours-breakdance.mp4", 1.8, 3.6),
        Seg(f"{L}/uptown_6.mp4", 2.0, 4.2, speed=1.5),
        Seg(f"{R}/basketball.mp4", 1.0, 3.4),
        Seg(f"{R}/ours_tennis.mp4", 0.2, 3.2),
        Seg(f"{L}/pod_3.mp4", 2.0, 3.8),
        Seg(f"{R}/ours_horsejump-high_30fps.mp4", 0.0, 2.2),
    ]


S = "assets/som_try"


def twitter_edit():
    """Opens on a 2D collage with the title, then 3D | 2D scenes.

    The robot is a full frame, contained, so the arm is not sliced by the
    split. Qualitative clips play at the same rate as on the site.
    """
    Q = "assets/good_cases"
    # (path, start, speed). Speed matches the site. Pandas and hands were
    # encoded at double time, so they play at half speed.
    tiles = [
        (f"{S}/pandas_1_2d.mp4", 0.4, 0.5),
        (f"{S}/tigers_2d.mp4", 0.4, 1.0),
        (f"{S}/fish_2d.mp4", 0.3, 1.0),
        (f"{S}/cats_2d.mp4", 0.2, 1.0),
        (f"{Q}/davis_hockey.mp4", 0.4, 2.0),
        (f"{Q}/davis_train.mp4", 0.4, 2.0),
        (f"{S}/hands_2d.mp4", 0.6, 0.5),
        (f"{S}/swing_2d.mp4", 0.4, 1.0),
        (f"{Q}/davis_dog.mp4", 0.3, 2.0),
        (f"{Q}/davis_dance-twirl.mp4", 0.4, 2.0),
        (f"{Q}/mevis_9f542dded87c.mp4", 0.6, 4.0),
        (f"{Q}/mevis_a9402f575b5c.mp4", 0.6, 4.0),
    ]
    return [
        Collage([s for s, _, _ in tiles], [t for _, t, _ in tiles],
                cols=4, rows=3, dur=5.2,
                speeds=[v for _, _, v in tiles], title=True),
        Pair(f"{S}/pandas_1_3d.mp4", f"{S}/pandas_1_2d.mp4",
             ss=0.4, dur=6.8, speed=0.5),
        Pair(f"{S}/tigers_3d.mp4", f"{S}/tigers_2d.mp4", ss=0.12, dur=2.55),
        Pair(f"{S}/hands_3d.mp4", f"{S}/hands_2d.mp4", ss=0.6, dur=5.8, speed=0.5),
        Pair(f"{S}/breakdance_3d.mp4", f"{S}/breakdance_2d.mp4", ss=1.6, dur=3.20),
        Pair(f"{S}/tennis_3d.mp4", f"{S}/tennis_2d.mp4", ss=0.5, dur=2.80),
        # Play through to the end of the clip. A short dur was cutting it off.
        Pair(f"{S}/robot_1_3d.mp4", f"{S}/robot_1_2d.mp4", ss=1.0, dur=30.0),
        Full(f"{L}/uptown_6.mp4", ss=2.0, dur=6.5, speed=1.5, caption=True),
        EndCard(3.8),
    ]


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build(edit: list, w: int, h: int, out: str, with_text: bool, xfade: float,
          fade_in: float, fade_out: float, loop_fold: float,
          crf: int, preset: str, verbose: bool, faces=None) -> None:
    print(f"\n=> {os.path.relpath(out, ROOT)}  ({w}x{h}, "
          f"{'with text' if with_text else 'no text'})")
    tmp = tempfile.mkdtemp(prefix="te_teaser_")
    try:
        parts, durs = [], []
        for i, item in enumerate(edit):
            part = os.path.join(tmp, f"p{i:02d}.mp4")
            if isinstance(item, Seg):
                label = os.path.basename(item.src)
                d = render_seg(item, w, h, part, with_text, tmp,
                               crf, preset, verbose)
            elif isinstance(item, Wall):
                label = f"wall {item.cols}x{item.rows}"
                d = render_wall(item, w, h, part, with_text, tmp,
                                crf, preset, verbose)
            elif isinstance(item, Card):
                if not with_text:
                    continue
                label = "end card"
                d = render_card(item, w, h, part, tmp, crf, preset, verbose)
            elif isinstance(item, Pair):
                label = os.path.basename(item.left).replace("_3d.mp4", "")
                d = render_pair(item, w, h, part, with_text, tmp, faces,
                                crf, preset, verbose)
            elif isinstance(item, Collage):
                label = f"collage {item.cols}x{item.rows}"
                d = render_collage(item, w, h, part, crf, preset, verbose,
                                   with_text, tmp, faces)
            elif isinstance(item, Full):
                label = os.path.basename(item.src)
                d = render_full(item, w, h, part, with_text, tmp, faces,
                                crf, preset, verbose)
            elif isinstance(item, EndCard):
                if not with_text:
                    continue
                label = "end card"
                d = render_endcard(item, w, h, part, tmp, faces,
                                   crf, preset, verbose)
            else:
                raise TypeError(item)
            print(f"   [{i:02d}] {label:44s} {d:5.2f}s")
            parts.append(part)
            durs.append(d)

        os.makedirs(os.path.dirname(out), exist_ok=True)
        staged = os.path.join(tmp, "seq.mp4") if loop_fold else out
        total = assemble(parts, durs, xfade, staged, fade_in, fade_out,
                         crf, preset, verbose)
        if loop_fold:
            fold_loop(staged, loop_fold, out, crf, preset, verbose)
            total -= loop_fold
        size = os.path.getsize(out) / 1e6
        print(f"   done: {total:.1f}s, {size:.1f} MB")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


TARGETS = ("hero", "teaser-plain", "teaser-text", "twitter", "static-dynamic")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("targets", nargs="*", metavar="TARGET",
                    help=f"which cuts to build ({', '.join(TARGETS)}); default all")
    ap.add_argument("--fast", action="store_true",
                    help="draft quality (much quicker, larger files)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo every ffmpeg command")
    args = ap.parse_args()

    unknown = [t for t in args.targets if t not in TARGETS]
    if unknown:
        print(f"error: unknown target(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"       choose from: {', '.join(TARGETS)}", file=sys.stderr)
        return 2

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            print(f"error: {tool} not found on PATH", file=sys.stderr)
            return 1

    preset = "veryfast" if args.fast else "slow"
    hero_crf = 28 if args.fast else HERO_CRF
    teaser_crf = 26 if args.fast else TEASER_CRF
    # medium is visually indistinguishable from slow on this footage and several
    # times faster; the twitter file is short enough that crf does the work.
    tw_preset = "veryfast" if args.fast else "medium"
    # Point clouds are close to noise, so x264 spends bits on them. 20 is still
    # sharp on a phone, and well above what Twitter keeps after it recompresses.
    tw_crf = 24 if args.fast else 20
    targets = args.targets or [t for t in TARGETS if t not in ("twitter", "static-dynamic")]

    if "teaser-text" in targets:
        if not ffmpeg_has_filter("drawtext"):
            print("error: this ffmpeg has no drawtext filter, which the long "
                  "teaser needs for captions.\n"
                  "       the twitter cut does not: "
                  "scripts/make_teaser.py twitter", file=sys.stderr)
            return 1
        for font in (FONT_BOLD, FONT_REG):
            if not os.path.exists(font):
                print(f"error: font not found: {font}", file=sys.stderr)
                return 1

    faces = load_fonts() if "twitter" in targets else None

    if "hero" in targets:
        build(hero_edit(), *HERO_SIZE, os.path.join(OUT_DIR, "hero_loop.mp4"),
              with_text=False, xfade=HERO_XFADE, fade_in=0.0, fade_out=0.0,
              loop_fold=HERO_XFADE, crf=hero_crf, preset=preset,
              verbose=args.verbose)

    for name, with_text in (("teaser-plain", False), ("teaser-text", True)):
        if name in targets:
            fn = "teaser_text.mp4" if with_text else "teaser_plain.mp4"
            build(teaser_edit(), *TEASER_SIZE, os.path.join(OUT_DIR, fn),
                  with_text=with_text, xfade=XFADE, fade_in=0.8, fade_out=1.0,
                  loop_fold=0.0, crf=teaser_crf, preset=preset,
                  verbose=args.verbose)

    if "twitter" in targets:
        build(twitter_edit(), *TEASER_SIZE,
              os.path.join(OUT_DIR, "twitter.mp4"),
              with_text=True, xfade=XFADE_TW, fade_in=0.0, fade_out=0.0,
              loop_fold=0.0, crf=tw_crf, preset=tw_preset,
              verbose=args.verbose, faces=faces)

    if "static-dynamic" in targets:
        out = os.path.join(OUT_DIR, "static_dynamic.mp4")
        print(f"\n=> {os.path.relpath(out, ROOT)}  (1920x960)")
        render_static_dynamic(out, tw_crf, tw_preset, args.verbose)
        print(f"   done: {probe_duration(out):.1f}s, {os.path.getsize(out) / 1e6:.1f} MB")

    print(f"\nOutputs in {os.path.relpath(OUT_DIR, ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
