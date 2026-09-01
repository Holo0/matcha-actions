# Déclencheur des relevés piloté par le backend

Les relevés ne sont plus lancés par les crons de GitHub Actions, mais par le backend
MatchAlert, qui appelle l'API GitHub. Ce document explique pourquoi, ce qu'il faut
configurer, et ce que ce choix coûte.

## 1. Pourquoi

GitHub déclenche les workflows planifiés **au mieux**, sans aucune garantie — c'est écrit
dans sa documentation, mais l'ampleur réelle mérite d'être mesurée. Relevé sur ce dépôt
fin août 2026, sur `keep-warm.yml`, qui demande un déclenchement toutes les 10 minutes,
donc six par heure :

```
écarts réels entre deux runs consécutifs, en minutes :
112, 125, 138, 139, 155, 175, 211, 212, 220, 259, 289,
294, 302, 310, 322, 333, 362, 402, 428, 469, 753
```

Un run toutes les 2 à 12 heures au lieu de toutes les 10 minutes : environ **97 % des
déclenchements sont purement abandonnés**, pas retardés. Les trois crons `*/15` des
veilles subissaient la même chose, avec des écarts de 3 à 5 heures.

Conséquences concrètes :

- le palier Premium annoncé « scan toutes les 15 minutes » n'existait pas ;
- le relevé « horaire » du palier gratuit tombait en pratique toutes les 3 à 5 heures ;
- `keep-warm.yml` ne pouvait pas tenir sa promesse de ping toutes les 10 minutes, donc
  l'instance Render s'endormait quand même.

Un `@Scheduled` Spring, lui, tient l'heure exacte. Le rythme est donc décidé par le
backend, et les workflows sont déclenchés par `workflow_dispatch`.

## 2. Ce qui a changé côté workflows

Le palier d'un run était déduit du cron qui l'avait déclenché
(`github.event.schedule == '*/15 * * * *'`). Sur un `workflow_dispatch`,
`github.event.schedule` est vide : la comparaison n'aurait jamais pu reconnaître un scan
dense. Elle est remplacée par une entrée explicite.

- `matcha-watch-shared.yml` reçoit une entrée `dense` (booléen) à la place de
  `dense_cron`.
- Les trois appelants exposent une entrée `mode` (`gratuit` / `premium`) dans leur
  `workflow_dispatch`, et la transmettent via `dense`. Le bouton « Run workflow » de
  l'onglet Actions permet donc de tester les deux paliers à la main.
- Le cron `*/15` a été **retiré** des trois appelants : le rythme dense est tenu par le
  backend.
- Le cron horaire est **conservé** comme filet. Il ne dépend de rien d'autre que de
  GitHub : si le backend est en veille, en panne ou en cours de redéploiement, le relevé
  gratuit continue de tourner et la supervision continue de voir des runs. C'est
  volontairement redondant avec le déclencheur du backend ; le groupe `concurrency` de
  chaque workflow empêche deux relevés de la même boutique de se chevaucher.

## 3. Configuration côté backend (Render)

Deux variables d'environnement à ajouter :

| Variable | Valeur |
|---|---|
| `SCRAPPER_TRIGGER_REPO` | `Holo0/matcha-actions` |
| `SCRAPPER_TRIGGER_TOKEN` | jeton GitHub, voir ci-dessous |

Le jeton doit pouvoir **écrire sur Actions**, et rien de plus :

- jeton **fin** (recommandé) : *Repository access* = `matcha-actions` seul,
  *Permissions → Actions* = **Read and write** ;
- jeton **classique** : scopes `repo` et `workflow`.

Ce jeton vit dans les variables d'environnement d'un service web exposé : un jeton qui
peut écrire ailleurs que sur Actions de ce dépôt est un risque gratuit. Les jetons fins
expirent — notez la date, un jeton expiré arrête tous les relevés dense, et l'alerte de
supervision arrivera avec 2 h 30 de retard (`SCRAPPER_STALENESS_MINUTES`).

