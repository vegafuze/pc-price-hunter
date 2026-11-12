#!/usr/bin/env python3
"""
PC Price Hunter – moteur de surveillance (une exécution = un passage complet).

1. découverte des fiches via les recherches marchandes (1 fois par jour)
2. lecture des fiches dues (robots.txt respecté, ~6 s entre deux pages d'un même site)
3. contrôle d'identité, historique, statistiques, score, totaux
4. alertes ntfy (anti-doublons, heures calmes) et bilan quotidien
5. écriture de data/ et de la page ETAT.md
"""
from __future__ import annotations

import copy
import csv
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyse  # noqa: E402
import notifier  # noqa: E402
import sources  # noqa: E402

ROOT = Path(os.environ.get("PPH_ROOT", Path(__file__).resolve().parent.parent))
CFG, DATA = ROOT / "config", ROOT / "data"
TZ = ZoneInfo("Europe/Brussels")
REPO = os.environ.get("GITHUB_REPOSITORY", "vegafuze/pc-price-hunter")
STATUS_URL = f"https://github.com/{REPO}/blob/main/ETAT.md"
RELEVE_FIELDS = ["ts", "produit", "marchand", "statut", "prix_affiche", "tva", "htva", "dispo",
                 "vendeur", "nom", "url", "note"]


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return default


