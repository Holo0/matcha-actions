# Installation sur GitHub Actions — 5 minutes, une seule fois

À la fin, le relevé tourne toutes les heures dans le cloud. Vous ne lancez plus rien.

> Ce guide couvre le relevé **Marukyu-Koyamaen** (`matcha_watch.py`), le seul des trois
> qui exige une connexion. Les deux autres marchands sont nettement plus simples (aucun
> identifiant à créer) et réutilisent les secrets configurés ici :
>
> - **Nakamura Tokichi** → **[SETUP-TOKICHI.md](SETUP-TOKICHI.md)**
> - **Les Thés sur Terre** → **[SETUP-LESTHES.md](SETUP-LESTHES.md)**

---

## 1. Créer un dépôt **privé** (ou public si vous activez le palier Premium)

Sur github.com → **New repository** → nom au choix → cochez **Private** → Create.

Privé, parce que le dépôt va contenir l'historique de vos relevés. Vos identifiants,
eux, ne sont jamais dans le dépôt : ils vivent dans les *secrets* GitHub, chiffrés et
invisibles même pour vous une fois enregistrés.

**Sauf si vous utilisez le scan dense Premium** (section 8bis) : celui-ci a besoin de
minutes GitHub Actions illimitées, ce qui suppose un dépôt **public**. Aucun identifiant
n'y est exposé (toujours des secrets), seul l'historique des relevés et les issues de
restock deviennent visibles. Rien ne vous empêche de démarrer en privé et de basculer en
public plus tard, depuis Settings → Danger Zone → Change visibility.

## 2. Pousser les fichiers

```bash
git clone https://github.com/VOTRE-COMPTE/VOTRE-DEPOT.git
cd VOTRE-DEPOT
# copiez-y : matcha_watch.py, .gitignore, README.md, SETUP.md, .github/
git add .
git commit -m "veille matcha"
git push
```

Ou glissez-déposez les fichiers via **Add file → Upload files** sur l'interface web.
Attention dans ce cas : GitHub n'accepte pas le dossier `.github/` par glisser-déposer,
il faut créer les fichiers à la main via **Add file → Create new file** en tapant leur
chemin.

> **Organisation des fichiers.** Chaque boutique a son script
> (`matcha_watch.py`, `matcha_watch_tokichi.py`, `matcha_watch_lesthes.py`) et son
> workflow (`matcha-watch*.yml`), mais tous les trois s'appuient sur deux fichiers
> partagés qu'il faut aussi copier :
>
> | Fichier | Rôle |
> |---|---|
> | `matcha_common.py` | modèle, comparaison à l'état précédent, exports, envoi vers le backend, notification |
> | `.github/workflows/matcha-watch-shared.yml` | corps du relevé : palier Premium, résumé, issue de restock, commit de l'état |
>
> Les workflows par boutique ne portent plus que leurs crons et leurs paramètres,
> d'où leur brièveté. Un script sans `matcha_common.py` à côté ne démarre pas.

## 3. Enregistrer les identifiants

**Settings → Secrets and variables → Actions → New repository secret**, deux fois :

| Nom | Valeur |
|---|---|
| `MKY_USER` | l'email du compte Marukyu-Koyamaen |
| `MKY_PASS` | le mot de passe |

## 4. Choisir les produits (facultatif)

**Par défaut, sans rien configurer, tout le catalogue est surveillé** (~47 fiches) sur le
cron horaire — comportement pensé pour un dépôt **public** (minutes illimitées, voir
section 1 et section 9). Pour restreindre à une liste précise : même écran, onglet
**Variables** → New repository variable :

| Nom | Valeur |
|---|---|
| `MATCHA_ONLY` | `wako,yugen,isuzu,aoarashi` |

Utile si vous êtes resté sur un dépôt **privé** (quota de minutes limité, voir section 9)
ou si vous ne suivez de toute façon qu'une poignée de références.

## 5. Choisir comment être prévenu

**Par défaut, sans rien configurer** : chaque restock ouvre une *issue* dans le dépôt, et
GitHub vous envoie un email. Ça marche immédiatement, à condition que vous surveilliez le
dépôt (bouton **Watch → All Activity**).

**Pour une notification push instantanée sur le téléphone**, ajoutez un secret :

| Nom | Valeur |
|---|---|
| `MATCHA_WEBHOOK_URL` | `https://ntfy.sh/un-nom-secret-a-vous` |

Installez l'app ntfy, abonnez-vous à ce même nom. Aucun compte à créer. Choisissez un nom
difficile à deviner : n'importe qui connaissant l'URL reçoit vos alertes.

Un webhook Slack ou Discord fonctionne aussi, le script détecte le format tout seul.

