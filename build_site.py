#!/usr/bin/env python3
"""
build_site.py — collecte les lots VE aux enchères et écrit docs/lots.json
pour le site statique (docs/index.html), publié sur GitHub Pages.

    python build_site.py                          # VPauto
    python build_site.py --sites vpauto,alcopa,mercier
    python build_site.py --fichier page.html --site alcopa   # page enregistrée (Ctrl+S)

Le moteur (filtres, scoring, extraction) est dans scraper.py : ce fichier
ajoute les modèles/sites manquants, calcule le prix max conseillé et sérialise.
"""
import argparse, datetime as dt, importlib.util, json, pathlib, sys

RACINE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scraper", RACINE / "scraper.py")
S = importlib.util.module_from_spec(spec)
spec.loader.exec_module(S)

# --- modèles ajoutés après coup -------------------------------------------
S.MODELES.append(dict(nom="Nissan Ariya", motif=r"\bariya\b", garantie=(8, 160000),
                      kwh=63, dc=130, exclure=r"e-?4orce"))
S.MARQUEURS_ELEC += r"|ariya"

# --- sites ajoutés ---------------------------------------------------------
S.BASES["mercier"] = "https://www.mercier.com/"
S.CONFIG["sites"]["mercier"] = {
    "mode": "browser",
    # Colle ici l'URL d'une vente Mercier (page "vente-vehicules-...") à surveiller
    "urls": [],
    "lien_motif": r"mercier\.com/auto/vente/[^/]+/[^/]+$",
    "frais_pct": 0.1428,     # barème légal fixe TTC
    "frais_min": 360,
    "frais_fixes": 160,      # dossier VE (certificat SoH) ; +238 € si achat en LIVE
}
S.CONFIG["sites"]["alcopa"]["frais_fixes"] = 0
S.CONFIG["sites"]["interencheres"]["frais_fixes"] = 0
S.CONFIG["sites"]["vpauto"]["frais_fixes"] = 0
CARTE_GRISE = 250            # Hauts-de-France, VE 4-11 CV, abattement 50 %


def prix_max_conseille(site_cfg, budget):
    """Enchère maximale pour rester sous le budget, frais de vente + carte grise compris."""
    pct, mini = site_cfg.get("frais_pct", 0), site_cfg.get("frais_min", 0)
    fixes = site_cfg.get("frais_fixes", 0)
    dispo = budget - CARTE_GRISE - fixes
    if pct == 0:                       # prix déjà "frais inclus" (VPauto)
        return max(0, round(dispo))
    b = dispo / (1 + pct)              # cas frais proportionnels
    if b * pct < mini:                 # cas forfait minimum
        b = dispo - mini
    return max(0, round(b / 50) * 50)  # arrondi à 50 €


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--sites", default="vpauto")
    a.add_argument("--fichier"); a.add_argument("--site", default="alcopa")
    a.add_argument("--sans-detail", action="store_true")
    a.add_argument("--visible", action="store_true")
    a.add_argument("--debug", action="store_true")
    a.add_argument("--sortie", default=str(RACINE / "docs" / "lots.json"))
    a = a.parse_args()

    lots = []
    if a.fichier:
        import requests  # noqa: F401  (cohérence d'import avec le moteur)
        contenu = pathlib.Path(a.fichier).read_text(encoding="utf-8", errors="ignore")
        cfg = S.CONFIG["sites"].get(a.site, {"lien_motif": r".+"})
        lots = (S.cartes_vpauto(contenu, S.BASES["vpauto"]) if a.site == "vpauto"
                else S.cartes_generiques(contenu, S.BASES.get(a.site, f"https://{a.site}/"),
                                         a.site, cfg.get("lien_motif", r".+")))
        print(f"{a.fichier} → {len(lots)} lots")
    else:
        import requests
        sess = requests.Session()
        for nom in [x.strip() for x in a.sites.split(",") if x.strip()]:
            cfg = S.CONFIG["sites"].get(nom)
            if not cfg:
                print(f"Site inconnu : {nom}"); continue
            print(f"[{nom}]")
            lots += (S.collecter_vpauto(cfg, sess, not a.sans_detail, a.debug)
                     if cfg["mode"] == "http"
                     else S.collecter_navigateur(nom, cfg, a.visible, a.debug))

    # Un site qui renvoie 0 lot (Cloudflare, pas d'écran, panne) ne doit pas effacer ses lots de la veille.
    precedents = []
    sortie_p = pathlib.Path(a.sortie)
    if sortie_p.exists() and not a.fichier:
        try:
            precedents = json.loads(sortie_p.read_text(encoding="utf-8")).get("lots", [])
        except ValueError:
            pass
    demandes = [x.strip() for x in a.sites.split(",") if x.strip()] if not a.fichier else []
    vides = [n for n in demandes if n in S.CONFIG["sites"] and not any(l["site"] == n for l in lots)]
    for n in vides:
        print(f"  ! {n} : 0 lot collecté, on conserve les lots du précédent run")

    evalues = []
    for l in lots:
        cfg = S.CONFIG["sites"].get(l["site"], {"frais_pct": 0.1428, "frais_min": 360})
        e = S.evaluer(l, cfg)
        e.pop("detail", None); e.pop("texte", None)
        e["prix_max_conseille"] = prix_max_conseille(cfg, S.CONFIG["budget_total_max"])
        e["mensualite_au_max"] = round(S.mensualite_totale(S.CONFIG["budget_total_max"]))
        evalues.append(e)

    evalues += [{**e, "perime": True} for e in precedents if e.get("site") in vides]
    sortie = pathlib.Path(a.sortie); sortie.parent.mkdir(parents=True, exist_ok=True)
    charge = {
        "genere_le": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "config": {k: S.CONFIG[k] for k in
                   ("budget_total_max", "km_max", "annee_min", "km_par_an",
                    "garantie_restante_min_ans", "soh_min")},
        "financement": S.CONFIG["financement"],
        "modeles": [m["nom"] for m in S.MODELES],
        "lots": sorted(evalues, key=lambda e: (-e["retenu"], -e["score"])),
    }
    sortie.write_text(json.dumps(charge, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    retenus = [e for e in evalues if e["retenu"]]
    print(f"\n{len(lots)} lots · {sum(1 for e in evalues if e.get('modele'))} de modèles ciblés · "
          f"{len(retenus)} retenus → {sortie}")
    for e in sorted(retenus, key=lambda e: -e["score"])[:10]:
        print(f"  {e['score']:>5}  {e['modele']:<30} {e.get('annee')}  {e.get('km')} km  "
              f"{e.get('prix_final')} €  (max conseillé {e['prix_max_conseille']} €)")


if __name__ == "__main__":
    main()
