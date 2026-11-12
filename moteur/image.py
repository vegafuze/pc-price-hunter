"""
Image récapitulative (PNG) pour le téléphone : composant, meilleur prix HTVA, écart à la cible.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1080
BG, CARD, LINE = (18, 20, 26), (30, 33, 42), (52, 56, 68)
TXT, MUTED = (236, 238, 243), (150, 156, 170)
GREEN, YELLOW, RED, GREY = (52, 199, 120), (240, 190, 60), (235, 87, 87), (130, 136, 150)
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")


def font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(str(FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)
    except OSError:
        return ImageFont.load_default(size=size)


def eur(v) -> str:
    if v is None:
        return "–"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s.replace(",00", "") + " €"


def fit(draw, text: str, f, width: int) -> str:
    if draw.textlength(text, font=f) <= width:
        return text
    while text and draw.textlength(text + "…", font=f) > width:
        text = text[:-1]
    return text + "…"


def render(path: Path, title: str, totals: list[tuple[str, str]], sections: list[tuple[str, list[dict]]]) -> None:
    """sections : [(titre, [{"label", "prix", "ecart", "couleur", "note"}])]"""
    rows = sum(len(r) for _, r in sections)
    h = 170 + 92 * ((len(totals) + 1) // 2) + 110 * len(sections) + 78 * rows + 90
    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    f_title, f_big, f_txt, f_small = font(46, True), font(36, True), font(30), font(24)
    d.text((48, 44), "PC Price Hunter", font=f_title, fill=TXT)
    d.text((48, 104), title, font=f_small, fill=MUTED)
    y = 160
    for i, (lab, val) in enumerate(totals):  # tuiles de totaux, 2 par ligne
        x = 48 + (i % 2) * 500
        if i % 2 == 0 and i:
            y += 92
        d.rounded_rectangle((x, y, x + 484, y + 80), 16, fill=CARD)
        d.text((x + 20, y + 10), lab, font=f_small, fill=MUTED)
        d.text((x + 20, y + 38), val, font=font(30, True), fill=TXT)
    y += 120
    for name, items in sections:
        d.text((48, y), name, font=f_big, fill=TXT)
        d.text((640, y + 10), "meilleur HTVA", font=f_small, fill=MUTED)
        d.text((850, y + 10), "écart", font=f_small, fill=MUTED)
        y += 60
        for it in items:
            d.line((48, y, W - 48, y), fill=LINE, width=1)
            col = {"vert": GREEN, "jaune": YELLOW, "rouge": RED}.get(it["couleur"], GREY)
            d.ellipse((52, y + 26, 72, y + 46), fill=col)
            d.text((88, y + 18), fit(d, it["label"], f_txt, 530), font=f_txt, fill=TXT)
            d.text((640, y + 18), it["prix"], font=f_txt, fill=TXT if it["couleur"] != "gris" else MUTED)
            d.text((850, y + 18), it["ecart"], font=f_txt, fill=col)
            if it.get("note"):
                d.text((640, y + 52), it["note"], font=font(18), fill=MUTED)
            y += 78
        y += 50
    d.text((48, h - 60), "Vert : sous la cible · jaune : sous le plafond · rouge : au-dessus · gris : en rupture",
           font=font(20), fill=MUTED)
    img.save(path, optimize=True)