## 6. Alimenter la base MatchAlert (facultatif)

Le relevé peut être écrit dans Firestore via l'API MatchAlert, en plus des alertes.
Deux secrets suffisent :

| Nom | Valeur |
|---|---|
| `MATCHALERT_API_URL` | l'URL publique du backend, ex. `https://api.matchalert.fr` |
| `SCRAPPER_API_KEY` | exactement la même valeur que côté backend |

Si l'un des deux manque, le script n'écrit rien et se comporte comme avant : cette
intégration ne peut pas faire échouer un relevé.

Ce qui part à chaque exécution :

- `POST /api/matcha-availability/push-batch` — l'état de **toutes** les tailles relevées.
  Le backend relit la dernière valeur connue pour chaque `(nom, size)` et n'écrit que si
  la disponibilité a changé ; envoyer l'état complet toutes les heures ne gonfle donc pas
  la collection.
- `POST /api/matchas/push-catalog` — le catalogue du magasin (`magasin`, `nom`, `size`,
  `url`). Sans lui, un produit reçoit bien ses disponibilités mais n'apparaît nulle part
  dans l'application, et son alerte de restock retombe sur l'URL de repli de la boutique
  au lieu du lien produit. Le backend fait un upsert sur `(magasin, nom, size)` en
  réutilisant l'identifiant existant, donc rien n'est dupliqué d'un run à l'autre.
- `POST /api/scrapper/log` — une ligne `SUCCESS` ou `ERROR` avec le résumé du run et
  l'identifiant de la boutique. Y compris quand le relevé échoue proprement : c'est là que
  se verront une session expirée ou un blocage anti-bot, sans avoir à ouvrir l'onglet
  Actions. Un script qui plante avant d'avoir rendu la main, lui, n'écrit rien — la
  supervision côté backend surveille donc l'absence de succès récent, pas les erreurs.

**Les libellés doivent correspondre.** La clé côté base est le couple `(nom, size)`. Le
script envoie le `data-product_title` du site (`Wako`, `Low Caffeine Matcha`) et la taille
telle quelle (`20g can`). Si les documents `matcha` déjà en base utilisent d'autres
libellés, l'application affichera deux produits distincts au lieu d'un. À vérifier avant
le premier run réel.

**Tester sans rien écrire** : bouton *Run workflow* → cochez **no_push**. Le relevé tourne,
les alertes partent, la base n'est pas touchée.

## 7. Premier lancement, en mode diagnostic

Onglet **Actions** → *Veille matcha Marukyu-Koyamaen* → **Run workflow** → cochez
**verbose** → **Run workflow**.

C'est l'étape qui valide la seule pièce jamais testée en conditions réelles : la connexion
automatique. Ouvrez le run, dépliez *Relever le stock*, et lisez.

| Ce que vous voyez | Signification |
|---|---|
| `N fiches à relever` (N ≈ 47 par défaut, ou moins si `MATCHA_ONLY` est défini) puis une ligne par produit | tout fonctionne |
| `Session non connectée` | secrets absents ou mal orthographiés |
| `Connexion refusée : …` | identifiants incorrects — le message vient du site |
| `Page de vérification anti-bot` | l'IP GitHub est filtrée → voir section 10 |

Le premier run n'émet jamais d'alerte : il enregistre l'état de référence. C'est voulu.
À partir du deuxième, seules les transitions rupture → disponible remontent.

## 8. C'est fini

Le workflow tourne ensuite tout seul, à la minute 13 de chaque heure. Rien à maintenir.

Chaque exécution laisse une trace lisible dans l'onglet Actions (résumé avec l'état de
chaque taille), et commite `matcha_state.json`, `stock.csv` et `stock.json`. Vous
récupérez donc gratuitement l'historique complet : quand chaque produit est revenu, à
quelle heure, à quel prix.

## 8bis. Palier Premium : scan dense pour les abonnés actifs

Le scan dense ne relève que les produits suivis par au moins un utilisateur avec un
abonnement MatchAlert actif — jamais tout le catalogue. Avant chaque run dense, une étape
interroge `GET /api/matcha-availability/premium-watched` (mêmes secrets
`MATCHALERT_API_URL` / `SCRAPPER_API_KEY` que la section 6) ; si personne de Premium ne
suit rien, le run entier est sauté sans consommer de minutes utiles.

