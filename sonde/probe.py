#!/usr/bin/env python3
"""
PC Price Hunter - sonde de faisabilité.

Lit une poignée de pages marchandes (targets.json) depuis GitHub Actions et
note, pour chacune : accessible ou bloquée, robots.txt, données structurées,
prix, disponibilité, vendeur, EAN/MPN.

Règles :
  - robots.txt respecté (page interdite = non lue) ;
  - identification honnête (User-Agent explicite), aucune contrefaçon ;
  - aucune tentative de contourner un captcha ou une protection ;
  - 1 requête toutes les ~6 s par site ;
  - aucun prix inventé : si on ne trouve rien, on écrit « rien trouvé ».
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.robotparser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RESULTS = ROOT / "results"

REPO = os.environ.get("GITHUB_REPOSITORY", "local")
UA = f"Mozilla/5.0 (compatible; PCPriceHunter/0.1; +https://github.com/{REPO}) personal price tracker"
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.7,en;q=0.5",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
DELAY_PER_HOST = 6.0
TIMEOUT = 25

BLOCK_MARKERS = [
    "captcha", "datadome", "cf-chl", "challenge-platform", "just a moment...",
    "attention required", "access denied", "robot check", "javascript is not enabled",
    "enable javascript", "are you a human", "px-captcha", "perimeterx",
]

_last_hit: dict[str, float] = {}
# base -> ("parsed", RobotFileParser) | ("open", raison) | ("closed", raison)
_robots: dict[str, tuple[str, object]] = {}
session = requests.Session()
session.headers.update(HEADERS)


# --------------------------------------------------------------------------- outils
def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def polite_get(url: str) -> requests.Response:
    host = urlparse(url).netloc
    wait = DELAY_PER_HOST - (time.time() - _last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    try:
        return session.get(url, timeout=TIMEOUT, allow_redirects=True)
    finally:
        _last_hit[host] = time.time()


def robots_allows(url: str) -> tuple[bool, str]:
    """Convention usuelle (RFC 9309) :
    200 -> on applique les règles ; 4xx -> pas de restriction déclarée ;
    5xx / 429 / erreur réseau -> prudence : on ne lit pas le site."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        try:
            r = polite_get(base + "/robots.txt")
            if r.status_code == 200:
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(r.text.splitlines())
                _robots[base] = ("parsed", rp)
            elif 400 <= r.status_code < 500 and r.status_code != 429:
                _robots[base] = ("open", f"robots.txt absent/refusé (HTTP {r.status_code})")
            else:
                _robots[base] = ("closed", f"robots.txt indisponible (HTTP {r.status_code})")
        except requests.RequestException as e:
            _robots[base] = ("closed", f"site injoignable ({type(e).__name__})")
    state, val = _robots[base]
    if state == "open":
        return True, str(val)
    if state == "closed":
        return False, str(val)
    ok = val.can_fetch(UA, url)  # type: ignore[union-attr]
    return ok, "autorisé par robots.txt" if ok else "interdit par robots.txt"


def looks_blocked(resp: requests.Response) -> str | None:
    if resp.status_code in (401, 403, 405, 429, 503):
        return f"HTTP {resp.status_code}"
    head = resp.text[:60000].lower()
    for m in BLOCK_MARKERS:
        if m in head:
            # une page produit normale peut contenir le mot "captcha" (formulaire d'avis…)
            # on ne conclut au blocage que si la page est petite ou sans données produit
            if len(resp.text) < 40000 or "application/ld+json" not in head:
                return f"protection détectée (« {m} »)"
    return None


def to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(" ", "").replace("€", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s):  # 1.394,00
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", s):  # 394,00
        s = s.replace(",", ".")
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def short(schema_url) -> str | None:
    if not schema_url:
        return None
    return str(schema_url).rstrip("/").split("/")[-1]


# --------------------------------------------------------------------------- extraction
def iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                yield node
                if "@graph" in node:
                    stack.append(node["@graph"])


