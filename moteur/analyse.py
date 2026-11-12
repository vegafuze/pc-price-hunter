"""
Vérification d'identité des offres, historique journalier, statistiques,
score d'opportunité et totaux de la configuration.
"""
from __future__ import annotations

import csv
import re
import statistics
from datetime import date, timedelta
from pathlib import Path

DISPO_OK = {"InStock", "LimitedAvailability", "InStoreOnly", "OnlineOnly"}


def norm(s) -> str:
    return re.sub(r"[\s\-_./]", "", str(s or "")).upper()


# ----------------------------------------------------------------------------- identité
def check_identity(info: dict, prod: dict, offer: dict) -> tuple[bool, str]:
    """Renvoie (accepté, raison). Un EAN/MPN connu prime sur le nom."""
    if info.get("condition") and info["condition"] not in ("NewCondition",):
        return False, f"état {info['condition']} (pas neuf)"
    seller = (info.get("seller") or "").lower()
    expected = [s.lower() for s in offer.get("vendeur_attendu", [])]
    if seller and expected and not any(e in seller for e in expected):
        return False, f"vendeur tiers ({info.get('seller')})"
    options = prod.get("options") or [prod.get("match", {})]
    name = (info.get("name") or "").lower()
    for opt in options:
        eans = [e.lstrip("0") for e in opt.get("ean", [])]
        mpns = [norm(m) for m in opt.get("mpn", [])]
        page_ean = str(info.get("ean") or "").lstrip("0")
        page_mpn = norm(info.get("mpn"))
        if eans and page_ean:
            if page_ean in eans:
                return True, "EAN vérifié"
            continue
        if mpns and page_mpn:
            if any(m in page_mpn for m in mpns):
                return True, "référence fabricant vérifiée"
            continue
        if not name:
            continue
        if any(x.lower() in name for x in opt.get("exclude", [])):
            continue
        if not all(m.lower() in name for m in opt.get("must", [])):
            continue
        if not all(re.search(rx, name, re.I) for rx in opt.get("regex", [])):
            continue
        if opt.get("ean") or opt.get("mpn"):
            return True, "nom conforme (identifiant absent de la page)"
        return True, "nom conforme"
    return False, "produit non conforme"


def offer_state(info: dict) -> str:
    """ok = prix + en stock ; rupture = prix mais pas en stock."""
    st = info.get("status")
    if st != "ok":
        return st or "erreur"
    return "ok" if (info.get("availability") in DISPO_OK) else "rupture"


# ----------------------------------------------------------------------------- historique
def load_daily(path: Path) -> dict[str, dict[str, float]]:
    """{produit: {date_iso: meilleur_htva}}"""
    out: dict[str, dict[str, float]] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                out.setdefault(row["produit"], {})[row["date"]] = float(row["meilleur_htva"])
            except (KeyError, ValueError):
                continue
    return out


def save_daily(path: Path, daily: dict[str, dict[str, float]], extra: dict[tuple[str, str], dict]) -> None:
    rows = []
    for prod, series in daily.items():
        for d, v in series.items():
            meta = extra.get((prod, d), {})
            rows.append({"date": d, "produit": prod, "meilleur_htva": f"{v:.2f}",
                         "marchand": meta.get("marchand", ""), "url": meta.get("url", "")})
    rows.sort(key=lambda r: (r["date"], r["produit"]))
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "produit", "meilleur_htva", "marchand", "url"])
        w.writeheader()
        w.writerows(rows)


def load_daily_meta(path: Path) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out[(row.get("produit", ""), row.get("date", ""))] = {"marchand": row.get("marchand", ""),
                                                                    "url": row.get("url", "")}
    return out


# ----------------------------------------------------------------------------- statistiques
def percentile_rank(values: list[float], v: float) -> float:
    if not values:
        return float("nan")
    below = sum(1 for x in values if x < v)
    equal = sum(1 for x in values if x == v)
    return round(100 * (below + 0.5 * equal) / len(values), 1)


def clamp(x: float, lo: float = 0, hi: float = 100) -> float:
    return max(lo, min(hi, x))


