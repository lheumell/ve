#!/usr/bin/env python3
"""
ve_encheres.py — Veille des voitures électriques aux enchères (VPauto, Alcopa, Interenchères)

Récupère les lots, garde ceux qui collent à tes critères (modèle fiable, batterie >= 58 kWh,
garantie batterie restante, km, budget frais compris), les note et génère un rapport HTML.

Installation :
    pip install requests beautifulsoup4
    # Pour Alcopa / Interenchères (sites protégés, rendu JS) :
    pip install playwright && playwright install chromium

Utilisation :
    python ve_encheres.py                      # VPauto seul (fonctionne tel quel)
    python ve_encheres.py --sites vpauto,alcopa
    python ve_encheres.py --visible            # navigateur visible si un site bloque le mode headless
    python ve_encheres.py --fichier page.html --site alcopa   # analyse une page enregistrée (Ctrl+S)
    python ve_encheres.py --debug              # enregistre les pages brutes dans ./debug/

Cron quotidien (Linux/macOS) :  0 8 * * * cd /chemin && python3 ve_encheres.py
"""
import argparse, datetime as dt, json, math, pathlib, re, sys, time, unicodedata, html as H
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ============================================================================
# 1. TES CRITÈRES — modifie ici
# ============================================================================
CONFIG = {
    "budget_total_max": 18000,       # prix final frais compris (€)
    "km_max": 120000,
    "annee_min": 2019,
    "km_par_an": 20000,              # ton rythme, pour calculer la garantie restante
    "garantie_restante_min_ans": 2.5,
    "soh_min": 90,                   # appliqué seulement si le SoH est affiché
    "facteur_mise_a_prix": 1.40,     # si seule la mise à prix est connue : prix final estimé = MAP x facteur
    "maison": (50.750, 2.252),       # Saint-Omer
    "delai_requetes_s": 2.5,         # politesse envers les sites
    "financement": {"apport": 2500, "tranche1": 10000, "taux1": 0.01, "taux2": 0.035, "mois": 60},
    "sites": {
        "vpauto": {
            "mode": "http",
            "urls": ["https://www.vpauto.fr/vehicule/liste?energy=electrique"],
            "pages_max": 6,
            "frais_pct": 0.0,        # VPauto affiche des prix « frais de vente inclus »
            "frais_min": 0,
        },
        "alcopa": {
            "mode": "browser",
            # Colle ici l'URL de ta recherche Alcopa filtrée sur « Électrique » (copiée depuis ton navigateur)
            "urls": ["https://www.alcopa-auction.fr/recherche?energy_families%5B%5D=EL&category_view=VP"],
            "pages_max": 8,          # pagination &page=N, arrêt dès qu'une page n'apporte rien de nouveau
            "lien_motif": r"alcopa-auction\.fr/.+",
            "frais_pct": 0.144,      # 12 % HT = 14,4 % TTC (salle / live). Les ventes web affichent souvent un prix TTC final.
            "frais_min": 360,
        },
        "interencheres": {
            "mode": "browser",
            # Colle ici l'URL de ta recherche Interenchères (catégorie véhicules, filtrée électrique)
            "urls": ["https://www.interencheres.com/vehicules/vehicules-electriques-et-hydrides"],
            "pages_max": 60,
            "lien_motif": r"interencheres\.com/vehicules/.+/lot-\d+\.html",
            "frais_pct": 0.144,      # variable selon le commissaire-priseur : vérifie la fiche
            "frais_min": 300,
        },
    },
}