Valeurs par défaut, à ne surcharger qu'en cas de besoin :

| Variable | Défaut | Rôle |
|---|---|---|
| `SCRAPPER_TRIGGER_ENABLED` | `true` | coupe le déclencheur sans redéployer |
| `SCRAPPER_TRIGGER_REF` | `main` | branche sur laquelle déclencher |
| `SCRAPPER_TRIGGER_WORKFLOWS` | `1:matcha-watch.yml,2:matcha-watch-tokichi.yml,3:matcha-watch-lesthes.yml` | magasin → fichier de workflow |
| `SCRAPPER_TRIGGER_DENSE_CRON` | `0 0,15,30,45 * * * *` | rythme du scan Premium |
| `SCRAPPER_TRIGGER_FULL_CRON` | `0 5 * * * *` | rythme du relevé complet |

Sans jeton, le backend démarre normalement, journalise un avertissement et ne déclenche
rien : seul le cron horaire de GitHub reste actif. Un rythme dégradé et visible vaut
mieux qu'une instance qui refuse de partir.

## 4. Condition à ne pas oublier : garder le backend éveillé

**C'est le point faible de ce montage.** Render met un service du plan gratuit en veille
après 15 minutes sans trafic entrant. L'ordonnanceur s'arrête avec l'instance, et un top
manqué n'est jamais rattrapé. Un backend endormi ne déclenche donc aucun relevé dense.

`keep-warm.yml` est censé couvrir ça, mais il souffre exactement du défaut décrit en
section 1 : son cron ne part pas plus régulièrement que les autres. **Il faut un moniteur
externe** sur `https://<votre-backend>/api/health`, toutes les 5 minutes :

- [cron-job.org](https://cron-job.org) — gratuit, jusqu'à la minute ;
- [UptimeRobot](https://uptimerobot.com) — gratuit, 5 minutes.

Aucun secret à configurer : `/api/health` est public et ne touche pas la base. Tant que ce
moniteur n'est pas en place, le rythme dense reste théorique.

## 5. Vérifier que ça marche

Sans attendre le prochain top, en tant qu'administrateur (`ADMIN_EMAILS`) :

```bash
# Relevé complet de toutes les boutiques
curl -X POST -H "Authorization: Bearer $JWT" \
  "https://<backend>/api/admin/scrapper/trigger"

# Scan dense d'une seule boutique
curl -X POST -H "Authorization: Bearer $JWT" \
  "https://<backend>/api/admin/scrapper/trigger?dense=true&magasin=2"
```

La réponse dit, pour chaque boutique, si GitHub a accepté la demande ou pourquoi il l'a
refusée. Réponses à reconnaître :

| Réponse | Cause |
|---|---|
| `409` avec « Declencheur non configure » | `SCRAPPER_TRIGGER_TOKEN` ou `SCRAPPER_TRIGGER_REPO` absent |
| `HTTP 404` dans un résultat | jeton sans le droit Actions, ou fichier de workflow renommé |
| `HTTP 401` dans un résultat | jeton expiré ou révoqué |
| `HTTP 422` dans un résultat | la branche `SCRAPPER_TRIGGER_REF` n'existe pas, ou le workflow n'a pas de `workflow_dispatch` |
| `aucun produit suivi par un abonné actif` | normal en scan dense : rien à relever pour cette boutique |

Un run déclenché apparaît dans l'onglet Actions avec l'événement `workflow_dispatch`, et
non `schedule`. C'est la façon la plus simple de distinguer les runs pilotés par le
backend de ceux du cron de secours.

## 6. Ce que le backend fait de mieux que le workflow

Sur un scan dense, le backend lit directement en base la liste des produits suivis par un
abonné actif. Quand personne n'est concerné pour une boutique, **le run n'est pas
déclenché du tout** — au lieu d'être déclenché pour se sauter lui-même, comme le faisait
le cron dense. Le workflow garde sa propre vérification, parce qu'il reste déclenchable à
la main, mais elle ne sert plus que de garde-fou.
