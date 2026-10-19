#!/usr/bin/env python3
"""
build_site.py — collecte tous les véhicules électriques (VPauto, Alcopa, Interenchères)
et écrit docs/lots.json pour le site statique (docs/index.html).

    python build_site.py                       # les 3 sites
    python build_site.py --sites vpauto,alcopa
    python build_site.py --debug               # enregistre les pages brutes dans ./debug/
"""
import argparse, collections, datetime as dt, importlib.util, json, pathlib

RACINE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scraper", RACINE / "scraper.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)


def historiser(lots, precedents, aujourdhui):
    """Historique : première apparition d'un lot et variation de son prix depuis le run précédent.
    - premiere_vue : date où le lot a été vu pour la première fois (None = déjà présent avant le suivi)
    - prix_precedent / prix_change_le : dernier changement de prix constaté"""
    avant = {l["id"]: l for l in precedents}
    suivi = any("premiere_vue" in l for l in precedents)      # 1er run avec historique : l'existant n'est pas « nouveau »
    for l in lots:
        p = avant.get(l["id"])
        if p is None:
            l["premiere_vue"] = aujourdhui if (suivi or not precedents) else None
            continue
        l["premiere_vue"] = p.get("premiere_vue")
        l["prix_precedent"], l["prix_change_le"] = p.get("prix_precedent"), p.get("prix_change_le")
        if p.get("prix") and l.get("prix") and p["prix"] != l["prix"]:
            l["prix_precedent"], l["prix_change_le"] = p["prix"], aujourdhui
        if l["prix_precedent"] is None:
            l.pop("prix_precedent"); l.pop("prix_change_le")


def archiver(lots, precedents, archive, aujourdhui):
    """Garde la trace des ventes passées dans docs/historique.json ({id: lot}).
    Un lot y entre quand sa date de vente est atteinte, ou quand il disparaît du catalogue ; on y conserve
    la dernière valeur vue (« prix » = dernière enchère observée) ; le prix d'adjudication relevé par resultats.py
    (vendu, prix_adjuge, adjuge_le) est conservé."""
    presents = {l["id"] for l in lots}
    for l in lots:
        if l.get("date_vente") and l["date_vente"] <= aujourdhui and not l.get("perime"):
            resultat = {k: archive[l["id"]][k] for k in ("vendu", "prix_adjuge", "adjuge_le") if k in archive.get(l["id"], {})}
            archive[l["id"]] = {**l, **resultat, "derniere_vue": aujourdhui}      # resultats.py ajoute le prix adjugé
    for p in precedents:
        if p["id"] not in presents and p["id"] not in archive:
            archive[p["id"]] = {**p, "derniere_vue": aujourdhui, "retire": True}
    return archive


# Budget total visé, frais de vente compris (+ carte grise si CARTE_GRISE > 0). À ajuster.
BUDGET = 18000
CARTE_GRISE = 0


def prix_max_conseille(lot):
    """Enchère maximale (arrondie à 100 €) qui garde le coût frais compris sous BUDGET. Barème du site."""
    site = lot.get("site")
    if site not in S.FRAIS_SITE:
        return None
    def cout(p):
        frais, _, _ = S.calcul_frais(site, p, frais_inclus=bool(lot.get("frais_inclus")))
        return p + frais + CARTE_GRISE
    bas, haut = 0, BUDGET // 100
    if cout(100) > BUDGET:
        return None
    while bas < haut:                       # dichotomie : le coût est croissant avec le prix
        m = (bas + haut + 1) // 2
        if cout(m * 100) <= BUDGET:
            bas = m
        else:
            haut = m - 1
    return bas * 100


def score(lot):
    """Note 0-100 : coût vs budget (50), kilométrage (15), année (10), SoH (15), batterie (10). Inconnu = note basse."""
    clamp = lambda x: max(0.0, min(1.0, x))
    cout = lot.get("cout_estime")
    s = 10 if not cout else 50 * clamp((BUDGET * 1.1 - cout) / (BUDGET * 0.5))    # 50 sous 60 % du budget, 0 à 110 %
    s += 4 if lot.get("km") is None else 15 * clamp((120000 - lot["km"]) / 100000)
    s += 3 if lot.get("annee") is None else 10 * clamp((lot["annee"] - 2018) / 8)
    s += 3 if lot.get("soh") is None else 15 * clamp((lot["soh"] - 80) / 18)
    s += 3 if lot.get("kwh") is None else 10 * clamp((lot["kwh"] - 40) / 50)
    return round(s)


def enrichir(lots):
    for l in lots:
        l["prix_max_conseille"] = prix_max_conseille(l)
        l["score"] = score(l)


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--sites", default=",".join(S.SITES))
    a.add_argument("--debug", action="store_true")
    a.add_argument("--sortie", default=str(RACINE / "docs" / "lots.json"))
    a = a.parse_args()
    sortie = pathlib.Path(a.sortie)

    precedents = []
    if sortie.exists():
        try:
            precedents = json.loads(sortie.read_text(encoding="utf-8")).get("lots", [])
        except ValueError:
            pass

    lots, comptes = [], {}
    for nom in [x.strip() for x in a.sites.split(",") if x.strip()]:
        if nom not in S.SITES:
            print(f"Site inconnu : {nom}"); continue
        print(f"[{nom}]")
        trouves = S.collecter(nom, a.debug)
        if not trouves:
            # Un site à 0 lot (Cloudflare, pas d'écran, panne) ne doit pas effacer ses lots de la veille.
            print(f"  ! {nom} : 0 lot collecté, on conserve les lots du précédent run")
            trouves = [{**l, "perime": True} for l in precedents if l.get("site") == nom]
        lots += trouves
        comptes[nom] = len(trouves)

    # sites non demandés : on garde leurs lots tels quels
    demandes = set(comptes)
    lots += [l for l in precedents if l.get("site") not in demandes and l.get("site") in S.SITES]

    historiser(lots, precedents, dt.date.today().isoformat())
    enrichir(lots)
    comptes = dict(collections.Counter(l["site"] for l in lots))      # total réel, même après un run partiel

    charge = {
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sites": comptes,
        "lots": lots,
    }
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(charge, ensure_ascii=False, indent=1), encoding="utf-8")

    fichier_histo = sortie.with_name("historique.json")
    archive = {}
    if fichier_histo.exists():
        try:
            archive = json.loads(fichier_histo.read_text(encoding="utf-8")).get("lots", {})
        except ValueError:
            pass
    archive = archiver(lots, precedents, archive, dt.date.today().isoformat())
    fichier_histo.write_text(json.dumps({"genere_le": charge["genere_le"], "lots": archive},
                                        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(lots)} lots → {sortie}  {comptes}")


if __name__ == "__main__":
    main()
