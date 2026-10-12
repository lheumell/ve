#!/usr/bin/env python3
"""
scraper.py — v1 : extrait TOUS les véhicules électriques de VPauto, Alcopa et Interenchères.

Aucun filtre, aucun scoring : chaque lot trouvé est renvoyé avec les champs bruts lus sur le site.
Le tri / la recherche se font ensuite dans le front (docs/index.html).

Champs d'un lot : id, site, url, titre, image, annee, km, prix, prix_type, mise_a_prix, frais_inclus,
                  kwh, soh, ville, dept, date_vente, heure_vente, texte
(un champ est None quand le site ne l'affiche pas)
"""
import datetime as dt, json, pathlib, re, time, unicodedata
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

DELAI = 2.5   # secondes entre deux requêtes, par politesse
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
SEP_NUM = r"[\s.  ]"

# Seule restriction : le périmètre « véhicules électriques » (filtre énergie du site lui-même).
SITES = {
    "vpauto": {
        "mode": "http",
        "urls": ["https://www.vpauto.fr/vehicule/liste?energy=electrique"],
        "pages_max": 6,
    },
    "alcopa": {
        "mode": "browser",
        "urls": ["https://www.alcopa-auction.fr/recherche?energy_families%5B%5D=EL&category_view=VP"],
        "pages_max": 8,                       # &page=N, arrêt dès qu'une page n'apporte rien de nouveau
        "base": "https://www.alcopa-auction.fr/",
        "lien_motif": r"alcopa-auction\.fr/voiture-occasion/.+",
    },
    "interencheres": {
        "mode": "browser-visible",            # Cloudflare bloque le headless : fenêtre visible (WSLg) requise
        "urls": ["https://www.interencheres.com/vehicules/vehicules-electriques-et-hydrides"],
        "pages_max": 60,
    },
}

# Frais en sus (TTC), hors carte grise et transport. Relevés sur les CGV Alcopa et sur les conditions des lots.
FRAIS_SITE = {
    "vpauto": {"pct": 0.0, "min": 0, "fixe": 285,        # prix « frais de vente inclus » ; dossier VE avec diagnostic batterie
               "detail": "frais de vente inclus + 285 € de dossier VE"},
    "alcopa": {"pct": 0.144, "min": 360, "fixe": 185,    # 14,40 % (min 360) + 140 € de frais de vente + 45 € véhicule électrique
               "detail": "14,4 % (min 360 €) + 185 € de frais fixes"},
    "interencheres": {"pct": 0.144, "min": 360, "fixe": 185,   # défaut = barème Alcopa, remplacé par celui du lot quand il est lisible
                      "detail": "14,4 % (min 360 €) + 185 € de frais fixes"},
}

def _eur(x):
    return f"{x:,.0f}".replace(",", " ")

