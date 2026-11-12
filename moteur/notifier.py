"""
Alertes ntfy : règles de déclenchement, anti-doublons, heures calmes et bilan quotidien.
"""
from __future__ import annotations

import os
from datetime import datetime

import requests


def eur(v) -> str:
    if v is None:
        return "–"
    s = f"{v:,.2f}".replace(",", " ").replace(".", ",")
    return s.replace(",00", "") + " €"


def pct(v) -> str:
    return "–" if v is None else f"{v:+.1f} %".replace(".", ",")


def send_ntfy(title: str, message: str, click: str | None, priority: int, tags: list[str],
              status_url: str) -> str:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return "pas de NTFY_TOPIC"
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    actions = []
    if click and click != status_url:
        actions.append({"action": "view", "label": "Voir l'offre", "url": click})
    actions.append({"action": "view", "label": "Récapitulatif", "url": status_url})
    body = {"topic": topic, "title": title, "message": message, "priority": priority,
            "tags": tags, "actions": actions, "click": click or status_url}
    try:
        r = requests.post(server, json=body, timeout=15)
        return f"HTTP {r.status_code}"
    except requests.RequestException as e:
        return type(e).__name__


def mood(pct7) -> str:
    """Traduit le percentile 7 jours : 0 % = prix le plus bas de la semaine, 100 % = le plus haut."""
    if pct7 is None:
        return ""
    if pct7 <= 10:
        return "🤩 Exceptionnel"
    if pct7 <= 25:
        return "😃 Très bien"
    if pct7 <= 50:
        return "🙂 Bien"
    if pct7 <= 75:
        return "😐 Moyen"
    return "😟 Cher pour la semaine"


def send_ntfy_image(title: str, message: str, png, status_url: str) -> str:
    """Envoie le bilan avec l'image récapitulative en pièce jointe (repli : texte seul)."""
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        return "pas de NTFY_TOPIC"
    server = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    try:
        data = open(png, "rb").read()
        r = requests.put(f"{server}/{topic}", data=data, timeout=30,
                         params={"title": title, "message": message, "click": status_url, "tags": "bar_chart",
                                 "priority": "2", "filename": "recapitulatif.png"})
        if r.status_code < 300:
            return f"HTTP {r.status_code} (avec image)"
    except (OSError, requests.RequestException):
        pass
    return send_ntfy(title, message, status_url, 2, ["bar_chart"], status_url) + " (sans image)"


def short_label(label: str) -> str:
    return label.split(" (")[0]


def title_for(p: dict, reasons: list[str]) -> str:
    r = reasons[0] if reasons else ""
    head = "🎯 Cible atteinte" if "cible" in r else "📉 Nouveau minimum" if "minimum" in r else "⚡ Bonne affaire"
    return f"{head} – {short_label(p['label'])}"


def product_message(p: dict, best: dict, st: dict, reasons: list[str], totals: dict | None) -> str:
    """Message court et lisible : une info par ligne, le détail complet est sur la page d'état."""
    v = best["htva"] * p.get("qty", 1)
    qty = f" (lot de {p['qty']})" if p.get("qty", 1) > 1 else ""
    lines = [
        f"💰 {eur(v)} HTVA · {eur(v * 1.21)} TVAC{qty}",
        f"🎯 Cible {eur(p['cible'])} · plafond {eur(p['plafond'])}",
        f"↘️ Écart {eur(st.get('ecart_eur'))} ({pct(st.get('ecart_pct'))})",
    ]
    if st.get("historique_suffisant"):
        lines.append(f"📊 {mood(st['pct7'])} : moins cher que {100 - st['pct7']:.0f} % des prix des 7 derniers jours"
                     f" (médiane {eur(st['med7'])})")
    else:
        lines.append(f"📊 Historique : {st.get('historique_jours', 0)} jour(s), encore insuffisant")
    lines.append(f"⭐ Score {st.get('score')}/100 · {st.get('verdict')}")
    lines.append(f"🏪 {best['marchand']}" + (f" · vendu par {best['vendeur']}" if best.get("vendeur") else ""))
    if totals:
        lines.append(f"🖥️ Total PC : {eur(totals['pc']['htva'])} HTVA" + ("" if totals['pc']['complet'] else " (incomplet)"))
    if len(reasons) > 1 or (reasons and "nouvelle baisse" in reasons[0]):
        lines.append("ℹ️ " + ", ".join(reasons))
    return "\n".join(lines)


def evaluate_product(p: dict, best: dict | None, st: dict, alerts: dict, cfg: dict) -> tuple[list[str], bool]:
    """Renvoie (raisons, urgent) et met à jour l'état anti-doublons."""
    if not best or best.get("fraicheur") != "vérifié" or best.get("test"):
        return [], False
    qty = p.get("qty", 1)
    v = best["htva"] * qty
    drop = cfg["seuil_nouvelle_baisse_pct"] / 100
    reasons, urgent = [], False

    def fire(key: str, label: str, is_urgent: bool, clear_if: bool):
        nonlocal urgent
        prev = alerts.get(key)
        if clear_if:
            alerts.pop(key, None)
            return
        if prev is None:
            reasons.append(label)
        elif v <= prev["prix"] * (1 - drop) and prev["prix"] - v >= 1:
            reasons.append(label + " – nouvelle baisse")
        else:
            return
        alerts[key] = {"prix": round(v, 2), "ts": datetime.utcnow().isoformat(timespec="seconds")}
        urgent = urgent or is_urgent

    key = f"cible:{p['id']}"
    if v <= p["cible"]:
        fire(key, "prix cible atteint", True, clear_if=False)
    elif v > p["cible"] * 1.01:  # petite marge pour éviter les alertes en yo-yo
        alerts.pop(key, None)
    if st.get("historique_suffisant"):
        key = f"stat:{p['id']}"
        exceptional = (v <= p["plafond"] and st["pct7"] is not None and st["pct7"] <= cfg["percentile_exceptionnel"]
                       and v <= st["med7"] * (1 - cfg["ecart_mediane_exceptionnel_pct"] / 100))
        if exceptional:
            fire(key, "prix statistiquement exceptionnel", False, clear_if=False)
        elif (st["pct7"] or 100) > 30:
            alerts.pop(key, None)
        key = f"min:{p['id']}"
        if st.get("min30_passe") and v <= p["plafond"] and v < st["min30_passe"] * 0.99:
            fire(key, "nouveau minimum sur 30 jours", False, clear_if=False)
        elif key in alerts and v > alerts[key]["prix"] * 1.03:
            alerts.pop(key, None)
    return reasons, urgent


def evaluate_budget(totals: dict, alerts: dict, cfg: dict) -> list[tuple[str, float]]:
    out = []
    pc = totals["pc"]
    if not pc["verifie"]:
        return out
    v = pc["htva"]
    for seuil in sorted(cfg["budgets_alerte"], reverse=True):
        key = f"budget:{seuil}"
        prev = alerts.get(key)
        if v > seuil * 1.01:
            alerts.pop(key, None)
            continue
        if v <= seuil and (prev is None or v <= prev["prix"] * (1 - cfg["seuil_nouvelle_baisse_pct"] / 100)):
            alerts[key] = {"prix": v, "ts": datetime.utcnow().isoformat(timespec="seconds")}
            out.append((f"PC complet ≤ {eur(seuil)} HTVA", seuil))
    return sorted(out, key=lambda x: x[1])[:1]  # une seule notification : le seuil le plus bas atteint


def in_quiet_hours(now_local: datetime, cfg: dict) -> bool:
    start, end = cfg["heures_calmes"]
    h = now_local.hour
    return h >= start or h < end