def compute_stats(series: dict[str, float], today: date, v: float | None, cible: float,
                  plafond: float, weights: dict, previous: float | None, min_days: int = 4) -> dict:
    """Statistiques sur valeurs journalières (une valeur par jour observé : pas de doublons)."""
    st: dict = {"historique_jours": 0, "historique_suffisant": False}
    if v is None:
        return st
    d7 = [series[d] for d in series if today - date.fromisoformat(d) < timedelta(days=7)]
    d30 = [series[d] for d in series if today - date.fromisoformat(d) < timedelta(days=30)]
    past30 = [series[d] for d in series
              if timedelta(days=0) < today - date.fromisoformat(d) < timedelta(days=30)]
    yesterday = series.get((today - timedelta(days=1)).isoformat())
    st.update({
        "ecart_eur": round(v - cible, 2),
        "ecart_pct": round((v - cible) / cible * 100, 1) if cible else None,
        "historique_jours": len(d30),
        "historique_suffisant": len(d7) >= min_days,
        "med7": round(statistics.median(d7), 2) if d7 else None,
        "min7": min(d7) if d7 else None,
        "max7": max(d7) if d7 else None,
        "pct7": percentile_rank(d7, v) if d7 else None,
        "med30": round(statistics.median(d30), 2) if d30 else None,
        "min30": min(d30) if d30 else None,
        "min30_passe": min(past30) if past30 else None,
        "var_derniere": round(v - previous, 2) if previous is not None else None,
        "var_veille": round(v - yesterday, 2) if yesterday is not None else None,
        "volatilite_pct": (round(statistics.pstdev(d30) / statistics.mean(d30) * 100, 1)
                           if len(d30) >= 3 else None),
    })
    # score d'opportunité : indicateur relatif, pas une probabilité
    parts = {"cible": clamp(50 + 5 * (cible - v) / cible * 100) if cible else 50}
    if st["historique_suffisant"]:
        parts["percentile"] = clamp(100 - st["pct7"])
        parts["mediane"] = clamp(50 + 5 * (st["med7"] - v) / st["med7"] * 100)
        parts["minimum"] = clamp(100 - 10 * (v - st["min30"]) / st["min30"] * 100)
    wsum = sum(weights.get(k, 0) for k in parts)
    st["score"] = round(sum(parts[k] * weights.get(k, 0) for k in parts) / wsum) if wsum else None
    st["score_detail"] = {k: round(x) for k, x in parts.items()}
    s = st["score"] or 0
    st["verdict"] = ("objectif atteint" if v <= cible else
                     "excellente occasion" if s >= 80 else "bonne occasion" if s >= 60 else
                     "prix correct" if s >= 40 else "pas intéressant")
    st["sous_plafond"] = v <= plafond
    return st


# ----------------------------------------------------------------------------- totaux
def line_value(best: dict | None, qty: int) -> float | None:
    return None if not best else round(best["htva"] * qty, 2)


def compute_totals(products: list[dict], blocs: list[dict], bests: dict[str, dict | None]) -> dict:
    """Totaux HTVA par catégorie. Un bloc (ex. watercooling + ventilateurs) prend son scénario le moins cher."""
    in_bloc = {pid for b in blocs for sc in b["scenarios"] for pid in sc["items"]}
    res = {}
    for cat in ("pc", "acc"):
        total, missing, stale, lines = 0.0, [], [], []
        for p in products:
            if p["cat"] != cat or p["id"] in in_bloc or p.get("hors_total"):
                continue
            b = bests.get(p["id"])
            if not b:
                missing.append(p["label"])
                continue
            total += b["htva"] * p.get("qty", 1)
            if b.get("fraicheur") != "vérifié":
                stale.append(p["label"])
            lines.append(p["id"])
        for bloc in blocs:
            if bloc.get("cat", "pc") != cat:
                continue
            best_sc = None
            for sc in bloc["scenarios"]:
                cost, ok, st = 0.0, True, False
                for pid, q in sc["items"].items():
                    b = bests.get(pid)
                    if not b:
                        ok = False
                        break
                    cost += b["htva"] * q
                    st = st or b.get("fraicheur") != "vérifié"
                if ok and (best_sc is None or cost < best_sc["cout"]):
                    best_sc = {"label": sc["label"], "cout": round(cost, 2), "ancien": st, "items": sc["items"]}
            bloc["choisi"] = best_sc
            if best_sc is None:
                missing.append(bloc["label"])
            else:
                total += best_sc["cout"]
                if best_sc["ancien"]:
                    stale.append(bloc["label"])
        res[cat] = {"htva": round(total, 2), "tvac": round(total * 1.21, 2), "manquants": missing,
                    "anciens": stale, "complet": not missing, "verifie": not missing and not stale}
    g = res["pc"]["htva"] + res["acc"]["htva"]
    res["general"] = {"htva": round(g, 2), "tvac": round(g * 1.21, 2),
                      "complet": res["pc"]["complet"] and res["acc"]["complet"]}
    return res
