# Installation du relevé Les Thés sur Terre — 2 minutes

Aussi court que le relevé Nakamura Tokichi : la boutique est publique, aucune
connexion n'est nécessaire. Pas d'identifiants à créer.

**Prérequis** : le relevé Marukyu-Koyamaen est déjà installé dans ce dépôt (mêmes
secrets réutilisés, section 6 de `SETUP.md`).

## 1. Pousser le fichier

```bash
git add matcha_watch_lesthes.py .github/workflows/matcha-watch-lesthes.yml
git commit -m "veille matcha Les Thés sur Terre"
git push
```

## 2. Rien à configurer de nouveau

Le workflow réutilise **exactement** les mêmes secrets que les deux autres relevés
(`MATCHALERT_API_URL`, `SCRAPPER_API_KEY`, `MATCHA_WEBHOOK_URL`, `SMTP_*`). S'ils
sont déjà définis, il n'y a rien de plus à faire.

## 3. Premier test

Onglet **Actions** → *Veille matcha Les Thes sur Terre* → **Run workflow**, en
cochant **verbose** pour voir quels produits sont surveillés et lesquels sont
ignorés.

Le premier run n'émet jamais d'alerte (état de référence). À partir du deuxième,
seules les transitions rupture → disponible remontent.

## 4. Ce qui tourne ensuite

Cron horaire (`23 * * * *` — 13 pour Marukyu, 17 pour Tokichi, 23 ici, pour
étaler la file de crons GitHub). Chaque exécution commite `lesthes_state.json`,
`stock_lesthes.csv` et `stock_lesthes.json`.

---

## 5. Deux limites à connaître

### Pas de stock taille par taille

Contrairement à Marukyu-Koyamaen (connecté) et Nakamura Tokichi (API Shopify),
ce site **ne permet pas** de connaître la disponibilité d'une taille précise.

La page produit ne rend pas son widget de détail côté serveur : les variantes
sont chargées en JavaScript, et les données de warmup Wix ne contiennent aucune
information d'inventaire. Il n'existe donc aucune source serveur pour le stock
par taille — l'obtenir exigerait un navigateur headless (Playwright), hors de
proportion pour un cron horaire.

**Conséquence** : la taille vaut toujours `Unique`, et « disponible » signifie
**« au moins une variante achetable »**. Les produits à plusieurs variantes sont
repérables à leur prix « À partir de … » et portent une mention explicite dans
la colonne `note` des exports.

C'est la même sémantique dégradée que le mode visiteur de Marukyu, documentée
dans son README : on distingue *« tout est en rupture »* de *« quelque chose est
disponible »*, jamais **quelle** taille.

### La liste des consommables est maintenue à la main

La catégorie `/category/matcha` mélange les thés et les accessoires (fouet,
chawan, chashaku, tamis, repose-fouet, boîte). Seuls les consommables sont
surveillés, via la liste `CONSUMABLE_SLUGS` en tête de
`matcha_watch_lesthes.py` :

```python
CONSUMABLE_SLUGS = {
    "matcha-premium-latte",
    "matcha-japon-kagoshima",
    "the-matcha-chiran-japon",
    "matcha-haut-de-gamme-japonais",
    "hojicha-en-poudre",
    "coffret-ceremonie-matcha",
}
```

Une liste explicite plutôt qu'un filtre sur le nom, parce qu'un « Bol à thé vert
matcha » contient le mot *matcha* sans être du matcha.

**Si la boutique ajoute un nouveau thé, il faut l'ajouter ici**, sinon il ne sera
jamais surveillé. Pour le repérer, lancez le relevé en mode bavard : les slugs
hors périmètre sont listés à chaque run.

```bash
python3 matcha_watch_lesthes.py --no-push --verbose
```

## 6. Garde-fous

Le script refuse de deviner un état de stock quand la page devient
méconnaissable — une refonte du site produirait sinon un relevé vide, interprété
comme « tout en rupture », suivi d'une vague de fausses alertes de restock au
retour. Il sort en erreur (code 1, aucune écriture en base) si :

- la page ne contient aucun bloc produit (`data-hook="product-item-root"`) ;
- aucun slug de `CONSUMABLE_SLUGS` n'est reconnu — signe que la boutique a
  renommé ses produits, pas qu'elle a vidé son catalogue.

Il avertit également si le nombre de produits vus tombe pile sur un palier de
pagination Wix (20, 24, 25, 50, 100) : la pagination du site est purement côté
client, donc une catégorie très fournie pourrait être tronquée.

## 7. Dépannage

Le parsing s'appuie **uniquement** sur les attributs `data-hook` et `data-slug`,
jamais sur les classes CSS — celles de Wix sont des hashes obfusqués
(`s_wnSvX o__34T_FM---typography-11-runningText`) qui changent à chaque
redéploiement du site.

Si un run échoue sur la structure, enregistrez la page et analysez-la hors ligne :

```bash
curl -sS -A "Mozilla/5.0" https://www.lesthes-surterre.com/category/matcha -o page.html
python3 matcha_watch_lesthes.py --parse-file page.html
```
