"""
Alertes Keepa (Amazon) reçues par e-mail dans une boîte ntfy privée.

Keepa envoie ses alertes à ntfy-<sujet>@ntfy.sh ; ntfy les garde 12 h. À chaque passage, le moteur
lit les nouveaux messages de cette boîte (secret KEEPA_TOPIC), reconnaît le produit par son ASIN et
renvoie une notification propre sur le téléphone. Le texte reçu est une donnée non fiable :
il est seulement lu, jamais exécuté, et seuls les ASIN de config/amazon.json sont reconnus.
"""
from __future__ import annotations

import json
import os
import re

import requests

ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")
DOMAINE_RE = re.compile(r"amazon\.(fr|de|com\.be|nl|it|es)\b", re.I)
KEEPA_DOMAINE = {"4": "fr", "3": "de"}  # identifiants Keepa : keepa.com/#!product/4-ASIN


def poll(topic: str, since: str | None) -> list[dict]:
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}/json"
    r = requests.get(url, params={"poll": "1", "since": since or "all"}, timeout=20)
    r.raise_for_status()
    out = []
    for line in r.text.splitlines():
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if m.get("event") == "message":
            out.append(m)
    return out


def clean(text: str) -> str:
    text = text.replace("\r", "").replace("&copy;", "©").replace("&euro;", "€").replace("&nbsp;", " ")
    text = re.sub(r"https?://\S+", "", text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def analyse(msg: dict, asins: dict) -> dict:
    """Renvoie {type, asin, produit, domaine, texte}. type : alerte | verification | autre."""
    titre, corps = msg.get("title", ""), msg.get("message", "")
    tout = f"{titre}\n{corps}"
    if "/verify/" in tout or re.search(r"confirm", titre, re.I):
        return {"type": "verification"}
    trouves = [a for a in ASIN_RE.findall(tout) if a in asins]
    asin = trouves[0] if trouves else None
    dom = None
    mk = re.search(r"product/(\d)-" + (asin or "B0"), tout)
    if mk:
        dom = KEEPA_DOMAINE.get(mk.group(1))
    if not dom:
        md = DOMAINE_RE.search(tout)
        dom = md.group(1).lower() if md else None
    return {"type": "alerte" if asin else "autre", "asin": asin, "produit": asins.get(asin) if asin else None,
            "domaine": dom, "titre": titre.strip(), "texte": clean(corps)[:700]}
