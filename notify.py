#!/usr/bin/env python3
"""
notify.py — alertes Telegram après chaque collecte (appelé par publish.sh).

Quatre alertes, chacune envoyée une seule fois :
  1. nouveau lot dont le score atteint SEUIL_SCORE
  2. rappel de vente d'un favori à J-3, J-1 et le jour même
  3. variation du prix d'un favori de plus de SEUIL_PRIX €
  4. disparition d'un favori du catalogue avant sa vente

Configuration : telegram.json {"token": "...", "chat_id": "..."} (ou variables TELEGRAM_TOKEN / TELEGRAM_CHAT_ID).
Favoris : favoris.json à la racine (bouton « Exporter mes favoris » du site).
État : .notifications.json (ignoré par git). Le premier lancement l'initialise sans rien envoyer.

    python notify.py             # envoi réel
    python notify.py --dry-run   # affiche les messages, ne touche pas à l'état
"""
import argparse, datetime as dt, html, json, os, pathlib, sys

import requests

RACINE = pathlib.Path(__file__).resolve().parent
LOTS, FAVORIS, ETAT, CONFIG = (RACINE / "docs" / "lots.json", RACINE / "favoris.json",
                               RACINE / ".notifications.json", RACINE / "telegram.json")
SEUIL_SCORE = 55
SEUIL_PRIX = 200
RAPPELS = (3, 1, 0)           # jours avant la vente
MAX_NOUVEAUX = 8              # au-delà, un seul message récapitulatif
# Mêmes modèles écartés que dans docs/index.html (exclusion()).
EXCLUS = ("c-zero", "2008", "smart", "dacia", "fiat", "zoe", "corsa", "mokka")


