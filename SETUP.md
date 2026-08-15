# Installation sur GitHub Actions — 5 minutes, une seule fois

À la fin, le relevé tourne toutes les heures dans le cloud. Vous ne lancez plus rien.

---

## 1. Créer un dépôt **privé**

Sur github.com → **New repository** → nom au choix → cochez **Private** → Create.

Privé, parce que le dépôt va contenir l'historique de vos relevés. Vos identifiants,
eux, ne sont jamais dans le dépôt : ils vivent dans les *secrets* GitHub, chiffrés et
invisibles même pour vous une fois enregistrés.

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
il faut créer le fichier à la main via **Add file → Create new file** en tapant le chemin
`.github/workflows/matcha-watch.yml`.

## 3. Enregistrer les identifiants

**Settings → Secrets and variables → Actions → New repository secret**, deux fois :

| Nom | Valeur |
|---|---|
| `MKY_USER` | l'email du compte Marukyu-Koyamaen |
| `MKY_PASS` | le mot de passe |

## 4. Choisir les produits (facultatif)

Même écran, onglet **Variables** → New repository variable :

| Nom | Valeur |
|---|---|
| `MATCHA_ONLY` | `wako,yugen,isuzu,aoarashi` |

Sans cette variable, ces quatre-là sont surveillés par défaut. Mettez `all` ou supprimez
la variable et modifiez le workflow pour tout suivre — mais lisez d'abord la section
quota plus bas.

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
- `POST /api/scrapper/log` — une ligne `SUCCESS` ou `ERROR` avec le résumé du run, dans la
  collection `logs`. Y compris quand le relevé échoue : c'est là que se verront une session
  expirée ou un blocage anti-bot, sans avoir à ouvrir l'onglet Actions.

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
| `4 fiches à relever` puis une ligne par produit | tout fonctionne |
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

---

## 9. Quota et coût

Gratuit, mais pas illimité sur un dépôt privé : **2 000 minutes par mois**.

| Configuration | Consommation estimée |
|---|---|
| 4 produits, toutes les heures | ~1 400 min/mois — **tient** |
| 4 produits + créneaux denses (lignes commentées du workflow) | ~1 700 min/mois — tient, mais serré |
| Tout le catalogue (47 fiches), toutes les heures | très au-delà — **ne tient pas** |

GitHub facture à la minute entamée, d'où l'écart avec le temps réel d'exécution.

Si vous voulez tout le catalogue ou une fréquence plus élevée, passez le dépôt en
**public** : les minutes deviennent illimitées. Vos identifiants restent protégés — les
secrets ne sont jamais exposés, y compris aux forks. En revanche l'historique des
relevés et les issues de restock deviennent visibles de tous.

## 10. Les limites à connaître

**L'horaire dérive.** GitHub met les crons en file d'attente : un run prévu à 13 peut
partir à 25 ou 35, surtout aux heures chargées. Comptez 5 à 20 minutes de retard. Sur un
produit qui s'épuise en 12 minutes, ça se paie. La minute 13 plutôt que 0 limite la casse,
parce que la file est saturée en début d'heure.

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
