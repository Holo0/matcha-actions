# Installation du relevé Nakamura Tokichi — 2 minutes

Beaucoup plus court que le relevé Marukyu-Koyamaen (voir `SETUP.md`) : la boutique
Nakamura Tokichi tourne sur Shopify et expose son catalogue publiquement, sans
connexion. Pas d'identifiants à créer, pas de session à gérer.

**Prérequis** : le relevé Marukyu-Koyamaen est déjà installé dans ce dépôt (mêmes
secrets réutilisés, section 6 de `SETUP.md`). Si ce n'est pas le cas, `SETUP.md`
reste le point de départ.

## 1. Pousser le fichier

```bash
git add matcha_watch_tokichi.py .github/workflows/matcha-watch-tokichi.yml
git commit -m "veille matcha Nakamura Tokichi"
git push
```

## 2. Rien à configurer si vous poussez déjà vers MatchAlert

Le workflow réutilise **exactement** les mêmes secrets que le relevé Marukyu :

| Secret | Rôle |
|---|---|
| `MATCHALERT_API_URL` | pousser les relevés en base (optionnel) |
| `SCRAPPER_API_KEY` | même clé que côté backend |
| `MATCHA_WEBHOOK_URL` | notification Discord/Slack/ntfy.sh (optionnel) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `MAIL_TO` / `MAIL_FROM` | notification par email (optionnel) |

Si ces secrets sont déjà définis pour le relevé Marukyu, il n'y a **rien de plus**
à faire : le nouveau workflow les lit automatiquement.

## 3. Premier test

Onglet **Actions** → *Veille matcha Nakamura Tokichi* → **Run workflow**.

Le premier run n'émet jamais d'alerte (état de référence). À partir du deuxième,
seules les transitions rupture → disponible remontent.

## 4. Ce qui tourne ensuite

Cron horaire (`17 * * * *`, minute différente du relevé Marukyu pour étaler la
charge). Chaque exécution commite `tokichi_state.json`, `stock_tokichi.csv` et
`stock_tokichi.json` — même mécanisme que le relevé Marukyu, fichiers distincts.

## 5. Pourquoi c'est plus simple que Marukyu-Koyamaen

Nakamura Tokichi tourne sur Shopify, qui expose un catalogue public en JSON
(`/collections/matcha/products.json`) donnant directement la disponibilité de
chaque produit — le même champ que le thème du site utilise pour griser le
bouton « Sold out ». Une seule requête récupère tout le catalogue : pas de
connexion, pas de session, pas de risque de page de vérification anti-bot liée
à une authentification répétée.

## 6. Limite connue

Chaque produit Shopify n'a ici qu'une seule variante ("Default Title") : il n'y
a pas de vraie option de taille côté Shopify pour ce catalogue, le poids est
encodé dans le nom du produit lui-même (ex. « Matcha Ukishima-no-Shiro, 30g
Can »). Le script sépare nom et taille sur la dernière virgule du titre — fiable
tant que Nakamura Tokichi garde cette convention de nommage. Si un futur produit
ne la suit pas, il apparaîtra avec la taille « Unique » plutôt qu'une valeur
inventée (`python3 matcha_watch_tokichi.py --verbose` pour vérifier).
