"""
Lecture des pages marchandes : politesse, robots.txt, détection des blocages,
extraction des prix (JSON-LD schema.org, microdonnées, JSON public Shopify)
et découverte de fiches produit via les pages de recherche des marchands.

Règles : robots.txt respecté, User-Agent honnête, aucune tentative de
contourner une protection, une requête toutes les ~6 s par site.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.robotparser
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

REPO = os.environ.get("GITHUB_REPOSITORY", "local")
UA = f"Mozilla/5.0 (compatible; PCPriceHunter/1.0; +https://github.com/{REPO}) personal price tracker"
HEADERS = {"User-Agent": UA, "Accept-Language": "fr-BE,fr;q=0.9,nl;q=0.7,en;q=0.5",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
DELAY_PER_HOST = float(os.environ.get("PPH_DELAY", "6"))
TIMEOUT = 25
NBSP = chr(0xA0)

BLOCK_MARKERS = [
    "captcha", "datadome", "cf-chl", "challenge-platform", "just a moment...",
    "attention required", "access denied", "robot check", "javascript is not enabled",
    "enable javascript", "are you a human", "px-captcha", "perimeterx",
]

_last_hit: dict[str, float] = {}
_robots: dict[str, tuple[str, object]] = {}
session = requests.Session()
session.headers.update(HEADERS)


# ----------------------------------------------------------------------------- HTTP
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
    """RFC 9309 : 200 -> règles appliquées ; 4xx -> pas de restriction ;
    5xx / 429 / réseau -> prudence, on ne lit pas."""
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
                _robots[base] = ("open", f"robots.txt absent (HTTP {r.status_code})")
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
        if m in head and (len(resp.text) < 40000 or "application/ld+json" not in head):
            return f"protection détectée ({m})"
    return None


def refused(why: str) -> dict:
    if "interdit" in why:
        return {"status": "interdit", "note": why}
    return {"status": "bloque" if "HTTP" in why else "erreur", "note": why}


# ----------------------------------------------------------------------------- parsing
def to_float(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(NBSP, "").replace(" ", "").replace("€", "").replace("EUR", "")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+,\d{1,2}", s):
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", s):
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


def iter_jsonld(soup: BeautifulSoup):
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except ValueError:
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
        cands = [parse_offer(o) for o in off]
        cands = [c for c in cands if c.get("price") is not None]
        instock = [c for c in cands if c.get("availability") in ("InStock", "InStoreOnly", "LimitedAvailability")]
        pool = instock or cands
        return min(pool, key=lambda c: c["price"]) if pool else {}
    if not isinstance(off, dict):
        return {}
    if is_type(off, "AggregateOffer") and off.get("offers"):
        return parse_offer(off["offers"])
    price = to_float(off.get("price") or off.get("lowPrice"))
    if price is None and isinstance(off.get("priceSpecification"), dict):
        price = to_float(off["priceSpecification"].get("price"))
    seller = off.get("seller")
    if isinstance(seller, dict):
        seller = seller.get("name")
    return {"price": price, "currency": off.get("priceCurrency"),
            "availability": short(off.get("availability")),
            "condition": short(off.get("itemCondition")), "seller": seller}


def extract_product(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out: dict = {"method": None}
    for node in iter_jsonld(soup):
        if is_type(node, "Product") or is_type(node, "ProductGroup"):
            brand = node.get("brand")
            if isinstance(brand, dict):
                brand = brand.get("name")
            out.update({
                "method": "JSON-LD", "name": node.get("name"), "brand": brand,
                "sku": node.get("sku"), "mpn": node.get("mpn"),
                "ean": node.get("gtin13") or node.get("gtin") or node.get("gtin14") or node.get("ean"),
                "image": (node.get("image")[0] if isinstance(node.get("image"), list) and node.get("image")
                          else node.get("image") if isinstance(node.get("image"), str) else None),
            })
            out.update({k: v for k, v in parse_offer(node.get("offers")).items() if v is not None})
            if out.get("price") is not None:
                return out

    def meta(*sel):
        for s in sel:
            el = soup.select_one(s)
            if el and (el.get("content") or el.get_text(strip=True)):
                return el.get("content") or el.get_text(strip=True)
        return None
    price = to_float(meta('[itemprop="price"]', 'meta[property="product:price:amount"]',
                          'meta[property="og:price:amount"]'))
    if price is not None:
        out.update({"method": out["method"] or "microdonnées", "price": price,
                    "currency": meta('[itemprop="priceCurrency"]', 'meta[property="product:price:currency"]'),
                    "availability": short(meta('[itemprop="availability"]', 'link[itemprop="availability"]')),
                    "name": out.get("name") or meta('meta[property="og:title"]', "h1")})
    if not out.get("name"):
        t = soup.find("h1") or soup.find("title")
        out["name"] = t.get_text(" ", strip=True)[:160] if t else None
    return out


# ----------------------------------------------------------------------------- lecture
def read_page(url: str) -> dict:
    ok, why = robots_allows(url)
    if not ok:
        return refused(why)
    try:
        r = polite_get(url)
    except requests.RequestException as e:
        return {"status": "erreur", "note": f"{type(e).__name__}"}
    blocked = looks_blocked(r)
    if blocked:
        return {"status": "bloque", "note": blocked}
    if r.status_code == 404 or r.status_code == 410:
        return {"status": "disparu", "note": f"HTTP {r.status_code}"}
    if r.status_code != 200:
        return {"status": "erreur", "note": f"HTTP {r.status_code}"}
    info = extract_product(r.text)
    info["final_url"] = r.url
    info["status"] = "ok" if info.get("price") is not None else "sans_prix"
    return info


def read_shopify(url: str, variant=None) -> dict:
    js = url.split("?")[0].rstrip("/") + ".js"
    ok, why = robots_allows(js)
    data = None
    if ok:
        try:
            r = polite_get(js)
            if r.status_code == 200:
                data = r.json()
        except (requests.RequestException, ValueError):
            data = None
    if data is None:
        return read_page(url)
    variants = data.get("variants") or []
    v = next((x for x in variants if x.get("id") == variant), variants[0] if variants else {})
    price = v.get("price")
    title = data.get("title")
    if v.get("title") not in (None, "Default Title"):
        title = f"{title} – {v.get('title')}"
    img = data.get("featured_image")
    if isinstance(img, str) and img.startswith("//"):
        img = "https:" + img
    return {"status": "ok" if price is not None else "sans_prix", "method": "Shopify JSON",
            "name": title, "brand": data.get("vendor"), "sku": v.get("sku"), "ean": v.get("barcode"),
            "price": price / 100 if isinstance(price, (int, float)) else to_float(price),
            "availability": "InStock" if v.get("available") else "OutOfStock", "image": img}


def search_links(url: str, pattern: str, must: list[str], exclude: list[str], limit: int = 6) -> dict:
    """Lit une page de recherche et renvoie les liens produit candidats."""
    ok, why = robots_allows(url)
    if not ok:
        return refused("recherche : " + why)
    try:
        r = polite_get(url)
    except requests.RequestException as e:
        return {"status": "erreur", "note": f"recherche : {type(e).__name__}"}
    blocked = looks_blocked(r)
    if blocked or r.status_code != 200:
        return {"status": "bloque" if blocked else "erreur",
                "note": f"recherche : {blocked or 'HTTP ' + str(r.status_code)}"}
    soup = BeautifulSoup(r.text, "lxml")
    must_l = [m.lower() for m in must]
    excl_l = [x.lower() for x in exclude]
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(r.url, a["href"]).split("#")[0]
        text = (a.get_text(" ", strip=True) + " " + (a.get("title") or "") + " " + href).lower()
        if pattern in href and all(m in text for m in must_l) and not any(x in text for x in excl_l):
            if href not in found:
                found.append(href)
        if len(found) >= limit:
            break
    return {"status": "ok", "links": found, "note": f"{len(found)} lien(s) candidat(s)"}
