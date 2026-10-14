"""Generate the PWA icons in static/icons/ (run once; output is committed).

    python scripts/generate_icons.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"
TEAL = (15, 118, 110, 255)
WHITE = (255, 255, 255, 255)
SS = 4  # supersampling factor for smooth edges


def draw_trolley(draw, size, scale):
    """Draw a white trolley glyph centred in a square of `size` px, at `scale` of it."""
    u = size * scale / 24  # glyph is designed on a 24-unit grid
    ox = oy = (size - 24 * u) / 2
    w = max(2, round(2.2 * u))

    def p(x, y):
        return (ox + x * u, oy + y * u)

    draw.line([p(2.5, 4), p(5, 4), p(7.2, 15), p(18, 15), p(20.5, 7), p(6, 7)], fill=WHITE, width=w, joint="curve")
    for cx in (9, 17):
        r = 1.9 * u
        x, y = p(cx, 19)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)
    # small cap on the line start to round it off
    r = w / 2
    for x, y in (p(2.5, 4), p(6, 7)):
        draw.ellipse([x - r, y - r, x + r, y + r], fill=WHITE)


def make(size, name, maskable=False, rounded=True):
    big = size * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    if maskable:
        draw.rectangle([0, 0, big, big], fill=TEAL)
        draw_trolley(draw, big, 0.55)  # keep the glyph inside the 80% safe zone
    else:
        radius = big * 0.22 if rounded else 0
        draw.rounded_rectangle([0, 0, big - 1, big - 1], radius=radius, fill=TEAL)
        draw_trolley(draw, big, 0.72)
    img = img.resize((size, size), Image.LANCZOS)
    img.save(OUT / name, optimize=True)
    print("wrote", OUT / name)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    make(192, "icon-192.png")
    make(512, "icon-512.png")
    make(512, "icon-maskable-512.png", maskable=True)
    make(180, "apple-touch-icon.png", rounded=False)
