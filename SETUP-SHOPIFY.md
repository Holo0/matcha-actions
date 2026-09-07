# Installation des six relevés Shopify — 3 minutes pour les six

Ippodo Tea, Rocky's Matcha, Kettl, Mizuba Tea Co., Naoki Matcha et
Matchaeologist tournent toutes sur **Shopify**, qui expose son catalogue
publiquement en JSON. Aucune connexion, aucun identifiant à créer, aucune
session à gérer : une requête suffit à connaître la disponibilité de toutes les
références d'une boutique.

C'est pourquoi elles s'installent ensemble, contrairement à Marukyu-Koyamaen qui
a son propre guide (`SETUP.md`) parce qu'elle exige une authentification.

**Prérequis** : le relevé Marukyu-Koyamaen est déjà installé dans ce dépôt
(mêmes secrets réutilisés, section 6 de `SETUP.md`). Sinon, `SETUP.md` reste le
point de départ.

---

## 1. Pousser les fichiers

Les six scripts partagent **`matcha_shopify.py`**, qui porte le téléchargement du
catalogue, la lecture de la disponibilité et les invariants de l'auto-test. Sans
ce fichier, aucun des six ne démarre.

```bash
git add matcha_shopify.py \
        matcha_watch_ippodo.py \
        matcha_watch_rocky.py \
        matcha_watch_kettl.py \
        matcha_watch_mizuba.py \
        matcha_watch_naoki.py \
        matcha_watch_matchaeologist.py \
        .github/workflows/matcha-watch-ippodo.yml \
        .github/workflows/matcha-watch-rocky.yml \
        .github/workflows/matcha-watch-kettl.yml \
        .github/workflows/matcha-watch-mizuba.yml \
        .github/workflows/matcha-watch-naoki.yml \
        .github/workflows/matcha-watch-matchaeologist.yml
git commit -m "veille matcha : six boutiques Shopify"
git push
```

## 2. Rien à configurer côté GitHub

Les six workflows lisent **exactement** les mêmes secrets que le relevé Marukyu
(`secrets: inherit`). S'ils sont déjà définis, il n'y a rien de plus à faire :

| Secret | Rôle |
|---|---|
| `MATCHALERT_API_URL` | pousser les relevés en base (optionnel) |
| `SCRAPPER_API_KEY` | même clé que côté backend |
| `MATCHA_WEBHOOK_URL` | notification Discord/Slack/ntfy.sh (optionnel) |
| `SMTP_*` / `MAIL_*` | notification par email (optionnel) |

## 3. Côté backend : rien à faire, sauf si trois variables sont définies

**Le cas normal est qu'il n'y ait rien à faire.** Les valeurs par défaut du code
(`application.properties`) connaissent déjà les neuf
boutiques : une instance qui ne définit aucune de ces variables est correctement
planifiée.

Le piège n'existe que si le service **définit** l'une de ces trois variables, car
elles écrasent le défaut **en entier** — ce n'est pas une fusion :

| Variable | Si elle est définie |
|---|---|
| `SCRAPPER_TRIGGER_WORKFLOWS` | doit énumérer les neuf boutiques, sinon les manquantes ne sont **jamais** déclenchées (ça, ça se voit : aucun run) |
| `SCRAPPER_TRIGGER_FULL_CRONS` | doit énumérer les magasins 2 à 9, sinon les manquants retombent sur le rythme **horaire** de Marukyu |
| `SCRAPPER_TRIGGER_DENSE_CRONS` | idem, sinon leur scan dense Premium tombe à 15 min au lieu de 5 |

Les valeurs à recopier dans ce cas sont dans `matchalert-backend/.env.example`.
Le magasin 1 (Marukyu) est volontairement **absent** des deux listes de crons : il
retombe sur `SCRAPPER_TRIGGER_FULL_CRON` / `_DENSE_CRON`, qui sont taillés pour
ses quarante fiches HTML.

