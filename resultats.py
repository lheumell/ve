#!/usr/bin/env python3
"""
resultats.py — relève le prix d'adjudication des lots Interenchères vendus aujourd'hui.

La page d'un lot affiche « Adjugé à X € » le jour de la vente (état Nuxt : saleItem.pricing.auctioned) ;
quelques jours plus tard elle renvoie 410. À lancer en fin de journée de vente (cron 16h45).

Lit docs/historique.json, ouvre chaque lot interencheres dont date_vente == aujourd'hui et qui n'a pas
encore de résultat, puis écrit dans le lot : vendu (bool), prix_adjuge (€, hors frais), adjuge_le.

    python resultats.py                      # lots d'aujourd'hui
    python resultats.py --date 2026-10-19    # une autre journée de vente
    python resultats.py --dry-run            # affiche sans écrire
"""
import argparse, datetime as dt, json, pathlib, time

RACINE = pathlib.Path(__file__).resolve().parent
HISTORIQUE = RACINE / "docs" / "historique.json"
DELAI = 2.5   # secondes entre deux pages, par politesse


def a_relever(lots, jour):
    return [l for l in lots.values()
            if l.get("site") == "interencheres" and l.get("date_vente") == jour and "adjuge_le" not in l]


def resultat(pg, url):
    """(vendu, prix) lus sur la page du lot ; (None, None) si la page n'est pas exploitable (410, Cloudflare…)."""
    r = pg.goto(url, wait_until="domcontentloaded", timeout=60000)
    if r is None or r.status != 200:
        return None, None
    pg.wait_for_function("() => window.__NUXT__ && window.__NUXT__.state && window.__NUXT__.state.saleItem", timeout=45000)
    pg.wait_for_timeout(1500)
    adj = pg.evaluate("() => (window.__NUXT__.state.saleItem.saleItem.pricing || {}).auctioned || null")
    if adj is None:
        return None, None                         # pas encore adjugé
    return bool(adj.get("sold")), adj.get("price") if adj.get("sold") else None


def main():
    a = argparse.ArgumentParser()
    a.add_argument("--date", default=dt.date.today().isoformat())
    a.add_argument("--dry-run", action="store_true")
    a = a.parse_args()

    donnees = json.loads(HISTORIQUE.read_text(encoding="utf-8"))
    cibles = a_relever(donnees["lots"], a.date)
    print(f"{len(cibles)} lot(s) Interenchères à relever pour le {a.date}")
    if not cibles:
        return

    from playwright.sync_api import sync_playwright
    vendus = sans_resultat = 0
    with sync_playwright() as p:
        try:
            nav = p.chromium.launch(headless=False)       # Cloudflare exige un navigateur visible
        except Exception as e:
            print(f"  ! navigateur visible indisponible : {str(e)[:120]}"); return
        pg = nav.new_page(locale="fr-FR")
        for l in cibles:
            try:
                vendu, prix = resultat(pg, l["url"])
            except Exception as e:
                print(f"  ! {l['id']} : {str(e)[:80]}"); sans_resultat += 1; continue
            if vendu is None:
                print(f"  ? {l['id']} {l['titre'][:40]} : pas de résultat"); sans_resultat += 1
            else:
                l["vendu"], l["prix_adjuge"], l["adjuge_le"] = vendu, prix, a.date
                vendus += vendu
                print(f"  {'✓' if vendu else '✗'} {l['titre'][:40]} : {prix if vendu else 'non vendu'}"
                      + (f" (estimation haute {l['prix']})" if l.get("prix") else ""))
            time.sleep(DELAI)
        nav.close()

    print(f"{vendus} vendu(s), {len(cibles) - vendus - sans_resultat} non vendu(s), {sans_resultat} sans résultat")
    if not a.dry_run:
        HISTORIQUE.write_text(json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
