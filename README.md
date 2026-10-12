# Veille VE — enchères

Scraper local + site statique publié sur GitHub Pages. Le scraper tourne chez toi
(connexion résidentielle, pas de blocage), le site n'héberge que l'affichage.

```
scraper.py       moteur : extraction, filtres, scoring (ne pas toucher au début)
build_site.py    collecte → docs/lots.json
publish.sh       build + commit + push
docs/index.html  le site (filtres, tri, badges NOUVEAU, masquage de lots)
docs/lots.json   les données, régénérées à chaque passage
```

## 1. Installation

```bash
cd ve-site
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # seulement pour alcopa / interenchères / mercier
python build_site.py --debug       # premier test
```

## 2. Mise en ligne (GitHub Pages)

```bash
git init && git add . && git commit -m "init"
git remote add origin git@github.com:<toi>/ve-encheres.git
git push -u origin main
```

Puis dans le dépôt : **Settings → Pages → Source: Deploy from a branch →
branch `main`, folder `/docs`**. Le site sort sur
`https://<toi>.github.io/ve-encheres/`.

Dépôt privé → GitHub Pages n'est pas disponible sur un compte gratuit.
Dans ce cas, `vercel --prod` depuis `docs/` fait la même chose en privé.

## 3. Mise à jour automatique

```bash
crontab -e
*/20 * * * * cd /home/lheumell/my/ve-site && ./publish.sh >> cron.log 2>&1
```

`publish.sh` ne commite que si `lots.json` a changé. La page se recharge toute
seule toutes les 5 minutes et à chaque retour sur l'onglet.

Pour plusieurs sites : `SITES=vpauto,alcopa,mercier ./publish.sh`

## 4. Réglages

Tout est en haut de `scraper.py` (`CONFIG`) : budget, km max, SoH min, garantie
restante minimale, financement, frais par site. Les modèles ciblés sont dans
`MODELES` (ajout d'un modèle = une ligne : motif, garantie, batterie, puissance DC).

`build_site.py` ajoute le Nissan Ariya, le site Mercier et calcule le **prix max
conseillé** par lot (enchère maximale pour rester sous le budget, frais de vente
et carte grise compris).

## 5. Quand un site ne remonte rien

```bash
./publish.sh --debug --visible
```
Les pages brutes atterrissent dans `debug/`. L'extraction générique cherche les
liens d'annonces et le JSON-LD ; si la structure du site change, c'est
`cartes_generiques()` dans `scraper.py` qu'il faut ajuster.

Pour Alcopa, Interenchères et Mercier, colle d'abord dans
`CONFIG["sites"][...]["urls"]` l'URL de ta recherche filtrée sur l'électrique,
copiée depuis ton navigateur.

## Rappel

Achat aux enchères = en l'état, sans essai, paiement comptant sous quelques jours.
Le score trie, il ne remplace pas le rapport d'expertise ni le certificat SoH.
# ve