# ============================================================================
# 2. MODÈLES RETENUS — garantie batterie (ans, km), batterie utile, recharge DC
# ============================================================================
MODELES = [
    dict(nom="Kia e-Niro / Niro EV", motif=r"e[\s-]?niro|niro\s*ev", garantie=(7, 150000), kwh=64, dc=77,
         exclure=r"\b39\s*kwh|\b136\s*ch|\b100\s*kw\b"),
    dict(nom="Hyundai Kona électrique", motif=r"\bkona\b", garantie=(8, 160000), kwh=64, dc=77,
         exclure=r"\b39\s*kwh|\b48\s*kwh|\b136\s*ch"),
    dict(nom="VW ID.3", motif=r"\bid\.?\s?3\b", garantie=(8, 160000), kwh=58, dc=120,
         exclure=r"\bpure\b|\b45\s*kwh|\b48\s*kwh|\b52\s*kwh"),
    dict(nom="VW ID.4/ID.5 · Skoda Enyaq · Cupra Born", motif=r"\bid\.?\s?[45]\b|enyaq|\bborn\b",
         garantie=(8, 160000), kwh=77, dc=125, exclure=r"\bpure\b|\b52\s*kwh|\b55\s*kwh|\biv\s*50\b|\b50\s*iv\b"),
    dict(nom="MG4", motif=r"\bmg\s?4\b", garantie=(7, 150000), kwh=61, dc=135,
         exclure=r"standard|\b51\s*kwh"),
    dict(nom="Tesla Model 3", motif=r"model\s?3", garantie=(8, 160000), kwh=57, dc=170, exclure=None),
    dict(nom="Renault Mégane E-Tech électrique", motif=r"megane.*(?:ev\s?60|e-?tech\s*(?:100\s*%\s*)?electri)",
         garantie=(8, 160000), kwh=60, dc=130, exclure=r"ev\s?40|\b40\s*kwh"),
    dict(nom="Hyundai Ioniq 5", motif=r"ioniq\s?5", garantie=(8, 160000), kwh=58, dc=220, exclure=None),
    dict(nom="Kia EV6", motif=r"\bev\s?6\b", garantie=(7, 150000), kwh=58, dc=230, exclure=None),
]
MARQUEURS_ELEC = r"electri|kwh|\bev\s?\d*\b|e[\s-]?niro|niro ev|\bid\.?\s?\d|model\s?[3y]|\bmg\s?4|ioniq|enyaq|\bborn\b|e-?tech"
MARQUEURS_THERMIQUE = r"hybrid|hybride|phev|\bhev\b|plug[\s-]?in|tfsi\s?e|rechargeable|\bgdi\b|\btsi\b|\btdi\b|\bdci\b|mhev|essence|diesel|gazole"
LOTS_TERMINES = r"\badjuge\b|vente terminee|\blot vendu\b"

# Coordonnées approximatives (préfectures) pour la distance
DEPTS = dict(x.split(":") for x in (
    "01:46.2,5.23 02:49.56,3.62 03:46.57,3.33 04:44.09,6.24 05:44.56,6.08 06:43.7,7.27 07:44.74,4.6 08:49.77,4.72 "
    "09:42.96,1.6 10:48.3,4.08 11:43.21,2.35 12:44.35,2.57 13:43.3,5.37 14:49.18,-0.37 15:44.93,2.44 16:45.65,0.16 "
    "17:46.16,-1.15 18:47.08,2.4 19:45.27,1.77 21:47.32,5.04 22:48.51,-2.76 23:46.17,1.87 24:45.18,0.72 25:47.24,6.02 "
    "26:44.93,4.89 27:49.02,1.15 28:48.44,1.49 29:48.0,-4.1 30:43.84,4.36 31:43.6,1.44 32:43.65,0.59 33:44.84,-0.58 "
    "34:43.61,3.88 35:48.11,-1.68 36:46.81,1.69 37:47.39,0.69 38:45.19,5.72 39:46.67,5.55 40:43.89,-0.5 41:47.59,1.33 "
    "42:45.44,4.39 43:45.04,3.88 44:47.22,-1.55 45:47.9,1.9 46:44.45,1.44 47:44.2,0.62 48:44.52,3.5 49:47.47,-0.55 "
    "50:49.12,-1.09 51:48.96,4.36 52:48.11,5.14 53:48.07,-0.77 54:48.69,6.18 55:48.77,5.16 56:47.66,-2.76 57:49.12,6.18 "
    "58:46.99,3.16 59:50.63,3.06 60:49.43,2.08 61:48.43,0.09 62:50.29,2.78 63:45.78,3.08 64:43.3,-0.37 65:43.23,0.08 "
    "66:42.7,2.9 67:48.57,7.75 68:48.08,7.36 69:45.76,4.84 70:47.62,6.15 71:46.31,4.83 72:48.0,0.2 73:45.57,5.92 "
    "74:45.9,6.13 75:48.86,2.35 76:49.44,1.1 77:48.54,2.66 78:48.8,2.13 79:46.32,-0.46 80:49.89,2.3 81:43.93,2.15 "
    "82:44.02,1.35 83:43.12,5.93 84:43.95,4.81 85:46.67,-1.43 86:46.58,0.34 87:45.83,1.26 88:48.17,6.45 89:47.8,3.57 "
    "90:47.64,6.86 91:48.63,2.44 92:48.89,2.2 93:48.91,2.44 94:48.79,2.46 95:49.04,2.08 2A:41.93,8.74 2B:42.7,9.45").split())
VILLES = {"lille": "59", "rouen": "76", "paris": "75", "lorient": "56", "caudan": "56", "nantes": "44", "lyon": "69",
          "bordeaux": "33", "marseille": "13", "miramas": "13", "rennes": "35", "toulouse": "31", "strasbourg": "67",
          "metz": "57", "nancy": "54", "reims": "51", "amiens": "80", "orleans": "45", "tours": "37", "montpellier": "34",
          "nice": "06", "grenoble": "38", "dijon": "21", "clermont": "63", "le mans": "72", "angers": "49", "caen": "14"}

