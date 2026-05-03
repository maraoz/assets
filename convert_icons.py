"""One-shot: convert every PNG under icons/<category>/ to a 64x64 WebP and
delete the original. The display size is 22-32px so 64x64 (~2x retina) is
plenty sharp; webp typically beats PNG by 30-60% at this size and quality.

Skips icons/casa-araoz.* (the footer/empty-state logo) — that's an SVG.
"""

import sys
from pathlib import Path
from PIL import Image

ROOT = Path(__file__).parent
ICONS = ROOT / "icons"
TARGET = 64


def fit_to_square(img: Image.Image, side: int) -> Image.Image:
    """Resize preserving aspect ratio, pad transparently to a square."""
    img.thumbnail((side, side), Image.LANCZOS)
    if img.size == (side, side):
        return img
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.size[0]) // 2, (side - img.size[1]) // 2))
    return canvas


def main():
    n_ok = n_skip = 0
    saved_bytes = 0
    for png in ICONS.rglob("*.png"):
        # Skip non-asset PNGs at the root of /icons (e.g., casa-araoz.png)
        if png.parent == ICONS:
            continue
        webp = png.with_suffix(".webp")
        if webp.exists():
            n_skip += 1
            continue
        try:
            img = Image.open(png).convert("RGBA")
            img = fit_to_square(img, TARGET)
            # quality 82 + method 6 — slightly slower encode, smaller file
            img.save(webp, format="WEBP", quality=82, method=6)
            old = png.stat().st_size
            new = webp.stat().st_size
            saved_bytes += old - new
            png.unlink()
            n_ok += 1
        except Exception as e:
            print(f"  err {png}: {e}", file=sys.stderr)

    print(f"converted {n_ok} files, skipped {n_skip}")
    print(f"saved {saved_bytes / 1024:.1f} KB total")


if __name__ == "__main__":
    main()