def save_json(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def append_csv(p: Path, fields: list[str], rows: list[dict]) -> None:
    if not rows:
        return
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def minutes_since(iso: str | None, now: datetime) -> float:
    if not iso:
        return 1e9
    return (now - datetime.fromisoformat(iso)).total_seconds() / 60


def round_robin(tasks: list, url_of) -> list:
    """Alterne les sites pour ne jamais attendre inutilement le délai de politesse."""
    by_host: dict[str, list] = defaultdict(list)
    for t in tasks:
        by_host[urlparse(url_of(t)).netloc].append(t)
    out, queues = [], list(by_host.values())
    while any(queues):
        for q in queues:
            if q:
                out.append(q.pop(0))
    return out


def fmt_local(iso: str | None) -> str:
    if not iso:
        return "–"
    return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d/%m %H:%M")


def main() -> int:
    t0 = time.time()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    now_iso = now.isoformat()
    local = now.astimezone(TZ)
    today = local.date()
    force = os.environ.get("PPH_FORCE") == "1"

    cfgp = load_json(CFG / "produits.json", {})
    reg = load_json(CFG / "reglages.json", {})
    products, blocs = cfgp["produits"], cfgp.get("blocs", [])
    etat = load_json(DATA / "etat.json", {})
    for k in ("offres", "decouverte", "alertes", "derniers_meilleurs"):
        etat.setdefault(k, {})
    etat.setdefault("file_attente", [])
    releves, notif_rows = [], []
    counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # ------------------------------------------------------------- 1. découverte
    searches = []
    for p in products:
        for s in p.get("recherches", []):
            key = f"{p['id']}|{s['url']}"
            d = etat["decouverte"].get(key)
            if force or not d or minutes_since(d.get("ts"), now) >= reg["decouverte_heures"] * 60:
                searches.append((p, s, key))
    for p, s, key in round_robin(searches, lambda t: t[1]["url"]):
        res = sources.search_links(s["url"], s["motif"], s.get("liens_must", []),
                                   s.get("liens_exclude", []), s.get("max", 4))
        etat["decouverte"][key] = {"ts": now_iso, "statut": res["status"], "note": res.get("note"),
                                   "liens": res.get("links", []), "rejetes": []}
        counters[s["marchand"] + " (recherche)"][res["status"]] += 1

    # ------------------------------------------------------------- 2. lecture des fiches
    tasks = []
    for p in products:
        interval = p.get("intervalle_min", reg["intervalle_defaut_min"])
        offers, seen = [], set()
        for o in p.get("offres", []):
            offers.append(dict(o))
        for s in p.get("recherches", []):
            d = etat["decouverte"].get(f"{p['id']}|{s['url']}", {})
            for link in d.get("liens", []):
                if link not in d.get("rejetes", []):
                    offers.append({"marchand": s["marchand"], "url": link, "tva": s.get("tva"), "type": "page",
                                   "via": f"{p['id']}|{s['url']}", "vendeur_attendu": s.get("vendeur_attendu", [])})
        for o in offers:
            if o["url"] in seen:
                continue
            seen.add(o["url"])
            last = etat["offres"].get(f"{p['id']}|{o['url']}")
            if force or not last or minutes_since(last.get("derniere_verif"), now) >= interval - 5:
                tasks.append((p, o))

    fetched: dict[tuple, dict] = {}  # une même page n'est lue qu'une fois par passage
    for p, o in round_robin(tasks, lambda t: t[1]["url"]):
        key = f"{p['id']}|{o['url']}"
        memo = (o.get("type"), o["url"], o.get("variant"))
        if memo not in fetched:
            try:
                fetched[memo] = (sources.read_shopify(o["url"], o.get("variant")) if o.get("type") == "shopify"
                                 else sources.read_page(o["url"]))
            except Exception as e:  # une source qui plante n'arrête jamais les autres
                fetched[memo] = {"status": "erreur", "note": f"{type(e).__name__}"}
        info = dict(fetched[memo])
        state = info.get("status", "erreur")
        if info.get("final_url") and sources.site_of(info["final_url"]) != sources.site_of(o["url"]):
            state, info["note"] = "non_conforme", "redirigé vers un autre site : ignoré"
            info["price"], info["final_url"] = None, None
        if state == "ok":
            accepted, why = analyse.check_identity(info, p, o)
            if not accepted:
                state, info["note"] = "non_conforme", why
                if o.get("via"):
                    etat["decouverte"].setdefault(o["via"], {}).setdefault("rejetes", []).append(o["url"])
            else:
                state = analyse.offer_state(info)
                info["note"] = why
                vat = o.get("tva") if o.get("tva") is not None else 0.21
                line = info["price"] / (1 + vat) * p.get("qty", 1)
                if line > p["plafond"] * 3 or line < p["cible"] * 0.35:
                    state, info["note"] = "non_conforme", "prix incohérent (lot, PC complet ou accessoire ?)"
                    if o.get("via"):
                        etat["decouverte"].setdefault(o["via"], {}).setdefault("rejetes", []).append(o["url"])
        tva = o.get("tva")
        tva_supposee = tva is None
        tva = 0.21 if tva is None else tva
        price = info.get("price")
        htva = round(price / (1 + tva), 2) if price is not None else None
        prev = etat["offres"].get(key, {})
        rec = {
            "produit": p["id"], "marchand": o["marchand"], "url": info.get("final_url") or o["url"],
            "statut": state, "prix": price, "tva": tva, "tva_supposee": tva_supposee, "htva": htva,
            "dispo": info.get("availability"), "vendeur": info.get("seller"), "nom": info.get("name"),
            "methode": info.get("method"), "note": info.get("note"), "mpn": info.get("mpn"), "ean": info.get("ean"), "image": info.get("image") or prev.get("image"),
            "derniere_verif": now_iso, "dernier_ok": now_iso if state == "ok" else prev.get("dernier_ok"),
            "dernier_prix_ok": htva if state == "ok" else prev.get("dernier_prix_ok"),
            "dernier_prix_connu": htva if htva is not None else prev.get("dernier_prix_connu"),
            "date_prix_connu": now_iso if htva is not None else prev.get("date_prix_connu"),
            "test": bool(o.get("test")),
        }
        if (prev.get("statut"), prev.get("prix"), prev.get("dispo")) != (state, price, rec["dispo"]):
            releves.append({"ts": now_iso, "produit": p["id"], "marchand": o["marchand"], "statut": state,
                            "prix_affiche": price, "tva": tva, "htva": htva, "dispo": rec["dispo"],
                            "vendeur": rec["vendeur"], "nom": (rec["nom"] or "")[:120], "url": rec["url"],
                            "note": rec["note"]})
        etat["offres"][key] = rec
        counters[o["marchand"]][state] += 1
        print(f"[{state:>12}] {p['id']:<14} {o['marchand']:<26} {notifier.eur(price):>12}  {info.get('note', '')}",
              flush=True)

    # ------------------------------------------------------------- 3. meilleurs prix, stats, totaux
    daily = analyse.load_daily(DATA / "journalier.csv")
    meta = analyse.load_daily_meta(DATA / "journalier.csv")
    weights = reg["poids_score"]
    bests, stats = {}, {}
    for p in products:
        interval = p.get("intervalle_min", reg["intervalle_defaut_min"])
        offs = [o for k, o in etat["offres"].items() if o["produit"] == p["id"] and not o.get("test")]
        fresh = [o for o in offs if o["statut"] == "ok" and minutes_since(o["derniere_verif"], now) <= interval + 45]
        best = None
        if fresh:
            b = min(fresh, key=lambda o: o["htva"])
            best = dict(b, fraicheur="vérifié")
        else:
            old = [o for o in offs if o["statut"] in ("erreur", "bloque") and o.get("dernier_prix_ok")
                   and minutes_since(o.get("dernier_ok"), now) <= 24 * 60]
            if old:
                b = min(old, key=lambda o: o["dernier_prix_ok"])
                best = dict(b, htva=b["dernier_prix_ok"], fraicheur="ancien")
        bests[p["id"]] = best
        series = daily.setdefault(p["id"], {})
        v_line = round(best["htva"] * p.get("qty", 1), 2) if best else None
        if best and best["fraicheur"] == "vérifié":
            t = today.isoformat()
            if t not in series or v_line < series[t]:
                series[t] = v_line
                meta[(p["id"], t)] = {"marchand": best["marchand"], "url": best["url"]}
        stats[p["id"]] = analyse.compute_stats(series, today, v_line, p["cible"], p["plafond"], weights,
                                               etat["derniers_meilleurs"].get(p["id"]),
                                               reg.get("jours_min_historique", 4))
        if v_line is not None:
            etat["derniers_meilleurs"][p["id"]] = v_line

    totals = analyse.compute_totals(products, blocs, bests)
    # total « tous articles » : ajoute le dernier prix connu (< 14 jours) des articles en rupture, à titre indicatif
    bests_all = {}
    for p in products:
        offs = [o for o in etat["offres"].values() if o["produit"] == p["id"] and not o.get("test")
                and o["statut"] not in ("non_conforme", "interdit", "disparu")
                and (o.get("dernier_prix_connu") or o.get("htva")) is not None
                and minutes_since(o.get("date_prix_connu") or o["derniere_verif"], now) <= 14 * 24 * 60]
        if bests[p["id"]]:
            bests_all[p["id"]] = dict(bests[p["id"]], en_stock=True)
        elif offs:
            b = min(offs, key=lambda o: o.get("dernier_prix_connu") or o["htva"])
            bests_all[p["id"]] = dict(b, htva=b.get("dernier_prix_connu") or b["htva"], fraicheur="rupture",
                                      en_stock=False, derniere_verif=b.get("date_prix_connu") or b["derniere_verif"])
        else:
            bests_all[p["id"]] = None
    blocs_all = copy.deepcopy(blocs)
    totals_all = analyse.compute_totals(products, blocs_all, bests_all)
    totals_all["blocs"] = blocs_all
    for name, key in (("_total_pc", "pc"), ("_total_accessoires", "acc")):
        if totals[key]["complet"]:
            s = daily.setdefault(name, {})
            t = today.isoformat()
            if t not in s or totals[key]["htva"] < s[t]:
                s[t] = totals[key]["htva"]
    yday = (today - timedelta(days=1)).isoformat()
    totals["pc"]["veille"] = daily.get("_total_pc", {}).get(yday)
    totals["acc"]["veille"] = daily.get("_total_accessoires", {}).get(yday)

    # ------------------------------------------------------------- 4. alertes
    quiet = notifier.in_quiet_hours(local, reg)
    sent = 0

    def push(kind, title, msg, click, prio, tags, pid=""):
        nonlocal sent
        res = notifier.send_ntfy(title, msg, click, prio, tags, STATUS_URL)
        sent += 1
        notif_rows.append({"ts": now_iso, "type": kind, "produit": pid, "titre": title, "resultat": res})

    for p in products:
        reasons, urgent = notifier.evaluate_product(p, bests[p["id"]], stats[p["id"]], etat["alertes"], reg)
        if not reasons:
            continue
        best = bests[p["id"]]
        msg = notifier.product_message(p, best, stats[p["id"]], reasons, totals if p["cat"] == "pc" else None)
        if urgent or not quiet:
            push("produit", notifier.title_for(p, reasons), msg, best["url"], 4 if urgent else 3,
                 ["tada" if urgent else "chart_with_downwards_trend"], p["id"])
        else:
            etat["file_attente"].append({"ts": now_iso, "titre": p["label"], "message": msg, "url": best["url"]})
            notif_rows.append({"ts": now_iso, "type": "en attente (nuit)", "produit": p["id"],
                               "titre": p["label"], "resultat": "reporté à 7 h"})
    for bloc in blocs:  # alerte sur le coût du bloc (ex. watercooling + 8 ventilateurs)
        c = bloc.get("choisi")
        if not c or c["ancien"]:
            continue
        series = daily.setdefault(bloc["id"], {})
        t = today.isoformat()
        if t not in series or c["cout"] < series[t]:
            series[t] = c["cout"]
        pseudo = {"id": bloc["id"], "label": bloc["label"], "cible": bloc["cible"], "plafond": bloc["plafond"],
                  "qty": 1, "cat": bloc.get("cat", "pc")}
        st = analyse.compute_stats(series, today, c["cout"], bloc["cible"], bloc["plafond"], weights,
                                   etat["derniers_meilleurs"].get(bloc["id"]), reg.get("jours_min_historique", 4))
        etat["derniers_meilleurs"][bloc["id"]] = c["cout"]
        stats[bloc["id"]] = st
        pbest = {"htva": c["cout"], "marchand": "plusieurs (" + c["label"] + ")", "url": STATUS_URL,
                 "fraicheur": "vérifié"}
        reasons, urgent = notifier.evaluate_product(pseudo, pbest, st, etat["alertes"], reg)
        if reasons:
            msg = notifier.product_message(pseudo, pbest, st, reasons, totals)
            push("bloc", bloc["label"], msg, STATUS_URL, 4 if urgent else 3, ["tada"], bloc["id"])
    for label, seuil in notifier.evaluate_budget(totals, etat["alertes"], reg):
        msg = (f"Total PC : {notifier.eur(totals['pc']['htva'])} HTVA ({notifier.eur(totals['pc']['tvac'])} TVAC)\n"
               f"Budget : {notifier.eur(seuil)} HTVA – toutes les offres sont disponibles et vérifiées.\n"
               "Détail des meilleures offres dans le récapitulatif.")
        push("budget", label, msg, STATUS_URL, 5, ["rotating_light"])
    if not quiet and etat["file_attente"]:
        q = etat["file_attente"]
        msg = "\n\n".join(f"{x['titre']}\n{x['message']}" for x in q)[:3800]
        push("nuit", f"{len(q)} opportunité(s) détectée(s) cette nuit", msg, q[0]["url"], 3, ["moon"])
        etat["file_attente"] = []

    if local.hour >= reg["bilan_heure"] and etat.get("bilan_date") != today.isoformat():
        pc, acc = totals["pc"], totals["acc"]
        close = sorted((p for p in products if bests[p["id"]] and stats[p["id"]].get("ecart_pct") is not None),
                       key=lambda p: stats[p["id"]]["ecart_pct"])[:3]
        ta = totals_all
        lines = [f"🖥️ PC complet : {notifier.eur(ta['pc']['htva'])} HTVA ({notifier.eur(ta['pc']['tvac'])} TVAC)",
                 f"✅ dont en stock : {notifier.eur(pc['htva'])} HTVA",
                 f"🎯 Budget 2 500 € : {notifier.eur(ta['pc']['htva'] - 2500)}",
                 f"🎧 Accessoires : {notifier.eur(ta['acc']['htva'])} HTVA",
                 f"🧾 Total général : {notifier.eur(ta['general']['htva'])} HTVA"]
        if pc.get("veille") is not None:
            lines.append(f"📈 Variation PC depuis hier : {notifier.eur(pc['htva'] - pc['veille'])}")
        lines += ["", "Les plus proches de la cible :"]
        for p in close:
            stp = stats[p["id"]]
            dot = "🟢" if stp["ecart_eur"] <= 0 else "🟡" if stp.get("sous_plafond") else "🔴"
            lines.append(f"{dot} {notifier.short_label(p['label'])} : {notifier.eur(bests[p['id']]['htva'] * p.get('qty', 1))} "
                         f"({notifier.pct(stp['ecart_pct'])})")
        push("bilan", "📊 Bilan du jour", "\n".join(lines), STATUS_URL, 2, ["bar_chart"])
        etat["bilan_date"] = today.isoformat()

    # ------------------------------------------------------------- 5. écriture
    pages_ok = sum(c.get("ok", 0) + c.get("rupture", 0) for c in counters.values())
    if pages_ok:
        etat["derniere_reussite"] = now_iso
    etat["derniere_execution"] = now_iso
    DATA.mkdir(parents=True, exist_ok=True)
    analyse.save_daily(DATA / "journalier.csv", daily, meta)
    append_csv(DATA / "releves" / f"{today:%Y-%m}.csv", RELEVE_FIELDS, releves)
    append_csv(DATA / "notifications.csv", ["ts", "type", "produit", "titre", "resultat"], notif_rows)
    run = {"ts": now_iso, "duree_s": round(time.time() - t0), "recherches": len(searches), "pages": len(tasks),
           "ok": sum(c.get("ok", 0) for c in counters.values()),
           "rupture": sum(c.get("rupture", 0) for c in counters.values()),
           "bloque": sum(c.get("bloque", 0) + c.get("interdit", 0) for c in counters.values()),
           "erreur": sum(c.get("erreur", 0) + c.get("sans_prix", 0) + c.get("disparu", 0) for c in counters.values()),
           "notifications": sent}
    append_csv(DATA / "executions.csv", list(run), [run])
    etat["sources_derniere_execution"] = {m: dict(c) for m, c in counters.items()}
    save_json(DATA / "etat.json", etat)
    save_json(DATA / "resume.json", {"maj": now_iso, "totaux": totals, "blocs": blocs, "totaux_tous_articles": totals_all,
                                     "meilleurs": bests, "stats": stats})
    (ROOT / "ETAT.md").write_text(render_status(products, blocs, bests, stats, totals, etat, now_iso, bests_all, totals_all),
                                  encoding="utf-8")
    print(json.dumps(run, ensure_ascii=False))
    return 0


def render_status(products, blocs, bests, stats, totals, etat, now_iso, bests_all=None, totals_all=None) -> str:
    e = notifier.eur
    pc, acc, gen = totals["pc"], totals["acc"], totals["general"]

    def flag(t):
        return "" if t["verifie"] else (" ⚠️ estimé" if t["complet"] else " ⚠️ incomplet")

    out = ["# PC Price Hunter – état actuel", "",
           f"**Dernière actualisation réussie : {fmt_local(etat.get('derniere_reussite'))}** (heure belge) · "
           f"dernière exécution : {fmt_local(now_iso)}", "",
           "| | HTVA | TVAC (21 %) |", "|---|---|---|",
           f"| **PC**{flag(pc)} | **{e(pc['htva'])}** | {e(pc['tvac'])} |",
           f"| Écart au budget 2 400 € / 2 500 € | {e(pc['htva'] - 2400)} / {e(pc['htva'] - 2500)} | |",
           f"| Accessoires{flag(acc)} | {e(acc['htva'])} | {e(acc['tvac'])} |",
           f"| Total général | {e(gen['htva'])} | {e(gen['tvac'])} |"]
    if totals_all:
        ta = totals_all
        out += [f"| **PC – tous articles** (ruptures comprises) | **{e(ta['pc']['htva'])}** | {e(ta['pc']['tvac'])} |",
                f"| Accessoires – tous articles | {e(ta['acc']['htva'])} | {e(ta['acc']['tvac'])} |",
                f"| Total général – tous articles | {e(ta['general']['htva'])} | {e(ta['general']['tvac'])} |"]
    if pc.get("veille") is not None:
        out.append(f"| Variation PC depuis hier | {e(pc['htva'] - pc['veille'])} | |")
    out += ["", "_Totaux hors frais de livraison. Les premières lignes ne comptent que les offres en stock ; "
            "« tous articles » ajoute le dernier prix connu des articles en rupture, à titre indicatif "
            "(les alertes budget ne se basent que sur des offres disponibles)._"]
    if pc["manquants"] or acc["manquants"]:
        out.append("_Sans offre disponible actuellement : " + ", ".join(pc["manquants"] + acc["manquants"]) + "._")
    if totals_all and (totals_all["pc"]["manquants"] or totals_all["acc"]["manquants"]):
        out.append("_Sans aucun prix connu (non compris même dans « tous articles ») : "
                   + ", ".join(totals_all["pc"]["manquants"] + totals_all["acc"]["manquants"]) + "._")
    for cat, title in (("pc", "PC – composants"), ("acc", "Accessoires")):
        out += ["", f"## {title}", "",
                "| Composant | Meilleur prix HTVA | Cible / plafond | Écart | Score | Où | Fraîcheur |",
                "|---|---|---|---|---|---|---|"]
        for p in products:
            if p["cat"] != cat:
                continue
            b, st = bests.get(p["id"]), stats.get(p["id"], {})
            if not b:
                ba = (bests_all or {}).get(p["id"])
                if ba:
                    va = ba["htva"] * p.get("qty", 1)
                    out.append(f"| ⚪ {p['label']} | {e(va)} (rupture) | {e(p['cible'])} / {e(p['plafond'])} | "
                               f"{e(va - p['cible'])} | | [{ba['marchand']}]({ba['url']}) | en rupture {fmt_local(ba.get('derniere_verif'))} |")
                else:
                    out.append(f"| {p['label']} | – | {e(p['cible'])} / {e(p['plafond'])} | | | "
                               f"{p.get('remarque', 'aucune offre')} | aucun prix |")
                continue
            v = b["htva"] * p.get("qty", 1)
            mark = "🟢" if v <= p["cible"] else ("🟡" if v <= p["plafond"] else "🔴")
            score = f"{st.get('score')}" + ("" if st.get("historique_suffisant") else "*")
            out.append(f"| {mark} {p['label']}{' ×' + str(p['qty']) if p.get('qty', 1) > 1 else ''} | **{e(v)}** | "
                       f"{e(p['cible'])} / {e(p['plafond'])} | {e(st.get('ecart_eur'))} ({notifier.pct(st.get('ecart_pct'))}) | "
                       f"{score} | [{b['marchand']}]({b['url']}) | {b['fraicheur']} {fmt_local(b.get('derniere_verif'))} |")
        for i, bloc in enumerate(blocs):
            if bloc.get("cat", "pc") != cat:
                continue
            c = bloc.get("choisi")
            ca = ((totals_all or {}).get("blocs") or [{}] * len(blocs))[i].get("choisi")
            if c:
                out.append(f"\n**{bloc['label']}** : scénario le moins cher = {c['label']} → **{e(c['cout'])} HTVA** "
                           f"(cible {e(bloc['cible'])}, plafond {e(bloc['plafond'])})")
            elif ca:
                out.append(f"\n**{bloc['label']}** : rien de complet en stock ; ruptures comprises, scénario le moins cher = "
                           f"{ca['label']} → {e(ca['cout'])} HTVA (cible {e(bloc['cible'])}, plafond {e(bloc['plafond'])})")
    out += ["", "## Sources (dernière exécution)", "", "| Marchand | Résultats |", "|---|---|"]
    for m, c in sorted(etat.get("sources_derniere_execution", {}).items()):
        out.append(f"| {m} | " + ", ".join(f"{k} : {v}" for k, v in sorted(c.items())) + " |")
    out += ["", "---",
            "🟢 sous la cible · 🟡 entre cible et plafond · 🔴 au-dessus du plafond · ⚪ en rupture (dernier prix connu) · "
            "score* = historique encore insuffisant (score basé sur la cible seulement).",
            "Le score d'opportunité est un indicateur relatif (0–100), pas une probabilité. "
            "« vérifié » = lu lors de la dernière vérification ; « ancien » = dernier prix connu (moins de 24 h), "
            "source momentanément inaccessible. Aucun prix n'est inventé."]
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    sys.exit(main())
