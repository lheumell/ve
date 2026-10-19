# Veille VE — enchères

Scraper local + site statique publié sur GitHub Pages. Le scraper tourne chez toi
(connexion résidentielle, pas de blocage), le site n'héberge que l'affichage.

```
scraper.py       moteur : extraction, filtres, scoring (ne pas toucher au début)
build_site.py    collecte → docs/lots.json (+ score et prix max conseillé)
notify.py        alertes Telegram (nouveaux lots, favoris)
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

## 5. Favoris

Sur chaque carte, **☆** met le lot en favori (gardé dans le navigateur). Le chip
**★ Mes favoris** n'affiche que les favoris, filtres ignorés : un lot qui a dépassé
ton budget reste visible, et un lot retiré du catalogue reste listé (grisé).
**⬇ Exporter** télécharge `favoris.json` ; **⬆ Importer** le relit et fusionne
(pour passer du téléphone au PC). Les ventes à moins de 7 jours ont un badge
« AUJOURD'HUI / DEMAIN / dans X jours ».

## 6. Alertes Telegram

1. Crée un bot avec @BotFather, récupère le token, puis envoie un message au bot et lis ton `chat_id`
   sur `https://api.telegram.org/bot<TOKEN>/getUpdates`.
2. Crée `telegram.json` à la racine : `{"token": "...", "chat_id": "..."}` (ignoré par git).
3. Dépose ton `favoris.json` exporté à la racine (ignoré par git).

`publish.sh` lance `notify.py` après chaque collecte. Alertes, chacune envoyée une seule fois
(état dans `.notifications.json`) : nouveau lot de score ≥ 55, rappel de vente d'un favori à
J-3 / J-1 / jour J, variation de prix d'un favori > 200 €, favori retiré avant sa vente.
Le premier lancement initialise l'état sans rien envoyer. Test : `python notify.py --dry-run`.

Le score (0-100) pèse coût frais compris (50), km (15), année (10), SoH (15), batterie (10) ;
`BUDGET` en haut de `build_site.py` règle le budget et le prix max conseillé.

## 7. Prix d'adjudication (statistiques)

Alcopa n'affiche pas le prix final, Interenchères si — mais seulement le jour de la vente (la page du
lot renvoie 410 quelques jours après). `resultats.sh` tourne chaque jour à 16h45 (cron), ouvre les lots
Interenchères vendus le jour même (`historique.json`) et y ajoute `vendu`, `prix_adjuge` (hors frais) et
`adjuge_le`. Un lot sans résultat au passage de 16h45 (vente pas finie) n'est pas marqué : `./resultats.sh` relancé le soir même le complète.
Test sans écriture : `python resultats.py --dry-run [--date AAAA-MM-JJ]`.

## 8. Quand un site ne remonte rien

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
# ve
# ve
# ve