Au démarrage, le backend écrit une ligne par boutique et par famille de relevé
(`Relevé complet du magasin 6 planifie sur « 0 8/10 * * * * »`) : c'est là qu'on
vérifie que les neuf sont bien planifiées au bon rythme.

## 4. Côté base : la migration s'occupe de tout

`V16__magasins_shopify.sql` insère les six marchands (identifiants `4` à `9`).
Elle n'insère **aucune ligne de catalogue** : les scrapers poussent le leur à
chaque relevé, la table `matcha` se remplit donc au premier run.

## 5. Premier test

Onglet **Actions** → *Veille matcha Kettl* (par exemple) → **Run workflow**.

Le premier run n'émet jamais d'alerte : il établit l'état de référence. À partir
du deuxième, seules les transitions rupture → disponible remontent.

Pour vérifier le parsing sans toucher au réseau ni à la base :

```bash
python3 matcha_watch_kettl.py --self-test
python3 matcha_watch_kettl.py --no-push --verbose   # relevé réel, rien poussé
```

## 6. Ce qui tourne ensuite

Une minute de cron propre à chaque boutique, pour étaler la charge sur la file de
GitHub : 29 Ippodo, 33 Rocky's, 37 Kettl, 41 Mizuba, 47 Naoki, 53 Matchaeologist
(13 Marukyu, 17 Tokichi, 23 Les Thés sur Terre étaient déjà pris). Ce cron n'est
qu'un **filet de sécurité** : le rythme réel est piloté par le backend
(`SETUP-DECLENCHEUR-BACKEND.md`).

Chaque exécution commite ses trois fichiers d'état, distincts par boutique :
`kettl_state.json`, `stock_kettl.csv`, `stock_kettl.json`, et ainsi de suite.

## 7. Ce que chaque boutique a de particulier

Toutes lisent le même JSON, mais **aucune n'y range la taille au même endroit**.
C'est le seul vrai travail d'intégration, et c'est ce que la configuration de
chaque script décrit. Le détail est dans l'en-tête de chaque fichier ; en résumé :

| Boutique | Magasin | Où vit la taille | Particularité |
|---|---|---|---|
| Ippodo Tea | 4 | titre du produit (« Kanza 20g Box ») | prix en **yens** ; « Matcha To-Go Packets (2g x 10 packets) » n'a pas de taille lisible → « Unique » |
| Rocky's Matcha | 5 | fin du titre (« … Matcha 20g ») | préfixe de marque « rocky's matcha » retiré du nom ; kits de théière écartés |
| Kettl | 6 | **vraie variante Shopify** (« 20g », « 1kg ») | le catalogue le plus fourni et le plus souvent en rupture ; abonnements écartés |
| Mizuba Tea Co. | 7 | **tags** (« 30g »), sinon la variante | coffrets écartés : leurs variantes nomment un chawan, pas un poids |
| Naoki Matcha | 8 | variante, en **onces** | **trois** collections à lire, dédupliquées ; pas de collection « matcha » unique |
| Matchaeologist | 9 | fin du titre | **piège** : le titre de variante est une note de dégustation, pas une taille |

## 8. Limite connue, commune aux six

Un produit dont le conditionnement n'est lisible ni en variante, ni dans le
titre, ni dans les tags apparaît avec la taille **« Unique »** — jamais avec une
valeur inventée. C'est volontaire : `(magasin, nom, size)` est la clé métier côté
backend, et une taille devinée créerait une deuxième ligne pour un produit qui
n'en a qu'une.

Si un marchand refond ses libellés, `--verbose` montre ce qui a été lu, et
`--self-test` reste vert (il travaille sur une fixture figée) : c'est le relevé
réel qu'il faut regarder, pas l'auto-test.

Enfin, si une collection est renommée ou vidée côté marchand, le script **sort en
erreur** au lieu de rapporter « aucun changement » — ce qui fait remonter une
ligne `ERROR` dans la supervision plutôt que de laisser croire à une boutique
sagement stable.