**Ce n'est plus un cron GitHub.** Il l'a été (`*/15 * * * *`), et ça ne marchait pas :
GitHub déclenche les workflows planifiés au mieux, et mesuré sur ce dépôt un cron demandé
toutes les 10 minutes partait en réalité toutes les 2 à 12 heures. Un palier annoncé à 15
minutes ne peut pas reposer là-dessus. C'est désormais le backend MatchAlert qui déclenche
par `workflow_dispatch`, à l'heure exacte — **voir
[SETUP-DECLENCHEUR-BACKEND.md](SETUP-DECLENCHEUR-BACKEND.md)**, qui liste les deux
variables à poser côté Render et le moniteur externe indispensable.

Le palier d'un run est porté par l'entrée `mode` (`gratuit` / `premium`) du
`workflow_dispatch`, transmise au workflow partagé sous le nom `dense`. Le bouton « Run
workflow » de l'onglet Actions permet donc de déclencher les deux paliers à la main.

Prérequis inchangé : le scan dense a besoin de minutes GitHub Actions illimitées, donc
d'un **dépôt public** (section 1).

---

## 9. Quota et coût

Gratuit, mais pas illimité sur un dépôt privé : **2 000 minutes par mois**. Le
comportement par défaut du workflow (catalogue complet, scan dense Premium) suppose un
dépôt **public** — voir ci-dessous si vous êtes resté en privé.

| Configuration | Consommation estimée |
|---|---|
| `MATCHA_ONLY` limité à 4 produits, toutes les heures, dépôt privé | ~1 400 min/mois — **tient** |
| 4 produits + créneaux denses (lignes commentées du workflow) | ~1 700 min/mois — tient, mais serré |
| Tout le catalogue (47 fiches) et/ou scan dense Premium actif, dépôt encore privé | **ne tient pas** — passez en public |
| Tout le catalogue et scan dense Premium, dépôt public | illimité — **tient toujours** |

GitHub facture à la minute entamée, d'où l'écart avec le temps réel d'exécution.

**Si vous êtes resté sur un dépôt privé**, définissez la variable `MATCHA_ONLY` (section
4) pour revenir à une poignée de produits, sinon le scan par défaut du catalogue complet
dépassera le quota gratuit. Si vous voulez le catalogue complet et/ou le scan dense
Premium, passez le dépôt en **public** : les minutes deviennent illimitées. Vos
identifiants restent protégés — les secrets ne sont jamais exposés, y compris aux forks.
En revanche l'historique des relevés et les issues de restock deviennent visibles de
tous.

**Anti-bot.** Un scan complet envoie ~1 100 requêtes/jour au lieu de ~100 pour 4
produits (voir README, section "Ce que le script ne fait pas") — le workflow utilise
donc un délai plus généreux (`--delay 10-20`, au lieu de `6-12`) sur un scan complet.
Si des `429`/pages de vérification apparaissent malgré tout, resserrez avec
`MATCHA_ONLY`.

## 10. Les limites à connaître

**L'horaire ne dérive pas : il est abandonné.** GitHub déclenche les workflows planifiés
au mieux. La formulation habituelle — « comptez 5 à 20 minutes de retard » — sous-estime
largement ce qui se passe réellement : mesuré sur ce dépôt fin août 2026, un cron demandé
toutes les 10 minutes partait toutes les 2 à 12 heures, soit environ 97 % des
déclenchements purement abandonnés. Sur un produit qui s'épuise en 12 minutes, ça se
paie.

C'est la raison d'être du déclencheur côté backend
([SETUP-DECLENCHEUR-BACKEND.md](SETUP-DECLENCHEUR-BACKEND.md)). Le cron horaire de chaque
workflow subsiste comme filet, avec cette réserve en tête : sa minute (13, 17, 23 plutôt
que 0) limite la casse aux heures chargées, sans rien garantir.

**L'IP peut être filtrée.** Les runners GitHub sortent par des plages datacenter connues.
Le site peut les traiter différemment de votre connexion personnelle. Si le run 7 échoue
sur une page de vérification, GitHub Actions n'est pas jouable pour ce site : il faut une
IP résidentielle, donc un Raspberry Pi chez vous ou un petit VPS. Le script est le même,
seul l'hébergement change — dites-le-moi et je vous prépare l'unité systemd.

**Les workflows planifiés s'endorment.** GitHub désactive les crons après 60 jours sans
activité sur le dépôt. Ici les commits d'état à chaque relevé maintiennent l'activité,
donc le problème ne devrait pas se poser. Vous recevez un email si ça arrive.

## 11. Arrêter ou modifier

- **Suspendre** : onglet Actions → le workflow → `...` → *Disable workflow*.
- **Changer la fréquence** : éditez la ligne `cron` du workflow.
- **Changer les produits** : modifiez la variable `MATCHA_ONLY`, rien d'autre.
- **Tester sans attendre** : bouton *Run workflow*, à n'importe quel moment.
