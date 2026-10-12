#!/usr/bin/env python3
"""
build_site.py — collecte tous les véhicules électriques (VPauto, Alcopa, Interenchères)
et écrit docs/lots.json pour le site statique (docs/index.html).

    python build_site.py                       # les 3 sites
    python build_site.py --sites vpauto,alcopa
    python build_site.py --debug               # enregistre les pages brutes dans ./debug/
"""
import argparse, datetime as dt, importlib.util, json, pathlib

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

    charge = {
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "sites": comptes,
        "lots": lots,
    }
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_text(json.dumps(charge, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n{len(lots)} lots → {sortie}  {comptes}")


if __name__ == "__main__":
    main()
