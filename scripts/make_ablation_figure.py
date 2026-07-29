#!/usr/bin/env python3
"""Compose a paper-style qualitative ablation figure (iters + WAFT)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ABLATIONS = ROOT / "assets" / "ablations"
OUT = ABLATIONS / "ablation_combined.png"

# Cell geometry (paper figure ~ two-column width at high res)
CELL_W = 640
CELL_H = 480
GUTTER = 10
LABEL_W = 72
TITLE_H = 56
PANEL_GAP = 48
PAD = 28
BG = (255, 255, 255)
FG = (20, 24, 29)
FG_SOFT = (67, 80, 95)

PANELS = [
    {
        "title": "(a) Iterative refinement",
        "rows": [
            ("w/o refinement", "iters/baseline"),
            ("w/ refinement", "iters/ours"),
        ],
    },
    {
        "title": "(b) WAFT",
        "rows": [
            ("w/o WAFT", "waft/baseline"),
            ("w/ WAFT", "waft/ours"),
        ],
    },
]

EXAMPLES = ("01", "02", "03")


def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def center_crop_to_aspect(img: Image.Image, aspect: float) -> Image.Image:
    w, h = img.size
    current = w / h
    if abs(current - aspect) < 1e-3:
        return img
    if current > aspect:
        new_w = int(round(h * aspect))
        left = (w - new_w) // 2
        return img.crop((left, 0, left + new_w, h))
    new_h = int(round(w / aspect))
    top = (h - new_h) // 2
    return img.crop((0, top, w, top + new_h))


def prepare_cell(path: Path) -> Image.Image:
    img = Image.open(path).convert("RGB")
    img = center_crop_to_aspect(img, CELL_W / CELL_H)
    return img.resize((CELL_W, CELL_H), Image.Resampling.LANCZOS)


def text_size(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def draw_rotated_label(
    canvas: Image.Image,
    text: str,
    *,
    box: tuple[int, int, int, int],
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
) -> None:
    """Draw vertically centered, 90°-rotated label inside box (x0,y0,x1,y1)."""
    x0, y0, x1, y1 = box
    tmp = Image.new("RGBA", (max(x1 - x0, 1) * 4, max(y1 - y0, 1) * 4), (0, 0, 0, 0))
    # Render horizontal text large enough, then rotate
    probe = Image.new("RGB", (10, 10), BG)
    probe_draw = ImageDraw.Draw(probe)
    tw, th = text_size(probe_draw, text, font)
    pad = 8
    layer = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((pad, pad), text, font=font, fill=fill + (255,))
    rotated = layer.rotate(90, expand=True)
    bw, bh = x1 - x0, y1 - y0
    rw, rh = rotated.size
    px = x0 + (bw - rw) // 2
    py = y0 + (bh - rh) // 2
    canvas.alpha_composite(rotated, (px, py))


def compose_panel(
    panel: dict,
    *,
    title_font: ImageFont.ImageFont,
    label_font: ImageFont.ImageFont,
) -> Image.Image:
    n_cols = len(EXAMPLES)
    n_rows = len(panel["rows"])
    grid_w = n_cols * CELL_W + (n_cols - 1) * GUTTER
    grid_h = n_rows * CELL_H + (n_rows - 1) * GUTTER
    width = LABEL_W + grid_w
    height = TITLE_H + grid_h

    panel_img = Image.new("RGBA", (width, height), BG + (255,))
    draw = ImageDraw.Draw(panel_img)

    # Title
    tw, th = text_size(draw, panel["title"], title_font)
    draw.text(((width - tw) // 2, (TITLE_H - th) // 2), panel["title"], font=title_font, fill=FG)

    for r, (row_label, folder) in enumerate(panel["rows"]):
        y = TITLE_H + r * (CELL_H + GUTTER)
        draw_rotated_label(
            panel_img,
            row_label,
            box=(0, y, LABEL_W, y + CELL_H),
            font=label_font,
            fill=FG_SOFT,
        )
        for c, ex in enumerate(EXAMPLES):
            x = LABEL_W + c * (CELL_W + GUTTER)
            path = ABLATIONS / folder / f"{ex}.png"
            cell = prepare_cell(path)
            panel_img.paste(cell, (x, y))

    return panel_img


def main() -> None:
    title_font = load_font(36, bold=True)
    label_font = load_font(26, bold=False)

    panels = [
        compose_panel(p, title_font=title_font, label_font=label_font) for p in PANELS
    ]

    panel_w = max(p.width for p in panels)
    total_h = PAD + sum(p.height for p in panels) + PANEL_GAP * (len(panels) - 1) + PAD
    total_w = PAD * 2 + panel_w

    canvas = Image.new("RGBA", (total_w, total_h), BG + (255,))
    y = PAD
    for i, panel in enumerate(panels):
        x = PAD + (panel_w - panel.width) // 2
        canvas.alpha_composite(panel, (x, y))
        y += panel.height
        if i < len(panels) - 1:
            y += PANEL_GAP

    OUT.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(OUT, format="PNG", optimize=True)
    print(f"Wrote {OUT} ({canvas.width}x{canvas.height})")


if __name__ == "__main__":
    main()
