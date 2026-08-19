#!/usr/bin/env python3
"""Build the TrackEverything teaser videos from the clips already in assets/.

Three cuts come out of the same edit list:

  hero          assets/teaser/hero_loop.mp4     30s, 1440x540, silent, no text,
                                                seamlessly looping. For the top of
                                                the website.
  teaser-plain  assets/teaser/teaser_plain.mp4  54s, 1920x1080, silent, no text.
  teaser-text   assets/teaser/teaser_text.mp4   59s, 1920x1080, silent, with a
                                                title, beat captions and an end card.

The source clips render on a black background, so every cut composites onto a
black canvas: the letterboxing is invisible and the content appears to float.

Usage:
  scripts/make_teaser.py                      # build everything
  scripts/make_teaser.py hero teaser-text     # build a subset
  scripts/make_teaser.py --fast               # draft quality, much quicker
"""

from __future__ import annotations

import argparse
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


# --------------------------------------------------------------------------- #
# ffmpeg helpers
# --------------------------------------------------------------------------- #

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
        Seg(f"{R}/pandas_great.mp4", 2.4, 4.2),

        # --- range of scenes ------------------------------------------------
        Seg(f"{R}/ours_a2de8ecf0da5.mp4", 1.2, 3.6),
        Seg(f"{R}/ours-breakdance.mp4", 1.8, 3.6),
        Seg(f"{R}/ours_horsejump-high_30fps.mp4", 0.0, 2.2),
        Seg(f"{R}/ours_tennis.mp4", 0.2, 3.4, cues=[
            head_cue("tracking happens in a persistent 3D scene", 0.4, 3.0),
            sub_cue("repeat views of a surface fuse; occluded points persist",
                    0.8, 3.0),
        ]),
        Seg(f"{R}/basketball.mp4", 1.0, 3.4),
        Seg(f"{R}/ours_swing.mp4", 0.2, 3.2),
        Seg(f"{R}/ours_963a498a493b.mp4", 0.2, 3.2),

        # --- long video -----------------------------------------------------
        Seg(f"{L}/uptown_1.mp4", 3.0, 7.0, speed=4.5, blur=4, cues=[
            head_cue("1000+ frames, in a single pass", 0.7, 6.4),
            sub_cue("prior dense 3D trackers run out of memory past ~96 frames",
                    1.1, 6.4),
        ]),
        Seg(f"{L}/pod_3.mp4", 2.0, 6.0, speed=5.5, blur=5, cues=[
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
        Seg(f"{R}/pandas_great.mp4", 2.4, 3.6),
        Seg(f"{R}/ours_a2de8ecf0da5.mp4", 1.2, 3.4),
        Seg(f"{R}/ours-breakdance.mp4", 1.8, 3.6),
        Seg(f"{L}/uptown_1.mp4", 3.0, 4.2, speed=4.5, blur=4),
        Seg(f"{R}/basketball.mp4", 1.0, 3.4),
        Seg(f"{R}/ours_tennis.mp4", 0.2, 3.2),
        Seg(f"{L}/pod_3.mp4", 2.0, 3.8, speed=5.5, blur=5),
        Seg(f"{R}/ours_horsejump-high_30fps.mp4", 0.0, 2.2),
    ]


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #

def build(edit: list, w: int, h: int, out: str, with_text: bool, xfade: float,
          fade_in: float, fade_out: float, loop_fold: float,
          crf: int, preset: str, verbose: bool) -> None:
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


TARGETS = ("hero", "teaser-plain", "teaser-text")


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
    for font in (FONT_BOLD, FONT_REG):
        if not os.path.exists(font):
            print(f"error: font not found: {font}", file=sys.stderr)
            return 1

    preset = "veryfast" if args.fast else "slow"
    hero_crf = 28 if args.fast else HERO_CRF
    teaser_crf = 26 if args.fast else TEASER_CRF
    targets = args.targets or list(TARGETS)

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

    print(f"\nOutputs in {os.path.relpath(OUT_DIR, ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