def parser_frais_ie(texte, type_vente):
    """Frais lus dans les conditions de paiement d'un lot Interenchères (None si le texte ne les donne pas)."""
    t = re.sub(r"\s+", " ", texte or "")
    if not re.search(r"frais", t, re.I):
        return None
    if re.search(r"volontaire", t, re.I) and re.search(r"l[ée]gaux|judiciaire", t, re.I):
        i = re.search(r"volontaire", t, re.I).start()
        j = re.search(r"l[ée]gaux|judiciaire", t, re.I).start()
        t = t[i:] if type_vente == "voluntary" else (t[j:i] if j < i else t[j:])
    # frais de vente = premier taux « TTC » entre 5 et 30 % (les 1,8 % de live / services ne sont pas des frais de vente)
    taux = [float(x.replace(",", ".")) for x in re.findall(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%\s*TTC", t, re.I)]
    taux = [x for x in taux if 5 <= x <= 30]
    if not taux:
        return None
    pct = round(taux[0] / 100, 4)
    mini = re.search(r"minimum\s+de\s+(\d+)\s*€", t, re.I)
    fixe = 0
    for motif in (r"(\d+)\s*€\s*(?:TTC\s*)?de frais de vente\b",
                  r"(\d+)\s*€\s*(?:TTC\s*)?de frais suppl[ée]mentaires pour tous les v[ée]hicules [ée]lectriques",
                  r"(\d+)\s*€\s*(?:TTC\s*)?(?:de )?frais de dossiers?"):
        mm = re.search(motif, t, re.I)
        if mm:
            fixe += int(mm.group(1))
    return {"pct": pct, "min": int(mini.group(1)) if mini else 0, "fixe": fixe}

def calcul_frais(site, prix, frais_lot=None, frais_inclus=False, fixe_fiche=None):
    """(frais, détail, source) pour un prix d'adjudication. source = « lot » (lu sur le lot) ou « site » (barème du site)."""
    f, source = dict(FRAIS_SITE[site]), "site"
    if frais_lot:
        f.update(frais_lot); source = "lot"
    if fixe_fiche is not None:
        f["fixe"], source = fixe_fiche, "lot"
    if frais_inclus:
        f["pct"], f["min"] = 0.0, 0
    detail = (f"{f['pct'] * 100:g}".replace(".", ",") + " %" + (f" (min {_eur(f['min'])} €)" if f["min"] else "") if f["pct"] else "frais de vente inclus") \
        + (f" + {_eur(f['fixe'])} €" if f["fixe"] else "")
    if not prix:
        return None, detail, source
    return round(max(prix * f["pct"], f["min"] if f["pct"] else 0) + f["fixe"]), detail, source


VILLES = {"lille": "59", "rouen": "76", "paris": "75", "lorient": "56", "caudan": "56", "nantes": "44", "lyon": "69",
          "bordeaux": "33", "marseille": "13", "miramas": "13", "rennes": "35", "toulouse": "31", "strasbourg": "67",
          "metz": "57", "nancy": "54", "reims": "51", "amiens": "80", "orleans": "45", "tours": "37", "montpellier": "34",
          "nice": "06", "grenoble": "38", "dijon": "21", "clermont": "63", "le mans": "72", "angers": "49", "caen": "14"}
MARQUEURS_ELEC = r"electri|kwh|\bev\s?\d*\b|e-?tech|\bid\.?\s?\d"
# Pas des voitures : cycles, deux-roues, quadricycles / voiturettes sans permis, engins.
NON_VOITURE = (r"\bv[ée]los?\b|trottinette|scooter|\bmotos?\b|\bquads?\b|cyclomoteur|gyropode|hoverboard|draisienne|"
               r"\bligier\b|\baixam\b|microcar|chatenet|tracteur|remorque|chariot|\bengin|tondeuse|karting|golfette|"
               r"sans permis|\bfourgon|\bcamion|utilitaire")
MARQUEURS_THERMIQUE = r"hybrid|hybride|phev|\bhev\b|plug[\s-]?in|rechargeable|mhev|essence|diesel|gazole|\btdi\b|\bdci\b"


# ============================================================================
# OUTILS
# ============================================================================
def norm(s):
    s = unicodedata.normalize("NFKD", (s or "").replace("€", " eur ")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s).lower().strip()

def num(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None

def lire(url, session, debug=False, nom="page"):
    r = session.get(url, timeout=30, headers={"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    r.raise_for_status()
    if debug:
        pathlib.Path("debug").mkdir(exist_ok=True)
        pathlib.Path(f"debug/{nom}.html").write_text(r.text, encoding="utf-8")
    return r.text

def lire_navigateur(urls, debug=False, prefixe="page"):
    from playwright.sync_api import sync_playwright
    pages = []
    with sync_playwright() as p:
        nav = p.chromium.launch(headless=True)
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
            time.sleep(DELAI)
        nav.close()
    return pages


# ============================================================================
# EXTRACTION DES CHAMPS (texte d'une carte ou d'une fiche)
# ============================================================================
def extraire_champs(texte):
    t = norm(texte)
    o = {}
    m = re.search(rf"\b(20[012]\d)\s*[-–|·/]\s*(\d[\d{SEP_NUM[1:-1]}]*?)\s*km\b", t)
    if m:
        o["annee"], o["km"] = int(m.group(1)), num(m.group(2))
    else:
        t_km = re.sub(rf"\b20[012]\d\b(?=\s+\d{{1,3}}(?:{SEP_NUM}\d{{3}})*\s*km\b)", " ", t)   # « mise : 2020 199 075 km »
        m = re.search(rf"(\d{{1,3}}(?:{SEP_NUM}\d{{3}})+|\d{{1,6}})\s*km\b", t_km)
        if m:
            o["km"] = num(m.group(1))
        m = re.search(r"\b(20[012]\d)\b", t)
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
        m2 = re.search(rf"(?:prix|enchere courante|enchere actuelle|offre actuelle|prix de reserve)\s*:?\s*{prix}\s*(?:€|eur)", t)
        if m2 and not m:
            o["prix"], o["prix_type"] = num(m2.group(1)), "prix affiché"
        elif not m:
            for c in re.findall(rf"{prix}\s*(?:€|eur)", t):
                v = num(c)
                if v and 500 <= v <= 500000:
                    o["prix"], o["prix_type"] = v, "prix affiché"
                    break
    o["frais_inclus"] = bool(re.search(r"frais\s*de\s*vente\s*inclus|frais\s*inclus", t))
    m = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*kwh(?!\s*/)", t)
    if m:
        o["kwh"] = float(m.group(1).replace(",", "."))
    m = re.search(r"(?:\bsoh\b|etat de sante|sante (?:de la )?batterie|certificat batterie)[^\d%]{0,40}(\d{2,3}(?:[.,]\d+)?)\s*%", t)
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
    if "date_vente" not in o:
        m = re.search(r"\blot\s*n\W*\d+\s+(\d{1,2})/(\d{1,2})/(\d{4})", t)
        if m:
            try:
                o["date_vente"] = dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
            except ValueError:
                pass
    return o

def enregistrer(site, ident, url, titre, image, texte, frais_lot=None, fixe_fiche=None, **forces):
    """Lot au schéma commun. `forces` impose des valeurs lues dans des données structurées."""
    ch = extraire_champs(texte)
    ch.update({k: v for k, v in forces.items() if v is not None})
    frais, detail, source = calcul_frais(site, ch.get("prix"), frais_lot, ch.get("frais_inclus", False) or site == "vpauto",
                                         fixe_fiche)
    return {
        "id": f"{site}:{ident}", "site": site, "url": url, "titre": (titre or "").strip()[:120], "image": image,
        "annee": ch.get("annee"), "km": ch.get("km"), "prix": ch.get("prix"), "prix_type": ch.get("prix_type"),
        "mise_a_prix": ch.get("mise_a_prix"), "frais_inclus": ch.get("frais_inclus", False),
        "kwh": ch.get("kwh"), "soh": ch.get("soh"), "ville": ch.get("ville"), "dept": ch.get("dept"),
        "date_vente": ch.get("date_vente"), "heure_vente": ch.get("heure_vente"),
        "frais_estimes": frais, "cout_estime": (ch["prix"] + frais) if ch.get("prix") and frais is not None else None,
        "frais_detail": detail, "frais_source": source, "texte": re.sub(r"\s+", " ", texte).strip()[:500],
    }


# ============================================================================
# VPAUTO (HTTP simple)
# ============================================================================
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
            lots[ident] = {"ident": ident, "url": url, "texte": texte, "image": urljoin(base, src) if src else None}
    return list(lots.values())

def collecter_vpauto(cfg, debug=False):
    session = requests.Session()
    bruts = {}
    for base in cfg["urls"]:
        for page in range(1, cfg["pages_max"] + 1):
            url = base + (f"&page={page}" if page > 1 else "")
            try:
                contenu = lire(url, session, debug, f"vpauto_{page}")
            except Exception as e:
                print(f"  ! {url} : {e}")
                break
            neufs = [l for l in cartes_vpauto(contenu, url) if l["ident"] not in bruts]
            for l in neufs:
                bruts[l["ident"]] = l
            print(f"  page {page} → {len(neufs)} nouveaux, {len(bruts)} cumulés")
            time.sleep(DELAI)
            if not neufs:
                break
    print(f"  lecture de {len(bruts)} fiches…")
    lots = []
    for l in bruts.values():
        fiche = {}
        try:                                   # la fiche apporte le vrai titre, le lieu, la date de mise en circulation
            fiche = lire_fiche_vpauto(BeautifulSoup(lire(l["url"], session), "html.parser"))
        except Exception as e:
            print(f"  ! fiche {l['url']} : {e}")
        lots.append(enregistrer("vpauto", l["ident"], l["url"], fiche.get("titre") or l["texte"][:90], l["image"],
                                l["texte"], fixe_fiche=fiche.get("frais_dossier"), date_vente=fiche.get("date_vente"),
                                heure_vente=fiche.get("heure_vente"), dept=fiche.get("dept"), ville=fiche.get("ville"),
                                kwh=fiche.get("kwh")))
        time.sleep(DELAI)
    return lots

def lire_fiche_vpauto(soup):
    vente = soup.find(attrs={"data-end-date": True})          # « 2026/10/26 09:30:00 » = date et heure de la vente
    for tag in soup(["script", "style"]):
        tag.decompose()
    txt = re.sub(r"\s+", " ", soup.get_text(" "))
    h1 = [re.sub(r"\s+", " ", h.get_text(" ")).strip() for h in soup.find_all("h1")]
    o = {"titre": h1[1] if len(h1) > 1 else (h1[0] if h1 else None)}
    m = vente and re.match(r"(\d{4})/(\d{2})/(\d{2})(?: (\d{2}:\d{2}))?", vente["data-end-date"])
    if m:
        o["date_vente"], o["heure_vente"] = f"{m.group(1)}-{m.group(2)}-{m.group(3)}", m.group(4)
    m = re.search(r"Localisation\s*:\s*(\d{2}|2A|2B)\s*-\s*([^:]{2,40}?)(?=\s+(?:Type|Observation|Merci|TVA|Equipements)|$)", txt)
    if m:
        o["dept"], o["ville"] = m.group(1), m.group(2).strip().title()
    m = re.search(r"(\d{2,3}(?:[.,]\d)?)\s*kwh", txt, re.I)
    if m:
        o["kwh"] = float(m.group(1).replace(",", "."))
    m = re.search(r"frais de dossier[^.€]{0,80}?s['’]?[ée]l[èe]vent\s*[àa]\s*(\d+)\s*€", txt, re.I)
    if m:
        o["frais_dossier"] = int(m.group(1))
    return o


# ============================================================================
# ALCOPA (navigateur headless, pagination &page=N)
# ============================================================================
def cartes_alcopa(html_txt, base, lien_motif):
    soup = BeautifulSoup(html_txt, "html.parser")
    lots = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base, a["href"]).split("#")[0]
        if not re.search(lien_motif, url) or url in lots:
            continue
        noeud, texte = a, a.get_text(" ", strip=True)
        for _ in range(6):                         # remonte jusqu'à la carte qui contient km et prix
            tl = texte.lower()
            if "km" in tl and ("€" in texte or "eur" in tl):
                break
            if noeud.parent is None:
                break
            noeud = noeud.parent
            texte = noeud.get_text(" ", strip=True)
        if "km" not in texte.lower() or len(texte) > 1500:
            continue
        img = noeud.find("img")
        src = img and (img.get("data-src") or img.get("src"))
        lots[url] = enregistrer("alcopa", url.rstrip("/").rsplit("-", 1)[-1], url,
                                a.get("title") or a.get_text(" ", strip=True) or texte,
                                urljoin(base, src) if src else None, texte)
    return list(lots.values())

def collecter_alcopa(cfg, debug=False):
    urls = [u + (f"&page={i}" if i > 1 else "") for u in cfg["urls"] for i in range(1, cfg["pages_max"] + 1)]
    lots = {}
    for url, contenu in lire_navigateur(urls, debug, "alcopa"):
        trouves = cartes_alcopa(contenu, cfg["base"], cfg["lien_motif"])
        neufs = [l for l in trouves if l["id"] not in lots]
        print(f"  {url[-14:]} → {len(trouves)} lots, {len(neufs)} nouveaux")
        for l in neufs:
            lots[l["id"]] = l
        if not neufs:
            break
    return list(lots.values())


# ============================================================================
# INTERENCHÈRES (navigateur visible : lit l'état Nuxt, pagination par clic)
# ============================================================================
def est_electrique_ie(it):
    """Électrique pur uniquement : le site déclare parfois « electric » pour des hybrides rechargeables."""
    ch = it.get("characteristics") or {}
    titre = norm(" ".join([(it.get("title_translations") or {}).get("fr-FR") or "", it.get("internal_reference") or ""]))
    if re.search(NON_VOITURE, titre) or (ch.get("national_type") and ch["national_type"] != "VP"):
        return False
    if (re.search(MARQUEURS_THERMIQUE, titre) or (ch.get("engine_displacement") or 0) > 0
            or re.search(r"(?<![\d.])[0-3]\.\d(?!\d)(?!\s*k)", titre)):      # « 1.6 », « 2.0 » = cylindrée
        return False
    if ch.get("fuel_type"):
        return ch["fuel_type"] == "electric"
    return bool(re.search(MARQUEURS_ELEC, titre))

def vente_ie(it):
    """(date, heure) de la vente, convertis en heure de Paris, depuis sale.start_at (UTC)."""
    brut = (it.get("sale") or {}).get("start_at") or (it.get("sale") or {}).get("datetime")
    try:
        d = dt.datetime.fromisoformat(brut.replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Paris"))
    except (AttributeError, ValueError):
        return None, None
    return d.date().isoformat(), d.strftime("%H:%M")

def lot_interencheres(it, liens):
    lid = str(it.get("id"))
    if lid not in liens or not est_electrique_ie(it):
        return None
    ch = it.get("characteristics") or {}
    titre = " ".join(x for x in (ch.get("brand"), ch.get("commercial_name"), ch.get("type_variant_version")) if x) \
        or (it.get("title_translations") or {}).get("fr-FR") or it.get("internal_reference") or ""
    desc = (it.get("description_translations") or {}).get("fr-FR") or ""
    est = (it.get("pricing") or {}).get("estimates") or {}
    adr = (it.get("organization") or {}).get("address") or {}
    cp = (adr.get("postcode") or "")[:2]
    img = ((it.get("medias") or [{}])[0].get("rewriteImgUrl") or {}).get("md")
    annee = (ch.get("first_registration_date") or "")[:4]
    date_vente, heure_vente = vente_ie(it)
    return enregistrer("interencheres", lid, urljoin("https://www.interencheres.com/", liens[lid]), titre,
                       ("https:" + img) if img else None, f"{titre} {desc}".replace("\n", " "),
                       frais_lot=parser_frais_ie((it.get("conditions") or {}).get("payment"), it.get("type")),
                       annee=int(annee) if annee.isdigit() else None, km=ch.get("mileage"),
                       date_vente=date_vente, heure_vente=heure_vente,
                       prix=est.get("max") or None, prix_type="estimation haute" if est.get("max") else None,
                       dept=cp if cp.isdigit() and cp != "20" else None, ville=adr.get("city"))

def collecter_interencheres(cfg, debug=False):
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
            total = min(int(m.group(1)) if m else 1, cfg["pages_max"])
            for page in range(1, total + 1):
                if page > 1:                       # pagination côté client : un rechargement re-déclenche Cloudflare
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
                time.sleep(DELAI)
        nav.close()
    return list(lots.values())


# ============================================================================
COLLECTEURS = {"vpauto": collecter_vpauto, "alcopa": collecter_alcopa, "interencheres": collecter_interencheres}

def collecter(nom, debug=False):
    lots = COLLECTEURS[nom](SITES[nom], debug)
    voitures = [l for l in lots if not re.search(NON_VOITURE, norm(l["titre"]))]
    if len(voitures) < len(lots):
        print(f"  {len(lots) - len(voitures)} lot(s) écarté(s) : pas des voitures (vélo, voiturette sans permis…)")
    return voitures
