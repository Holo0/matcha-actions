# Empêcher le backend de s'endormir

Render met un service du plan gratuit **en veille après 15 minutes sans trafic**.
Le réveil prend une cinquantaine de secondes, pendant lesquelles le visiteur
attend devant une page blanche. Pour un site de veille de stock, qui n'a pas de
trafic continu, c'est le cas le plus fréquent : presque chaque visiteur arrive
sur un service endormi.

Le workflow `keep-warm.yml` appelle `GET /api/health` toutes les dix minutes.

---

## 1. Ce qu'il faut configurer

Un seul secret, **déjà présent** si vous avez suivi `SETUP.md` — c'est le même
que celui utilisé par les scrapers :

| Secret | Valeur |
|---|---|
| `MATCHALERT_API_URL` | `https://matchalert-backend.onrender.com` |

Sans lui, le workflow s'arrête proprement avec un avertissement et ne fait rien.

Le workflow vit dans ce dépôt (`matcha-actions`) et **pas** dans
`matchalert-backend`, pour une raison de facturation : ce dépôt est public, donc
les minutes GitHub Actions y sont illimitées. `matchalert-backend` est privé, où
GitHub arrondit **chaque job à la minute entière** — 144 pings par jour y
coûteraient environ 4 300 minutes par mois, pour un quota gratuit de 2 000.

## 2. Vérifier que ça marche

Onglet **Actions** → *Maintenir le backend MatchAlert eveille* → **Run
workflow**. Le résumé du run affiche l'un des deux cas :

- `Service deja eveille (0s).` — le ping précédent a fait son travail.
- `Reveil a froid en 47s — le service s'etait endormi.` — le ping a payé le
  réveil à la place d'un visiteur. Normal au premier run ; répété, c'est le
  signe que les crons sont trop retardés (voir §4).

En ligne de commande :

```bash
curl -s -w '\n%{http_code} en %{time_total}s\n' \
  https://matchalert-backend.onrender.com/api/health
# {"status":"UP","service":"MatchAlert"}
# 200 en 0.183s
```

## 3. ⚠️ Le quota d'heures Render

**À lire avant de laisser tourner.** Le plan gratuit de Render donne
**750 heures d'instance par mois, pour tout le compte**. Un service endormi n'en
consomme pas ; un service maintenu éveillé 24 h/24 en consomme la totalité :

| | Heures/mois |
|---|---|
| Mois de 31 jours | 744 h |
| Quota gratuit | 750 h |
| Marge restante | **6 h** |

Autrement dit : **ça tient, mais tout juste, et seulement si `matchalert-backend`
est le seul service web gratuit du compte.** S'il y en a un second, le quota est
dépassé et Render **suspend les services gratuits** jusqu'au cycle suivant.

Deux façons de reprendre de la marge, si besoin :

**a. Restreindre le ping aux heures de trafic.** Personne ne consulte le
catalogue à 4 h du matin ; un réveil lent à ce moment-là ne gêne personne. Dans
`keep-warm.yml`, remplacez le cron par :

```yaml
    # 06:00–22:59 UTC, soit 08:00–00:59 à Paris en été. ~527 h/mois.
    - cron: "*/10 6-22 * * *"
```

**b. Passer sur un moniteur externe** (§4), qui ne change rien au quota Render
mais tient un rythme plus régulier — vous pourrez alors espacer sans risquer la
mise en veille.

## 4. La limite de ce montage : les crons GitHub sont approximatifs

GitHub déclenche les workflows planifiés **au mieux**, et les retarde
régulièrement de plusieurs minutes aux heures chargées. Le cron est réglé sur
10 minutes précisément pour garder 5 minutes de marge sous le seuil de 15 — mais
un retard exceptionnel dépassant cette marge laissera passer une mise en veille,
donc un réveil lent pour le visiteur suivant.

**C'est une réduction du problème, pas une suppression.** En pratique la
proportion de visiteurs tombant sur un service froid passe de « presque tous » à
« rarement », ce qui est l'essentiel de l'écart.

Deux autres pièges à connaître :

- GitHub **désactive les workflows planifiés d'un dépôt sans activité pendant
  60 jours**. Les scrapers commitant leur état à chaque relevé, ce dépôt reste
  actif — mais si vous les arrêtez, le ping s'arrêtera aussi, silencieusement.
- Le workflow ne fait jamais échouer son job, même quand le backend ne répond
  pas. Un mail d'échec toutes les dix minutes apprendrait surtout à ignorer les
  mails de ce dépôt. Les incidents se lisent dans les avertissements de l'onglet
  Actions.

### Alternative plus fiable : UptimeRobot

Gratuit, intervalle de 5 minutes, déclenchement régulier — et il vous alerte en
plus quand le backend est réellement tombé, ce que le workflow ne fait pas.

1. Compte gratuit sur [uptimerobot.com](https://uptimerobot.com).
2. **Add New Monitor** → type `HTTP(s)`.
3. URL : `https://matchalert-backend.onrender.com/api/health`
4. **Monitoring Interval** : 5 minutes.
5. Alertes sur votre adresse email.

Si vous l'adoptez, supprimez `keep-warm.yml` ou espacez son cron : les deux
feraient le même travail en double.

## 5. Pourquoi `/api/health` et pas une autre route

L'endpoint ne touche **pas** la base de données, et c'est délibéré :

- Le pool de connexions est plafonné à 5 par le quota du pooler Supabase (voir
  `application.properties` côté backend). En consommer une toutes les dix
  minutes pour ne rien en faire serait un mauvais échange.
- Un moniteur externe pointe dessus. S'il dépendait de la base, un hoquet de
  Supabase déclencherait une alerte alors que le service répond — et le réveil,
  seul but du ping, aurait quand même eu lieu.

C'est donc une sonde de **vivacité**, pas de disponibilité. Vérifier que la base
répond relèverait d'un endpoint distinct, à ne pas brancher sur le ping.

Le réveil en arrière-plan de `matcha-watch.yml` pointe désormais aussi sur
`/api/health`. Il visait `/api/matchas`, qui n'est pas un chemin mappé (seuls
`/all`, `/details` et `/overview` le sont) : il réveillait donc le conteneur en
récoltant un 404. Ce réveil ne sert plus que de filet, pour le cas où
`keep-warm.yml` serait désactivé.
