"""Generate the Open Graph / social-share card (1200x630 PNG) once, committed to
src/web/static/og.png and served statically — so production needs no Pillow.
Re-run after a copy change:  python tools/make_og.py

ponytail: a hand-drawn PIL card, no design tool, no per-page dynamic rendering.
Uses Windows Arial for generation; the committed PNG is what ships.
"""
import os

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "src", "web", "static", "og.png")

W, H = 1200, 630
FOREST = (22, 51, 0)
GREEN = (159, 232, 112)
PALE = (226, 246, 213)
WHITE = (255, 255, 255)
MUTE = (150, 170, 130)

BOLD = "C:/Windows/Fonts/arialbd.ttf"
REG = "C:/Windows/Fonts/arial.ttf"


def f(path, size):
    return ImageFont.truetype(path, size)


def text(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img = Image.new("RGB", (W, H), FOREST)
    d = ImageDraw.Draw(img)

    # thin green top rule
    d.rectangle([0, 0, W, 10], fill=GREEN)

    pad = 80
    # eyebrow / brand
    text(d, (pad, 70), "ARIZONA MEDICAL PRICE  &  BILL COACH", f(BOLD, 26), GREEN)

    # headline (two lines)
    text(d, (pad, 130), "What should your care", f(BOLD, 76), WHITE)
    text(d, (pad, 214), "cost in Arizona?", f(BOLD, 76), WHITE)

    # subhead
    text(d, (pad, 322), "Compare prices across 53 hospitals, find the fair price,",
         f(REG, 34), PALE)
    text(d, (pad, 366), "and negotiate your bill down.", f(REG, 34), PALE)

    # price-spectrum motif (echoes the app's Bluebook-style range)
    bx, by, bw, bh = pad, 452, W - 2 * pad, 26
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=13, fill=(38, 74, 12))
    # green "fair zone" + marker dot
    fz0, fz1 = bx + int(bw * 0.18), bx + int(bw * 0.42)
    d.rounded_rectangle([fz0, by, fz1, by + bh], radius=13, fill=GREEN)
    mx = bx + int(bw * 0.30)
    d.ellipse([mx - 16, by - 8, mx + 16, by + bh + 8], fill=WHITE, outline=GREEN, width=4)
    text(d, (bx, by + 44), "Medicare benchmark", f(REG, 22), MUTE)
    text(d, (mx, by + 44), "fair price", f(BOLD, 22), GREEN, anchor="ma")
    text(d, (bx + bw, by + 44), "hospital list price", f(REG, 22), MUTE, anchor="ra")

    # footer strip
    text(d, (pad, H - 66), "healthcare.traqqit.com", f(BOLD, 30), WHITE)
    text(d, (W - pad, H - 62), "Free  ·  No signup  ·  Estimates only",
         f(REG, 26), MUTE, anchor="ra")

    img.save(OUT, "PNG")
    print("wrote", OUT, os.path.getsize(OUT), "bytes")


if __name__ == "__main__":
    main()
