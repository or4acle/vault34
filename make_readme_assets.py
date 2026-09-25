"""Generate README assets: a logo and a set of sample images for the screenshot.

Run once from the project root. Output goes to images/ and media/inbox/; the
inbox images are throwaway content for the screenshot only.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
IMAGES = ROOT / "images"
INBOX = ROOT / "media" / "inbox"

BG = (14, 16, 20)
ACCENT = (110, 168, 254)
ACCENT2 = (167, 139, 250)


def _font(size: int, bold: bool = True):
    for name in (("segoeuib.ttf" if bold else "segoeui.ttf"),
                 "arialbd.ttf" if bold else "arial.ttf"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def gradient(size, top, bottom, diagonal=False):
    w, h = size
    im = Image.new("RGB", (1, h) if not diagonal else (w, h))
    if diagonal:
        im = Image.new("RGB", (w, h))
        px = im.load()
        for y in range(h):
            for x in range(w):
                t = (x / max(w - 1, 1) + y / max(h - 1, 1)) / 2
                px[x, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        return im
    for y in range(h):
        t = y / max(h - 1, 1)
        im.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return im.resize(size, Image.BILINEAR)


def make_logo(path: Path, size: int = 320) -> None:
    im = gradient((size, size), (18, 22, 32), (40, 30, 66), diagonal=True)
    d = ImageDraw.Draw(im, "RGBA")

    # soft accent glow
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([size * 0.12, size * 0.10, size * 0.88, size * 0.86],
                                 fill=(*ACCENT, 46))
    im = Image.alpha_composite(im.convert("RGBA"), glow.filter(
        ImageFilter.GaussianBlur(size * 0.07))).convert("RGB")

    d = ImageDraw.Draw(im)
    # vault dial: a ring with a handle, echoing the "vault" name
    cx, cy, r = size / 2, size * 0.40, size * 0.17
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=max(2, int(size * 0.022)))
    d.ellipse([cx - r * 0.42, cy - r * 0.42, cx + r * 0.42, cy + r * 0.42],
              outline=ACCENT2, width=max(2, int(size * 0.016)))
    d.line([cx, cy - r * 0.62, cx, cy], fill=ACCENT2, width=max(2, int(size * 0.016)))
    for k in range(8):
        a = math.radians(k * 45 + 22)
        d.line([cx + math.cos(a) * r * 1.14, cy + math.sin(a) * r * 1.14,
                cx + math.cos(a) * r * 1.30, cy + math.sin(a) * r * 1.30],
               fill=ACCENT, width=max(2, int(size * 0.018)))

    font = _font(int(size * 0.30))
    text = "34"
    box = d.textbbox((0, 0), text, font=font)
    d.text(((size - (box[2] - box[0])) / 2 - box[0], size * 0.60), text,
           font=font, fill=(245, 247, 252))
    im.save(path)
    print("wrote", path.relative_to(ROOT), im.size)


def sample_gradient(name, w, h, a, b, angle=None):
    im = gradient((w, h), a, b, diagonal=True).convert("RGB")
    im.save(INBOX / f"{name}.png")
    return im


def sample_scene(name, w, h, sky, land, sun):
    im = gradient((w, h), sky, (250, 240, 220))
    d = ImageDraw.Draw(im, "RGBA")
    d.ellipse([w * 0.62, h * 0.14, w * 0.62 + w * 0.16, h * 0.14 + w * 0.16], fill=sun)
    d.polygon([(0, h), (w * 0.35, h * 0.45), (w * 0.7, h)], fill=land)
    d.polygon([(w * 0.3, h), (w * 0.72, h * 0.55), (w, h)], fill=tuple(
        min(255, c + 22) for c in land))
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    im.save(INBOX / f"{name}.png")
    return im


def sample_shapes(name, w, h, bg, colours):
    im = Image.new("RGB", (w, h), bg)
    d = ImageDraw.Draw(im, "RGBA")
    cols = len(colours)
    for i, c in enumerate(colours):
        x0 = w * i / cols
        x1 = w * (i + 1) / cols
        d.rectangle([x0, 0, x1, h], fill=c)
        r = min(w, h) * 0.16
        ccx, ccy = (x0 + x1) / 2, h * (0.35 + 0.12 * (i % 3))
        d.ellipse([ccx - r, ccy - r, ccx + r, ccy + r], fill=(255, 255, 255, 60))
    im.save(INBOX / f"{name}.png")
    return im


def main() -> None:
    IMAGES.mkdir(exist_ok=True)
    INBOX.mkdir(parents=True, exist_ok=True)
    make_logo(IMAGES / "logo.png")

    sample_scene("aurora_ridge", 1280, 800, (28, 32, 74), (16, 20, 40), (255, 214, 140))
    sample_gradient("dusk", 1100, 1100, (250, 140, 90), (60, 30, 90))
    sample_shapes("palette", 1400, 900, (18, 20, 28),
                  [(110, 168, 254), (167, 139, 250), (74, 222, 128), (251, 191, 36)])
    sample_scene("harbour_dusk", 1200, 900, (40, 60, 110), (14, 18, 30), (255, 180, 120))
    sample_gradient("mint", 1000, 1400, (16, 40, 44), (74, 222, 160))
    sample_shapes("confetti", 1200, 1200, (24, 22, 34),
                  [(251, 113, 133), (251, 191, 36), (110, 168, 254), (167, 139, 250)])
    sample_gradient("ember", 1300, 900, (60, 12, 20), (255, 138, 60))
    sample_scene("pine_ridge", 1400, 800, (140, 190, 230), (18, 48, 38), (255, 250, 210))
    sample_shapes("strata", 1100, 1100, (20, 24, 30),
                  [(167, 139, 250), (110, 168, 254)])

    made = sorted(INBOX.glob("*.png"))
    print(f"wrote {len(made)} sample images to {INBOX.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