def charger_json(chemin, defaut):
    try:
        return json.loads(chemin.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defaut


def config_telegram():
    c = charger_json(CONFIG, {})
    token = os.environ.get("TELEGRAM_TOKEN") or c.get("token")
    chat = os.environ.get("TELEGRAM_CHAT_ID") or c.get("chat_id")
    return (token, str(chat)) if token and chat else None


def ids_favoris():
    """Accepte l'export du site ({"favoris": [{"id": ...}]}) ou une simple liste d'identifiants."""
    f = charger_json(FAVORIS, {})
    items = f.get("favoris", []) if isinstance(f, dict) else f
    return [x["id"] if isinstance(x, dict) else x for x in items if x]


def exclu(lot):
    t = (lot.get("texte") or "").lower()
    return any(m in t for m in EXCLUS)


def euros(x):
    return "?" if x is None else f"{x:,.0f}".replace(",", " ") + " €"


def lien(lot):
    return f'<a href="{html.escape(lot["url"], quote=True)}">{html.escape(lot.get("titre") or "?")}</a>'


def quand(lot):
    d = lot.get("date_vente")
    return f"vente le {dt.date.fromisoformat(d):%d/%m}" + (f" à {lot['heure_vente']}" if lot.get("heure_vente") else "") if d else "date de vente inconnue"


def ligne_lot(lot):
    prix = lot.get("prix") or lot.get("mise_a_prix")
    return (f"{lien(lot)}\n{lot.get('annee') or '?'} · {euros(lot.get('km')).replace(' €', '')} km · {euros(prix)}"
            f" · {html.escape(lot.get('ville') or lot['site'])}\n{quand(lot)}"
            + (f" · prix max conseillé {euros(lot['prix_max_conseille'])}" if lot.get("prix_max_conseille") else ""))


def jours_avant(lot, aujourdhui):
    return (dt.date.fromisoformat(lot["date_vente"]) - aujourdhui).days if lot.get("date_vente") else None


def calculer(lots, favoris, etat, aujourdhui):
    """Renvoie [(clés_à_marquer, message)] et met à jour `etat` pour ce qui n'est pas une alerte."""
    envoyees = etat.setdefault("envoyees", {})
    par_id = {l["id"]: l for l in lots}
    sortie = []

    # 1. nouveaux lots
    nouveaux = [l for l in lots if l["id"] not in etat["vus"] and not exclu(l)
                and (l.get("score") or 0) >= SEUIL_SCORE and (jours_avant(l, aujourdhui) or 0) >= 0]
    nouveaux.sort(key=lambda l: -l["score"])
    for l in nouveaux[:MAX_NOUVEAUX]:
        sortie.append(([f"nouveau:{l['id']}"], f"🆕 <b>Nouveau lot</b> — score {l['score']}\n{ligne_lot(l)}"))
    if len(nouveaux) > MAX_NOUVEAUX:
        sortie.append(([], f"🆕 … et {len(nouveaux) - MAX_NOUVEAUX} autres nouveaux lots au-dessus du score {SEUIL_SCORE}."))
    etat["vus"] = sorted(set(etat["vus"]) | set(par_id))

    for ident in favoris:
        l = par_id.get(ident)
        if l is None:
            # 4. disparition : seulement si on l'avait vu, et si la vente n'était pas déjà passée
            vu = etat["favoris_vus"].get(ident)
            cle = f"disparu:{ident}"
            if vu and cle not in envoyees:
                d = vu.get("date_vente")
                if not d or dt.date.fromisoformat(d) >= aujourdhui:
                    sortie.append(([cle], f"❌ <b>Favori retiré du catalogue</b>\n"
                                          f"{html.escape(vu.get('titre') or ident)} ({quand(vu)}) n'est plus listé."))
                else:
                    envoyees[cle] = True      # vente passée : disparition normale, sans alerte
            continue
        etat["favoris_vus"][ident] = {k: l.get(k) for k in ("titre", "url", "date_vente", "heure_vente")}

        # 2. rappels : un seul message, au seuil le plus proche atteint ; les seuils plus lointains sont soldés avec
        j = jours_avant(l, aujourdhui)
        if j is not None and j >= 0:
            niveau = min(t for t in RAPPELS if j <= t) if j <= max(RAPPELS) else None
            if niveau is not None:
                cles = [f"rappel:{ident}:{l['date_vente']}:{t}" for t in RAPPELS if t >= niveau]
                if cles[-1] not in envoyees:
                    quand_txt = "AUJOURD'HUI" if j == 0 else "demain" if j == 1 else f"dans {j} jours"
                    sortie.append((cles, f"⏰ <b>Vente {quand_txt}</b> (favori)\n{ligne_lot(l)}"))

        # 3. variation de prix : référence = dernier prix notifié (ou vu au premier passage)
        prix = l.get("prix")
        ref = etat["prix"].get(ident)
        if prix and ref is None:
            etat["prix"][ident] = prix
        elif prix and abs(prix - ref) > SEUIL_PRIX:
            sens = "📉 baisse" if prix < ref else "📈 hausse"
            sortie.append(([f"prix:{ident}:{prix}"],
                           f"{sens} <b>{euros(abs(prix - ref))}</b> sur un favori : {euros(ref)} → {euros(prix)}\n{ligne_lot(l)}"))
            etat["prix"][ident] = prix
    return sortie


def envoyer(cfg, texte):
    token, chat = cfg
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                      json={"chat_id": chat, "text": texte, "parse_mode": "HTML", "disable_web_page_preview": True})
    if not r.ok:
        raise RuntimeError(f"Telegram {r.status_code} : {r.text[:200]}")


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--dry-run", action="store_true")
    a = a.parse_args()

    cfg = config_telegram()
    if not cfg and not a.dry_run:
        print("notify : bot non configuré (telegram.json absent), alertes ignorées")
        return 0
    donnees = charger_json(LOTS, None)
    if not donnees or not donnees.get("lots"):
        print("notify : docs/lots.json introuvable ou vide")
        return 0
    lots = donnees["lots"]
    if any("score" not in l for l in lots):           # lots.json produit avant l'ajout du score
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_site", RACINE / "build_site.py")
        b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
        b.enrichir(lots)

    favoris = ids_favoris()
    premier = not ETAT.exists()
    etat = charger_json(ETAT, {})
    for cle, defaut in (("vus", []), ("envoyees", {}), ("prix", {}), ("favoris_vus", {})):
        etat.setdefault(cle, defaut)
    aujourdhui = dt.date.today()

    messages = calculer(lots, favoris, etat, aujourdhui)
    if premier:
        print(f"notify : état initialisé ({len(lots)} lots connus), aucune alerte envoyée")
        messages = []                                  # on garde vus / prix / favoris_vus, on n'envoie rien
    elif not messages:
        print("notify : rien de neuf")

    for cles, texte in messages:
        if a.dry_run:
            print("---\n" + texte)
            continue
        try:
            envoyer(cfg, texte)
        except Exception as e:                        # clé non marquée : l'alerte repartira au prochain passage
            print(f"notify : échec d'envoi ({e})", file=sys.stderr)
            for c in cles:
                if c.startswith("nouveau:"):          # sera re-détecté comme nouveau au prochain passage
                    etat["vus"].remove(c.split(":", 1)[1])
            continue
        for c in cles:
            etat["envoyees"][c] = True
        print(f"notify : envoyé ({texte.splitlines()[0]})")
    if not a.dry_run:
        ETAT.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