BASES = {"vpauto": "https://www.vpauto.fr/", "alcopa": "https://www.alcopa-auction.fr/",
         "interencheres": "https://www.interencheres.com/"}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
SEP_NUM = r"[\s.\u00a0\u202f]"

# ============================================================================
# 3. OUTILS
# ============================================================================
def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").replace("€", " eur ")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower().strip()

def num(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None

def haversine(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))

def mensualite(capital, taux, n):
    if capital <= 0:
        return 0.0
    r = taux / 12
    return capital * r / (1 - (1 + r) ** -n)

def mensualite_totale(prix):
    f = CONFIG["financement"]
    emprunt = max(0, prix - f["apport"])
    t1 = min(f["tranche1"], emprunt)
    return mensualite(t1, f["taux1"], f["mois"]) + mensualite(emprunt - t1, f["taux2"], f["mois"])

def lire(url, session, debug=False, nom="page"):
    r = session.get(url, timeout=30, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    r.raise_for_status()
    if debug:
        pathlib.Path("debug").mkdir(exist_ok=True)
        pathlib.Path(f"debug/{nom}.html").write_text(r.text, encoding="utf-8")
    return r.text

def lire_navigateur(urls, visible=False, debug=False, prefixe="page"):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! Playwright absent : pip install playwright && playwright install chromium")
        return []
    pages = []
    with sync_playwright() as p:
        nav = p.chromium.launch(headless=not visible)
        pg = nav.new_page(locale="fr-FR", user_agent=UA)
        for i, u in enumerate(urls):
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=60000)
                try:
                    pg.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                for _ in range(10):                      # charge les résultats paresseux
                    pg.mouse.wheel(0, 4000)
                    pg.wait_for_timeout(600)
                contenu = pg.content()
                pages.append((u, contenu))
                if debug:
                    pathlib.Path("debug").mkdir(exist_ok=True)
                    pathlib.Path(f"debug/{prefixe}_{i}.html").write_text(contenu, encoding="utf-8")
            except Exception as e:
                print(f"  ! {u} : {e}")
            time.sleep(CONFIG["delai_requetes_s"])
        nav.close()
    return pages