def is_type(node: dict, name: str) -> bool:
    t = node.get("@type")
    return t == name or (isinstance(t, list) and name in t)


def parse_offer(off) -> dict:
    if isinstance(off, list):
        # plusieurs offres : on garde la moins chère qui a un prix
        cands = [parse_offer(o) for o in off]
        cands = [c for c in cands if c.get("price") is not None]
        return min(cands, key=lambda c: c["price"]) if cands else {}
    if not isinstance(off, dict):
        return {}
    price = to_float(off.get("price") or off.get("lowPrice"))
    if price is None and isinstance(off.get("priceSpecification"), dict):
        price = to_float(off["priceSpecification"].get("price"))
    seller = off.get("seller")
    if isinstance(seller, dict):
        seller = seller.get("name")
    return {
        "price": price,
        "currency": off.get("priceCurrency"),
        "availability": short(off.get("availability")),
        "condition": short(off.get("itemCondition")),
        "seller": seller,
        "offer_type": off.get("@type"),
    }


def extract_product(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out: dict = {"method": None}
    for node in iter_jsonld(soup):
        if is_type(node, "Product") or is_type(node, "ProductGroup"):
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            out.update({
                "method": "JSON-LD schema.org",
                "name": node.get("name"),
                "brand": brand,
                "sku": node.get("sku"),
                "mpn": node.get("mpn"),
                "ean": node.get("gtin13") or node.get("gtin") or node.get("gtin14") or node.get("ean"),
            })
            out.update({k: v for k, v in parse_offer(node.get("offers")).items() if v is not None})
            if out.get("price") is not None:
                return out
    # repli : microdonnées / balises meta
    def meta(*sel):
        for s in sel:
            el = soup.select_one(s)
            if el and (el.get("content") or el.get_text(strip=True)):
                return el.get("content") or el.get_text(strip=True)
        return None
    price = to_float(meta('[itemprop="price"]', 'meta[property="product:price:amount"]',
                          'meta[property="og:price:amount"]'))
    if price is not None:
        out.update({
            "method": out["method"] or "microdonnées / meta",
            "price": price,
            "currency": meta('[itemprop="priceCurrency"]', 'meta[property="product:price:currency"]'),
            "availability": short(meta('[itemprop="availability"]', 'link[itemprop="availability"]')),
            "name": out.get("name") or meta('meta[property="og:title"]', "h1"),
        })
    if not out.get("name"):
        t = soup.find("h1") or soup.find("title")
        out["name"] = t.get_text(" ", strip=True)[:160] if t else None
    return out


def shopify(target: dict) -> dict:
    url = target["url"].split("?")[0].rstrip("/") + ".js"
    ok, why = robots_allows(url)
    data = None
    if ok:
        try:
            r = polite_get(url)
            if r.status_code == 200:
                data = r.json()
        except (requests.RequestException, ValueError):
            data = None
    if data is None:  # repli : page HTML normale (données structurées)
        res = read_page(target["url"])
        res["note"] = f"JSON Shopify indisponible ({why}) – repli page : " + str(res.get("note", ""))
        return res
    variants = data.get("variants") or []
    v = next((x for x in variants if x.get("id") == target.get("variant")), variants[0] if variants else {})
    price = v.get("price")
    return {
        "status": "ok" if price is not None else "pas_de_données",
        "method": "Shopify JSON public (.js)",
        "name": f"{data.get('title')} – {v.get('title')}" if v.get("title") not in (None, "Default Title") else data.get("title"),
        "brand": data.get("vendor"),
        "sku": v.get("sku"),
        "ean": v.get("barcode"),
        "price": price / 100 if isinstance(price, (int, float)) else to_float(price),
        "currency": "(devise de la boutique, à confirmer)",
        "availability": "InStock" if v.get("available") else "OutOfStock",
        "note": why,
    }


def refused(why: str) -> dict:
    if "interdit" in why:
        return {"status": "robots_interdit", "note": why}
    return {"status": "bloqué" if "HTTP" in why else "erreur", "note": why}


def read_page(url: str) -> dict:
    ok, why = robots_allows(url)
    if not ok:
        return refused(why)
    try:
        r = polite_get(url)
    except requests.RequestException as e:
        return {"status": "erreur", "note": f"{type(e).__name__}: {str(e)[:120]}"}
    blocked = looks_blocked(r)
    if blocked:
        return {"status": "bloqué", "note": blocked, "http": r.status_code}
    if r.status_code != 200:
        return {"status": "erreur", "note": f"HTTP {r.status_code}", "http": r.status_code}
    info = extract_product(r.text)
    info.update({"http": 200, "final_url": r.url, "note": why})
    info["status"] = "ok" if info.get("price") is not None else "pas_de_données"
    return info


def search_then_read(target: dict, comp: dict) -> dict:
    url = target["url"]
    ok, why = robots_allows(url)
    if not ok:
        res = refused(why)
        res["note"] = "recherche : " + res["note"]
        return res
    try:
        r = polite_get(url)
    except requests.RequestException as e:
        return {"status": "erreur", "note": f"{type(e).__name__}"}
    blocked = looks_blocked(r)
    if blocked or r.status_code != 200:
        return {"status": "bloqué" if blocked else "erreur", "note": f"recherche : {blocked or 'HTTP ' + str(r.status_code)}"}
    soup = BeautifulSoup(r.text, "lxml")
    pat = target.get("link_pattern", "")
    must = [m.lower() for m in comp.get("must", [])]
    excl = [x.lower() for x in comp.get("exclude", [])]
    found = []
    for a in soup.find_all("a", href=True):
        href = urljoin(r.url, a["href"])
        text = (a.get_text(" ", strip=True) + " " + (a.get("title") or "")).lower()
        if pat in href and all(m in (text + href.lower()) for m in must) and not any(x in text for x in excl):
            if href not in found:
                found.append(href)
    if not found:
        return {"status": "pas_de_données", "note": "recherche lue, aucun lien produit reconnu (page sans doute générée en JavaScript)"}
    res = read_page(found[0])
    res["note"] = f"trouvé via recherche ({len(found)} lien(s)) – " + str(res.get("note", ""))
    res["found_url"] = found[0]
    return res


# --------------------------------------------------------------------------- contrôles
def identity_check(res: dict, comp: dict) -> list[str]:
    warn = []
    name = (res.get("name") or "").lower()
    if name:
        miss = [m for m in comp.get("must", []) if m.lower() not in name]
        if miss:
            warn.append("nom ne contient pas : " + ", ".join(miss))
        bad = [x for x in comp.get("exclude", []) if x.lower() in name]
        if bad:
            warn.append("variante à exclure ? : " + ", ".join(bad))
    if res.get("ean") and comp.get("ean") and str(res["ean"]).lstrip("0") not in [e.lstrip("0") for e in comp["ean"]]:
        warn.append(f"EAN {res['ean']} ≠ attendu")
    if res.get("mpn") and comp.get("mpn") and res["mpn"].replace(" ", "").upper() not in [m.replace(" ", "").upper() for m in comp["mpn"]]:
        warn.append(f"MPN {res['mpn']} ≠ attendu")
    if res.get("condition") and res["condition"] not in ("NewCondition",):
        warn.append(f"état : {res['condition']}")
    return warn


# --------------------------------------------------------------------------- rapport
STATUS_FR = {"ok": "✅ prix lu", "pas_de_données": "🟡 page lue, pas de prix", "bloqué": "⛔ bloqué",
             "robots_interdit": "🚫 interdit (robots.txt)", "erreur": "❌ erreur"}


def fmt_eur(v) -> str:
    return "–" if v is None else f"{v:,.2f} €".replace(",", " ").replace(".", ",")


def write_report(cfg: dict, rows: list[dict], started: str) -> str:
    lines = [f"# Sonde PC Price Hunter – {started} (UTC)", "",
             f"Exécutée depuis : `{os.environ.get('RUNNER_NAME', 'local')}` / dépôt `{REPO}`", ""]
    total = len(rows)
    ok = sum(r["status"] == "ok" for r in rows)
    lines += [f"**{ok}/{total} pages avec un prix lisible.**", ""]
    for key, comp in cfg["components"].items():
        sub = [r for r in rows if r["c"] == key]
        if not sub:
            continue
        lines += [f"## {comp['label']}", "",
                  "| Marchand | Statut | Produit lu | Prix affiché | HTVA calculé | Dispo | Vendeur | EAN / MPN | Méthode | Remarques |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for r in sub:
            ids = " / ".join(str(x) for x in (r.get("ean"), r.get("mpn")) if x) or "–"
            rem = "; ".join([str(r.get("note") or "")] + r.get("warnings", []))
            name = (r.get("name") or "–").replace("|", "/")[:70]
            lines.append(f"| {r['merchant']} | {STATUS_FR.get(r['status'], r['status'])} | {name} | "
                         f"{fmt_eur(r.get('price'))} | {fmt_eur(r.get('htva'))} | {r.get('availability') or '–'} | "
                         f"{r.get('seller') or '–'} | {ids} | {r.get('method') or '–'} | {rem.replace('|', '/')} |")
        lines.append("")
    lines += ["---", "HTVA = prix affiché ÷ (1 + TVA du site). Frais de livraison non inclus à ce stade.",
              "Aucun prix n'est inventé : une case « – » signifie « non trouvé »."]
    return "\n".join(lines)


def notify(summary: str) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC absent : pas de notification de test.")
        return
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        r = requests.post(f"{server}/{topic}", data=summary.encode("utf-8"),
                          headers={"Title": "PC Price Hunter - sonde terminee", "Tags": "mag", "Priority": "3"},
                          timeout=15)
        print("Notification ntfy :", r.status_code)
    except requests.RequestException as e:
        print("Notification ntfy impossible :", type(e).__name__)


def main() -> int:
    cfg = json.loads((HERE / "targets.json").read_text(encoding="utf-8"))
    started = now_iso()
    rows = []
    for t in cfg["targets"]:
        comp = cfg["components"][t["c"]]
        try:
            if t["kind"] == "shopify":
                res = shopify(t)
            elif t["kind"] == "search":
                res = search_then_read(t, comp)
            else:
                res = read_page(t["url"])
        except Exception as e:  # une source qui plante ne doit jamais arrêter les autres
            res = {"status": "erreur", "note": f"{type(e).__name__}: {str(e)[:120]}"}
        res.update({"c": t["c"], "merchant": t["merchant"], "url": t["url"], "vat": t.get("vat"),
                    "checked_at": now_iso()})
        if res.get("price") is not None and t.get("vat") is not None:
            res["htva"] = round(res["price"] / (1 + t["vat"]), 2)
        res["warnings"] = identity_check(res, comp) if res["status"] == "ok" else []
        rows.append(res)
        print(f"[{res['status']:>15}] {t['merchant']:<32} {fmt_eur(res.get('price')):>12}  {res.get('note', '')}", flush=True)

    RESULTS.mkdir(exist_ok=True)
    (RESULTS / "sonde-latest.json").write_text(json.dumps({"started": started, "ua": UA, "rows": rows},
                                                          ensure_ascii=False, indent=1), encoding="utf-8")
    report = write_report(cfg, rows, started)
    (RESULTS / "sonde-latest.md").write_text(report, encoding="utf-8")
    ok = sum(r["status"] == "ok" for r in rows)
    notify(f"Sonde terminee : {ok}/{len(rows)} pages avec un prix lisible. Resultats dans results/sonde-latest.md")
    print(f"\n{ok}/{len(rows)} pages avec un prix lisible.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
