"""Generate the Open Graph / social-share cards (1200x630 PNG), committed to
src/web/static/ and served statically — so production needs no Pillow.

    python tools/make_og.py            # both cards
    python tools/make_og.py ortho      # just ortho

ponytail: hand-drawn PIL cards, no design tool, no per-page dynamic rendering.
Two profiles share one drawing routine because the layout is the same and only the
palette, the words and the motif's meaning differ. Generation uses Manrope if it can
be fetched (the HOPCo typeface) and falls back to Windows Arial; the committed PNG ships.
"""
import io
import os
import sys
import urllib.request

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "src", "web", "static")
W, H = 1200, 630

CARDS = {
    "consumer": {
        "out": "og.png",
        "bg": (22, 51, 0), "accent": (159, 232, 112), "pale": (226, 246, 213),
        "mute": (150, 170, 130), "bar": (38, 74, 12),
        "eyebrow": "ARIZONA MEDICAL PRICE  &  BILL COACH",
        "head": ["What should your care", "cost in Arizona?"],
        "sub": ["Compare prices across 53 hospitals, find the fair price,",
                "and negotiate your bill down."],
        "scale": ("Medicare benchmark", "fair price", "hospital list price"),
        "domain": "healthcare.traqqit.com",
        "foot": "Free  ·  No signup  ·  Estimates only",
    },
    # HOPCo palette, scraped from hopco.com: #003A70 navy, #00ADEF cyan, #07285E deep.
    "ortho": {
        "out": "og-ortho.png",
        "bg": (0, 58, 112), "accent": (0, 173, 239), "pale": (214, 230, 245),
        "mute": (130, 166, 202), "bar": (7, 40, 94),
        "eyebrow": "ORTHOPEDIC PRICE INTELLIGENCE  ·  HOPCO",
        "head": ["Where to send the patient,", "and what it will cost."],
        "sub": ["Every published orthopedic and spine price across Arizona, Virginia,",
                "Nevada, Michigan and Florida — by facility, by payer, benchmarked to Medicare."],
        "scale": ("Medicare benchmark", "lowest nearby", "highest published"),
        "domain": "ortho.traqqit.com",
        "foot": "CMS price-transparency files  ·  Benchmarking data",
    },
}

MANROPE = ("https://github.com/sharanda/manrope/raw/master/fonts/ttf/"
           "Manrope-{}.ttf")
ARIAL = {"Bold": "C:/Windows/Fonts/arialbd.ttf", "Regular": "C:/Windows/Fonts/arial.ttf"}
_cache = {}


def face(weight):
    """Manrope if reachable (the HOPCo typeface), else Arial. Cached per run."""
    if weight in _cache:
        return _cache[weight]
    try:
        with urllib.request.urlopen(MANROPE.format(weight), timeout=8) as r:
            _cache[weight] = r.read()
    except Exception:
        _cache[weight] = ARIAL[weight]
    return _cache[weight]


def f(weight, size):
    src = face(weight)
    return ImageFont.truetype(io.BytesIO(src) if isinstance(src, bytes) else src, size)


def draw(cfg):
    img = Image.new("RGB", (W, H), cfg["bg"])
    d = ImageDraw.Draw(img)
    white, pad = (255, 255, 255), 80
    d.rectangle([0, 0, W, 10], fill=cfg["accent"])

    d.text((pad, 70), cfg["eyebrow"], font=f("Bold", 26), fill=cfg["accent"])
    for i, line in enumerate(cfg["head"]):
        d.text((pad, 130 + i * 84), line, font=f("Bold", 72), fill=white)
    for i, line in enumerate(cfg["sub"]):
        d.text((pad, 322 + i * 44), line, font=f("Regular", 30), fill=cfg["pale"])

    # price-spectrum motif: the range, the benchmark and where the good answer sits
    bx, by, bw, bh = pad, 452, W - 2 * pad, 26
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=13, fill=cfg["bar"])
    fz0, fz1 = bx + int(bw * 0.18), bx + int(bw * 0.42)
    d.rounded_rectangle([fz0, by, fz1, by + bh], radius=13, fill=cfg["accent"])
    mx = bx + int(bw * 0.30)
    d.ellipse([mx - 16, by - 8, mx + 16, by + bh + 8], fill=white, outline=cfg["accent"], width=4)
    left, mid, right = cfg["scale"]
    d.text((bx, by + 44), left, font=f("Regular", 22), fill=cfg["mute"])
    d.text((mx, by + 44), mid, font=f("Bold", 22), fill=cfg["accent"], anchor="ma")
    d.text((bx + bw, by + 44), right, font=f("Regular", 22), fill=cfg["mute"], anchor="ra")

    d.text((pad, H - 66), cfg["domain"], font=f("Bold", 30), fill=white)
    d.text((W - pad, H - 62), cfg["foot"], font=f("Regular", 24), fill=cfg["mute"], anchor="ra")

    out = os.path.join(STATIC, cfg["out"])
    img.save(out, "PNG")
    print("wrote", out, os.path.getsize(out), "bytes")


def main():
    os.makedirs(STATIC, exist_ok=True)
    want = sys.argv[1:] or list(CARDS)
    for name in want:
        draw(CARDS[name])


if __name__ == "__main__":
    main()