# ============================================================================
# 4. EXTRACTION
# ============================================================================
def extraire_champs(texte):
    """Tire année, km, prix, kWh, SoH, lieu d'un texte d'annonce (liste ou fiche)."""
    t = norm(texte)
    o = {}
    m = re.search(rf"\b(20[12]\d)\s*[-–|·/]\s*(\d[\d{SEP_NUM[1:-1]}]*?)\s*km\b", t)
    if m:
        o["annee"], o["km"] = int(m.group(1)), num(m.group(2))
    else:
        t_km = re.sub(rf"\b20[12]\d\b(?=\s+\d{{1,3}}(?:{SEP_NUM}\d{{3}})*\s*km\b)", " ", t)   # « mise : 2020 199 075 km »
        m = re.search(rf"(\d{{1,3}}(?:{SEP_NUM}\d{{3}})+|\d{{4,6}})\s*km\b", t_km)
        if m:
            o["km"] = num(m.group(1))
        m = re.search(r"\b(20[12]\d)\b", t)
        if m:
            o["annee"] = int(m.group(1))
    prix = rf"(\d{{1,3}}(?:{SEP_NUM}\d{{3}})+|\d{{3,6}})"
    m = re.search(rf"estimation\s*:?\s*{prix}\s*(?:€|eur)?\s*(?:-|–|a)\s*{prix}\s*(?:€|eur)", t)
    if m:
        o["prix"], o["prix_type"] = num(m.group(2)), "estimation haute"
    else:
        m = re.search(rf"mise\s*a\s*prix\s*:?\s*{prix}\s*(?:€|eur)", t)
        if m:
            o["mise_a_prix"] = num(m.group(1))
        m2 = re.search(rf"(?:prix|enchere actuelle|offre actuelle|prix de reserve)\s*:?\s*{prix}\s*(?:€|eur)", t)
        if m2 and not m:
            o["prix"], o["prix_type"] = num(m2.group(1)), "prix affiché"
        elif not m:
            for c in re.findall(rf"{prix}\s*(?:€|eur)", t):
                v = num(c)
                if v and 1500 <= v <= 150000:
                    o["prix"], o["prix_type"] = v, "prix affiché"
                    break
    o["frais_inclus"] = bool(re.search(r"frais\s*de\s*vente\s*inclus|frais\s*inclus|ttc.{0,20}aucun frais", t))
    m = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*kwh(?!\s*/)", t)
    if m:
        o["kwh"] = float(m.group(1).replace(",", "."))
    m = re.search(r"(?:\bsoh\b|etat de sante|sante (?:de la )?batterie)[^\d%]{0,40}(\d{2,3}(?:[.,]\d+)?)\s*%", t)
    if m:
        v = float(m.group(1).replace(",", "."))
        if 50 <= v <= 100:
            o["soh"] = v
    m = re.search(r"dept\s*:?\s*(\d{2}|2a|2b)\b", t)
    if m:
        o["dept"] = m.group(1).upper()
    else:
        for v, d in VILLES.items():
            if re.search(rf"\b{v}\b", t):
                o["dept"], o["ville"] = d, v.title()
                break
    m = re.search(r"(?:vente|enchere[s]?)\s*(?:le|du)?\s*(?:\w+\.?\s)?(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", t)
    if m:
        a = int(m.group(3)); a = a + 2000 if a < 100 else a
        try:
            o["date_vente"] = dt.date(a, int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass
    return o

def cartes_vpauto(html_txt, base):
    soup = BeautifulSoup(html_txt, "html.parser")
    lots = {}
    for a in soup.find_all("a", href=re.compile(r"/vehicule/[0-9A-Za-z]{6,}/")):
        url = urljoin(base, a["href"].split("?")[0])
        ident = re.search(r"/vehicule/([0-9A-Za-z]+)/", url).group(1)
        texte = a.get_text(" ", strip=True)
        if len(texte) < 15 or "km" not in texte.lower():
            continue
        img = a.find("img")
        src = img and (img.get("data-src") or img.get("src"))
        if ident not in lots or len(texte) > len(lots[ident]["texte"]):
            lots[ident] = {"id": f"vpauto:{ident}", "site": "vpauto", "url": url, "texte": texte,
                           "titre": texte[:90], "image": urljoin(base, src) if src else None}
    return list(lots.values())

def cartes_generiques(html_txt, base, site, lien_motif):
    """Extraction générique : liens d'annonces + texte de la carte qui les contient, et JSON-LD."""
    soup = BeautifulSoup(html_txt, "html.parser")
    lots = {}
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for it in (data if isinstance(data, list) else data.get("@graph", [data])):
            if not isinstance(it, dict) or it.get("@type") not in ("Car", "Vehicle", "Product", "Offer"):
                continue
            url = urljoin(base, it.get("url") or "")
            off = it.get("offers") or {}
            off = off[0] if isinstance(off, list) and off else off
            km = (it.get("mileageFromOdometer") or {}).get("value") if isinstance(it.get("mileageFromOdometer"), dict) else it.get("mileageFromOdometer")
            texte = f'{it.get("name","")} {it.get("vehicleModelDate","") or it.get("productionDate","")} - {km or ""} km {off.get("price","") if isinstance(off, dict) else ""} €'
            if url:
                lots[url] = {"id": f"{site}:{url}", "site": site, "url": url, "texte": texte, "titre": it.get("name", "")[:90],
                             "image": (it.get("image")[0] if isinstance(it.get("image"), list) else it.get("image"))}
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"]).split("#")[0]
        if not re.search(lien_motif, url) or url in lots:
            continue
        noeud, texte = a, a.get_text(" ", strip=True)
        for _ in range(6):
            tl = texte.lower()
            if "km" in tl and ("€" in texte or "eur" in tl):
                break
            if noeud.parent is None:
                break
            noeud = noeud.parent
            texte = noeud.get_text(" ", strip=True)
        tl = texte.lower()
        if "km" not in tl or len(texte) > 1500 or not re.search(r"20[12]\d", texte):
            continue
        img = noeud.find("img")
        src = img and (img.get("data-src") or img.get("src"))
        lots[url] = {"id": f"{site}:{url}", "site": site, "url": url, "texte": texte,
                     "titre": (a.get("title") or a.get_text(" ", strip=True) or texte)[:90],
                     "image": urljoin(base, src) if src else None}
    return list(lots.values())

# ============================================================================
# 5. ÉVALUATION
# ============================================================================
def modele_de(texte):
    t = norm(texte)
    if re.search(MARQUEURS_THERMIQUE, t) and not re.search(r"\bkwh\b|electri", t):
        return None, "thermique ou hybride"
    if not re.search(MARQUEURS_ELEC, t):
        return None, "pas électrique"
    for m in MODELES:
        if re.search(m["motif"], t):
            if m["exclure"] and re.search(m["exclure"], t):
                return None, f"{m['nom']} : petite batterie"
            return m, None
    return None, "modèle hors liste"

def prix_final(ch, site_cfg):
    """Prix final frais compris (et s'il est estimé depuis la mise à prix)."""
    prix, estime = ch.get("prix"), False
    if not prix and ch.get("mise_a_prix"):
        prix, estime = round(ch["mise_a_prix"] * CONFIG["facteur_mise_a_prix"]), True
    if prix and not (ch.get("frais_inclus") or site_cfg.get("frais_pct", 0) == 0):
        prix = round(max(prix * (1 + site_cfg["frais_pct"]), prix + site_cfg["frais_min"]))
    return prix, estime

def distance_km(ch):
    if ch.get("dept") in DEPTS:
        la, lo = map(float, DEPTS[ch["dept"]].split(","))
        return round(haversine(CONFIG["maison"], (la, lo)) * 1.25)
    return None

def evaluer(lot, site_cfg):
    c = CONFIG
    raisons = []
    modele, pb = modele_de(lot["texte"])
    if re.search(LOTS_TERMINES, norm(lot["texte"])):
        raisons.append("lot déjà vendu")
    if not modele:                    # aucun filtre à l'extraction : le lot est gardé avec ses champs, le tri se fait dans le front
        raisons.append(pb)
        ch = extraire_champs(lot["texte"])
        prix, prix_estime = prix_final(ch, site_cfg)
        return {**lot, **ch, "modele": None, "prix_final": prix, "prix_estime": prix_estime,
                "distance_km": distance_km(ch), "garantie_restante_ans": None,
                "mensualite": round(mensualite_totale(prix)) if prix else None,
                "manque": [], "retenu": False, "raisons": raisons, "score": 0}
    ch = extraire_champs(lot["texte"])
    if lot.get("detail"):                                   # la fiche complète la liste (SoH, date, kWh, lieu)
        det = extraire_champs(lot["detail"])
        for k in ("soh", "date_vente", "kwh"):
            if det.get(k) is not None:
                ch[k] = det[k]
        if "dept" not in ch and det.get("dept"):
            ch["dept"] = det["dept"]
        ch["frais_inclus"] = ch.get("frais_inclus") or det.get("frais_inclus", False)
    annee, km = ch.get("annee"), ch.get("km")

    prix, prix_estime = prix_final(ch, site_cfg)

    # Garantie batterie restante
    g_ans, g_km = modele["garantie"]
    if modele["nom"] == "Tesla Model 3" and re.search(r"long[\s-]?range|grande autonomie|dual motor|performance|awd", norm(lot["texte"])):
        g_km = 192000
    reste = None
    if annee and km is not None:
        age = (dt.date.today() - dt.date(annee, 6, 30)).days / 365.25
        reste = round(min(g_ans - age, (g_km - km) / c["km_par_an"]), 1)

    dist = distance_km(ch)

    # Filtres éliminatoires
    if annee and annee < c["annee_min"]:
        raisons.append(f"trop ancienne ({annee})")
    if km is not None and km > c["km_max"]:
        raisons.append(f"{km:,} km > {c['km_max']:,}".replace(",", " "))
    if reste is not None and reste < c["garantie_restante_min_ans"]:
        raisons.append(f"garantie batterie restante {max(reste, 0)} an(s)")
    if prix and prix > c["budget_total_max"]:
        raisons.append(f"prix final ~{prix:,} € > budget".replace(",", " "))
    if ch.get("soh") and ch["soh"] < c["soh_min"]:
        raisons.append(f"SoH {ch['soh']} %")
    if ch.get("kwh") and ch["kwh"] < 55 and not (modele["nom"] == "Tesla Model 3"):
        raisons.append(f"batterie {ch['kwh']} kWh")
    manque = [n for n, v in (("année", annee), ("km", km), ("prix", prix)) if v is None]

    # Score /100
    s = 0.0
    s += 35 * min(max((reste or 0), 0), 5) / 5 if reste is not None else 10
    s += 20 * max(0, 1 - (km or c["km_max"]) / c["km_max"])
    s += 20 * min(max((c["budget_total_max"] - prix) / (c["budget_total_max"] * 0.4), 0), 1) if prix else 5
    s += 15 * min(max((ch["soh"] - 85) / 12, 0), 1) if ch.get("soh") else 5
    s += 10 * max(0, 1 - dist / 800) if dist is not None else 4
    s -= 5 if prix_estime else 0
    s -= 3 * len(manque)

    return {**lot, **ch, "modele": modele["nom"], "dc_kw": modele["dc"], "prix_final": prix,
            "prix_estime": prix_estime, "garantie_restante_ans": reste, "distance_km": dist,
            "mensualite": round(mensualite_totale(prix)) if prix else None,
            "manque": manque, "retenu": not raisons, "raisons": raisons, "score": round(max(s, 0), 1)}

# ============================================================================
# 6. COLLECTE
# ============================================================================
def collecter_vpauto(cfg, session, detail=True, debug=False):
    lots = {}
    for base in cfg["urls"]:
        nb_avant = -1
        for page in range(1, cfg["pages_max"] + 1):
            url = base + (f"&page={page}" if page > 1 else "")
            try:
                contenu = lire(url, session, debug, f"vpauto_{len(lots)}_{page}")
            except Exception as e:
                print(f"  ! {url} : {e}")
                break
            for l in cartes_vpauto(contenu, url):
                lots.setdefault(l["id"], l)
            time.sleep(CONFIG["delai_requetes_s"])
            if len(lots) == nb_avant:              # plus de nouveaux lots : fin de pagination
                break
            nb_avant = len(lots)
        print(f"  {base.split('?')[-1][:40]:<40} → {len(lots)} lots cumulés")
    lots = list(lots.values())
    if detail:
        cand = [l for l in lots if modele_de(l["texte"])[0]]
        print(f"  lecture de {len(cand)} fiches électriques…")
        for l in cand:
            try:
                soup = BeautifulSoup(lire(l["url"], session), "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                l["detail"] = soup.get_text(" ", strip=True)[:20000]
            except Exception as e:
                print(f"  ! fiche {l['url']} : {e}")
            time.sleep(CONFIG["delai_requetes_s"])
    return lots

def collecter_interencheres(cfg, debug=False):
    """Interenchères : lit l'état Nuxt (window.__NUXT__) des pages de résultats. Cloudflare bloque le mode
    headless : le navigateur est lancé en mode visible (WSLg/X requis), sans quoi 0 lot est remonté."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ! Playwright absent"); return []
    lots = {}
    with sync_playwright() as p:
        try:
            nav = p.chromium.launch(headless=False)
        except Exception as e:
            print(f"  ! navigateur visible indisponible (pas d'affichage ?) : {str(e)[:120]}"); return []
        pg = nav.new_page(locale="fr-FR")
        for base in cfg["urls"]:
            try:
                pg.goto(base, wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_function("() => window.__NUXT__ && window.__NUXT__.state && window.__NUXT__.state.sale",
                                     timeout=45000)
            except Exception:
                print(f"  ! bloquée (Cloudflare « {pg.title()[:30]} ») ou sans données"); continue
            pg.wait_for_timeout(3000)
            m = re.search(r'"totalPages":(\d+)', pg.evaluate("() => JSON.stringify(window.__NUXT__)"))
            total = min(int(m.group(1)) if m else 1, cfg.get("pages_max", 8))
            for page in range(1, total + 1):
                if page > 1:                               # pagination côté client : un rechargement re-déclenche Cloudflare
                    try:
                        pg.locator("button.v-pagination__item", has_text=re.compile(rf"^{page}$")).first.click(timeout=15000)
                        pg.wait_for_timeout(5000)
                    except Exception:
                        print(f"  ! page {page} : bouton de pagination introuvable"); break
                nuxt = pg.evaluate("() => JSON.stringify({items: window.__NUXT__.state.sale.saleItems || []})")
                hrefs = pg.evaluate("() => [...document.querySelectorAll('a[href*=\"/lot-\"]')].map(a => a.getAttribute('href'))")
                liens = {m.group(1): h for h in hrefs for m in [re.search(r"/lot-(\d+)\.html", h)] if m}
                if debug:
                    pathlib.Path("debug").mkdir(exist_ok=True)
                    pathlib.Path(f"debug/interencheres_{page}.json").write_text(nuxt, encoding="utf-8")
                for it in json.loads(nuxt)["items"]:
                    l = lot_interencheres(it, liens)
                    if l:
                        lots.setdefault(l["id"], l)
                print(f"  page {page}/{total} → {len(lots)} lots cumulés")
                time.sleep(CONFIG["delai_requetes_s"])
        nav.close()
    return list(lots.values())

def lot_interencheres(it, liens):
    ch = it.get("characteristics") or {}
    titre = (it.get("title_translations") or {}).get("fr-FR") or it.get("internal_reference") or ""
    desc = (it.get("description_translations") or {}).get("fr-FR") or ""
    lid = str(it.get("id"))
    if lid not in liens:
        return None
    annee = (ch.get("first_registration_date") or "")[:4]
    km = ch.get("mileage")
    est = (it.get("pricing") or {}).get("estimates") or {}
    org = it.get("organization") or {}
    adr = org.get("address") or {}
    dept = (adr.get("postcode") or "")[:2]
    parts = [titre, f"{annee} - {km} km" if annee and km is not None else "",
             "électrique" if ch.get("fuel_type") == "electric" else "",
             f"Estimation : {est['min']} € - {est['max']} €" if est.get("min") else "",
             f"Dept : {dept}" if dept in DEPTS else (adr.get("city") or ""), desc.replace("\n", " ")]
    img = ((it.get("medias") or [{}])[0].get("rewriteImgUrl") or {}).get("md")
    return {"id": f"interencheres:{lid}", "site": "interencheres", "url": urljoin("https://www.interencheres.com/", liens[lid]),
            "texte": " ".join(x for x in parts if x), "titre": titre[:90], "image": ("https:" + img) if img else None}

def collecter_navigateur(nom, cfg, visible=False, debug=False):
    if nom == "interencheres":
        return collecter_interencheres(cfg, debug)
    if not cfg["urls"]:
        print(f"  (aucune URL configurée pour {nom} : colle ta recherche filtrée dans CONFIG['sites']['{nom}']['urls'])")
        return []
    lots = {}
    urls = cfg["urls"]
    if cfg.get("pages_max"):
        urls = [u + (f"&page={i}" if i > 1 else "") for u in urls for i in range(1, cfg["pages_max"] + 1)]
    for url, contenu in lire_navigateur(urls, visible, debug, nom):
        trouves = cartes_generiques(contenu, url, nom, cfg["lien_motif"])
        neufs = [l for l in trouves if l["id"] not in lots]
        print(f"  {url[-40:]} → {len(trouves)} lots, {len(neufs)} nouveaux")
        if not trouves:
            print(f"    Rien d'extrait. Relance avec --debug et --visible, puis envoie debug/{nom}_0.html pour ajuster.")
        for l in neufs:
            lots[l["id"]] = l
        if cfg.get("pages_max") and not neufs:
            break
    return list(lots.values())

# ============================================================================
# 7. RAPPORT
# ============================================================================
def rapport(evalues, chemin, nouveaux):
    retenus = sorted([e for e in evalues if e["retenu"]], key=lambda e: -e["score"])
    ecartes = [e for e in evalues if not e["retenu"] and e.get("modele")]
    esc = lambda x: H.escape(str(x)) if x is not None else "?"
    def carte(e):
        badges = []
        if e["id"] in nouveaux: badges.append('<span class="b new">NOUVEAU</span>')
        if e.get("prix_estime"): badges.append('<span class="b warn">prix estimé depuis la mise à prix</span>')
        if e.get("manque"): badges.append(f'<span class="b warn">manque : {", ".join(e["manque"])}</span>')
        soh = f'{e["soh"]:.0f} %' if e.get("soh") else "à demander"
        img = f'<img src="{esc(e["image"])}" loading="lazy">' if e.get("image") else '<div class="noimg">⚡</div>'
        return f"""<a class="c" href="{esc(e['url'])}" target="_blank">{img}<div>
<div class="t"><b>{esc(e['modele'])}</b> <span class="sc">{e['score']}</span></div>
<div class="s">{esc(e['titre'])}</div>
<div class="g"><span>{esc(e.get('annee'))}</span><span>{(f"{e['km']:,}".replace(',', ' ') + ' km') if e.get('km') is not None else '? km'}</span>
<span><b>{(f"{e['prix_final']:,}".replace(',', ' ') + ' €') if e.get('prix_final') else '? €'}</b></span>
<span>~{esc(e.get('mensualite'))} €/mois</span><span>garantie batt. {esc(e.get('garantie_restante_ans'))} an(s)</span>
<span>SoH {soh}</span><span>{(str(e['distance_km']) + ' km de chez toi') if e.get('distance_km') is not None else 'lieu ?'}</span>
<span>vente {esc(e.get('date_vente'))}</span><span>{esc(e['site'])}</span></div>{''.join(badges)}</div></a>"""
    lignes = "".join(f"<tr><td><a href='{esc(e['url'])}' target=_blank>{esc(e['modele'])}</a></td><td>{esc(e.get('annee'))}</td>"
                     f"<td>{esc(e.get('km'))}</td><td>{esc(e.get('prix_final'))}</td><td>{esc(', '.join(e['raisons']))}</td></tr>"
                     for e in sorted(ecartes, key=lambda e: -e["score"]))
    page = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>VE aux enchères — {dt.date.today():%d/%m/%Y}</title><style>
body{{font:15px system-ui,sans-serif;margin:0;padding:16px;background:#f3f6f7;color:#16323f;max-width:980px;margin:auto}}
h1{{font-size:22px}} .c{{display:flex;gap:12px;background:#fff;border-radius:12px;padding:10px;margin:10px 0;text-decoration:none;color:inherit;box-shadow:0 1px 3px #0001}}
.c img,.noimg{{width:150px;height:110px;object-fit:cover;border-radius:8px;flex-shrink:0;background:#dfe8eb;display:flex;align-items:center;justify-content:center;font-size:32px}}
.t{{display:flex;justify-content:space-between;font-size:16px}} .sc{{background:#1d7a55;color:#fff;border-radius:8px;padding:0 8px;font-weight:700}}
.s{{font-size:12px;color:#557;margin:2px 0 6px}} .g{{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:13px}}
.b{{display:inline-block;font-size:11px;border-radius:5px;padding:1px 6px;margin:6px 6px 0 0;background:#dff1e8}} .b.warn{{background:#fbeed5}} .b.new{{background:#1d7a55;color:#fff}}
table{{width:100%;border-collapse:collapse;font-size:13px;background:#fff}} td,th{{padding:5px;border-bottom:1px solid #e3e9eb;text-align:left}}
details{{margin-top:24px}} .note{{font-size:12px;color:#557}}
@media(max-width:560px){{.c img,.noimg{{width:96px;height:80px}}}}</style></head><body>
<h1>⚡ VE aux enchères — {len(retenus)} lot(s) retenu(s)</h1>
<p class="note">Critères : budget {CONFIG['budget_total_max']} € frais compris · ≤ {CONFIG['km_max']:,} km · garantie batterie ≥ {CONFIG['garantie_restante_min_ans']} ans à {CONFIG['km_par_an']:,} km/an · SoH ≥ {CONFIG['soh_min']} % si affiché.
Mensualité : {CONFIG['financement']['apport']} € d'apport, 10 000 € à 1 %, reste à 3,5 %, 60 mois. Rappel : achat en l'état, sans essai — exige le rapport d'expertise et le SoH avant d'enchérir.</p>
{''.join(carte(e) for e in retenus) or '<p>Aucun lot ne passe tous les filtres aujourd’hui.</p>'}
<details><summary>{len(ecartes)} lot(s) de modèles ciblés écartés (voir pourquoi)</summary>
<table><tr><th>Modèle</th><th>Année</th><th>Km</th><th>Prix final</th><th>Raison</th></tr>{lignes}</table></details>
<p class="note">Généré le {dt.datetime.now():%d/%m/%Y %H:%M}. Données extraites automatiquement : vérifie toujours la fiche.</p></body></html>"""
    pathlib.Path(chemin).write_text(page, encoding="utf-8")
    return retenus

# ============================================================================
# 8. MAIN
# ============================================================================
def main():
    ap = argparse.ArgumentParser(description="Veille VE aux enchères")
    ap.add_argument("--sites", default="vpauto", help="vpauto,alcopa,interencheres")
    ap.add_argument("--fichier", help="analyser une page HTML enregistrée au lieu d'aller sur le site")
    ap.add_argument("--site", default="alcopa", help="site du --fichier")
    ap.add_argument("--sans-detail", action="store_true", help="ne pas ouvrir les fiches VPauto (plus rapide, moins précis)")
    ap.add_argument("--visible", action="store_true", help="navigateur visible (si le mode headless est bloqué)")
    ap.add_argument("--debug", action="store_true", help="enregistre les pages brutes dans ./debug/")
    ap.add_argument("--sortie", default="ve_encheres_rapport.html")
    ap.add_argument("--etat", default="ve_encheres_etat.json")
    a = ap.parse_args()

    lots = []
    if a.fichier:
        contenu = pathlib.Path(a.fichier).read_text(encoding="utf-8", errors="ignore")
        cfg = CONFIG["sites"].get(a.site, {"lien_motif": r".+", "frais_pct": 0.144, "frais_min": 300})
        lots = cartes_vpauto(contenu, "https://www.vpauto.fr/") if a.site == "vpauto" else \
               cartes_generiques(contenu, BASES.get(a.site, "https://" + a.site + "/"), a.site, cfg.get("lien_motif", r".+"))
        print(f"{a.fichier} → {len(lots)} lots")
    else:
        s = requests.Session()
        for nom in [x.strip() for x in a.sites.split(",") if x.strip()]:
            cfg = CONFIG["sites"].get(nom)
            if not cfg:
                print(f"Site inconnu : {nom}"); continue
            print(f"[{nom}]")
            lots += collecter_vpauto(cfg, s, not a.sans_detail, a.debug) if cfg["mode"] == "http" \
                    else collecter_navigateur(nom, cfg, a.visible, a.debug)

    evalues = [evaluer(l, CONFIG["sites"].get(l["site"], {"frais_pct": 0.144, "frais_min": 300})) for l in lots]
    etat_p = pathlib.Path(a.etat)
    vus = set(json.loads(etat_p.read_text())) if etat_p.exists() else set()
    nouveaux = {e["id"] for e in evalues if e["retenu"] and e["id"] not in vus}
    retenus = rapport(evalues, a.sortie, nouveaux)
    etat_p.write_text(json.dumps(sorted(vus | {e["id"] for e in evalues if e["retenu"]})))
    json_p = pathlib.Path(a.sortie).with_suffix(".json")
    json_p.write_text(json.dumps([{k: v for k, v in e.items() if k != "detail"} for e in evalues], ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"\n{len(lots)} lots analysés · {sum(1 for e in evalues if e.get('modele'))} de modèles ciblés · "
          f"{len(retenus)} retenus ({len(nouveaux)} nouveaux)")
    for e in retenus[:10]:
        print(f"  {e['score']:>5}  {e['modele']:<28} {e.get('annee')}  {e.get('km')} km  "
              f"{e.get('prix_final')} €  garantie {e.get('garantie_restante_ans')} an(s)  {e['url']}")
    print(f"Rapport : {pathlib.Path(a.sortie).resolve()}")

if __name__ == "__main__":
    main()
