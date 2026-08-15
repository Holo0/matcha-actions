# matcha_watch — relevé de stock Marukyu-Koyamaen

Script autonome à lancer toutes les heures. Relève l'état de stock **taille par taille**
du catalogue matcha, garde un historique, et ne signale que les changements.

Le parsing est calé sur la **structure réelle du site**, vérifiée sur une page
enregistrée en session authentifiée — pas sur des hypothèses.

---

## 1. Pourquoi la connexion est obligatoire

La boutique affiche `You must register and login to shop.` aux visiteurs anonymes.
Déconnecté, la page ne contient ni le détail des tailles, ni leur disponibilité :
seul le cas « produit entièrement en rupture » reste visible. Un scraper anonyme ne peut
donc distinguer que *« tout est en rupture »* de *« quelque chose est disponible »* —
jamais **quelle** taille.

Le script ouvre donc une vraie session et la conserve dans un fichier de cookies entre
deux exécutions : une seule authentification, pas une par heure.

---

## 2. Ce que le site expose réellement (et pourquoi c'est piégeux)

Le thème n'utilise **pas** le mécanisme WooCommerce habituel. Il n'y a pas d'attribut
`data-product_variations` contenant le JSON des variantes, et pas de menu déroulant de
tailles. Chaque taille est une ligne rendue côté serveur :

```html
<form class="variations_form cart" data-product_id="22817" data-product_title="Kiwami Choan">
  <div class="product-form-row woocommerce-variation" data-variation_id="22822">
    <dl class="pa pa-sku"><dt>SKU</dt><dd>1G36020C1</dd></dl>
    <dl class="pa pa-size"><dt>Size</dt><dd>20g can</dd></dl>
    <span class="woocs_price_JPY">¥12,600</span>
    <span class="woocs_price_EUR">€68.50</span>
    <p class="stock out-of-stock">Out of stock</p>
    <button class="single_add_to_cart_button" disabled>Add to cart</button>
  </div>
```

Une taille **disponible** ressemble à ceci — et c'est là que se cache le vrai piège :

```html
<p class="stock in-stock sold-individually amount-2">Limit one per person</p>
<div class="quantity sold_individually"><input class="qty" name="quantity[size]" readonly value="1"></div>
<button class="single_add_to_cart_button" type="submit">Add to cart</button>
```

Le libellé affiché n'est **pas** `In stock` mais `Limit one per person`. Un parseur qui
cherche le texte « In stock » conclurait à une rupture sur une taille pourtant achetable :
faux négatif silencieux, exactement le cas qu'on ne veut pas rater. Le script se fie donc
aux **classes CSS** (`in-stock` / `out-of-stock`), jamais au texte.

Cinq conséquences pour le parsing :

- le nom du produit se lit dans `data-product_title` — le `<h1>` vaut littéralement
  `Product Detail`, inutilisable ;
- la disponibilité se lit sur les classes de `p.stock`, recoupée avec l'attribut
  `disabled` du bouton. Un bouton désactivé l'emporte toujours sur un libellé optimiste ;
- le texte de `p.stock` est récupéré à part comme **limite d'achat** et remonté dans les
  alertes et la colonne `purchase_limit` ;
- le champ quantité s'appelle `quantity[size]`, pas `quantity` ;
- les prix sont éclatés en plusieurs balises (`<span>€</span>68<small>.50</small>`) :
  il faut concaténer sans séparateur, sinon on obtient `€ 68 .50`.

Un parseur WooCommerce générique se serait planté sur ce site. Celui-ci gère la structure
réelle en priorité, avec le mécanisme standard en repli si le thème changeait un jour.

---

## 3. Installation

```bash
pip install requests beautifulsoup4
python3 matcha_watch.py --self-test     # 15 contrôles, aucun réseau
```

Les fixtures du `--self-test` sont le balisage **réel** de deux pages enregistrées en
session authentifiée : une entièrement épuisée, une avec une taille disponible. Les deux
branches sont donc couvertes par du vrai HTML.

Analyser une page enregistrée depuis le navigateur, sans rien envoyer :

```bash
python3 matcha_watch.py --parse-file "Low Caffeine Matcha.html"
```

```
Low Caffeine Matcha   (2 taille(s))
  DISPO    20g can        ¥2,200     €11.96     1F94020C1  [Limit one per person]
  rupture  40g can        ¥4,120     €22.40     1F94040C1
```

C'est l'outil de diagnostic à utiliser en premier si quelque chose cloche.

---

## 4. Identifiants

Lus dans l'environnement, jamais écrits dans le code.

```bash
export MKY_USER='votre.email@exemple.com'
export MKY_PASS='votre-mot-de-passe'
```

Pour du permanent :

```bash
printf 'MKY_USER=…\nMKY_PASS=…\n' > ~/.matcha.env && chmod 600 ~/.matcha.env
```

**Variante sans mot de passe.** Connectez-vous dans le navigateur, exportez les cookies du
domaine `marukyu-koyamaen.co.jp` au format Netscape (extension « Get cookies.txt »),
enregistrez-les sous `matcha_cookies.txt` à côté du script. Il réutilisera la session sans
jamais voir votre mot de passe. À refaire quand elle expire.

Le script teste l'authentification en regardant si une fiche produit expose ses variantes —
c'est le seul signal fiable, le site n'exposant pas de lien de déconnexion exploitable.

---

## 5. Utilisation

```bash
# vos quatre références, avec exports
python3 matcha_watch.py --only wako,yugen,isuzu,aoarashi --csv stock.csv --json stock.json

# tout le catalogue, silencieux (pour cron)
python3 matcha_watch.py --quiet
```

Le **premier run** n'alerte jamais : il enregistre l'état de référence dans
`matcha_state.json`. Ensuite, seules les transitions `rupture → disponible` remontent.
`--sold-out-too` ajoute les passages en rupture.

**Codes de sortie** : `0` rien de neuf · `2` restock détecté · `1` erreur.

---

## 6. Notifications

Sans configuration, tout s'affiche sur la sortie standard.

| Variable | Effet |
|---|---|
| `MATCHA_WEBHOOK_URL` | POST vers Slack, Discord ou ntfy.sh (détecté automatiquement) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` | serveur d'envoi |
| `MAIL_TO`, `MAIL_FROM` | destinataire et expéditeur |

Le plus rapide à mettre en place, pour du push mobile sans créer de compte :

```bash
export MATCHA_WEBHOOK_URL='https://ntfy.sh/un-nom-secret-a-vous'
```

---

## 7. Écrire dans la base MatchAlert

Optionnel, et inactif tant que les deux variables ne sont pas définies :

| Variable | Effet |
|---|---|
| `MATCHALERT_API_URL` | URL du backend, ex. `https://api.matchalert.fr` |
| `SCRAPPER_API_KEY` | clé attendue dans le header `X-API-KEY`, identique au backend |

Le script appelle alors deux endpoints, tous deux protégés par cette seule clé :

`POST /api/matcha-availability/push-batch` reçoit l'état de chaque taille sous la forme
`{nom, size, time, isAvailable}`. Le dédoublonnage est fait **côté backend** : il relit la
dernière entrée `(nom, size)` et n'écrit que si la disponibilité a changé. Le script envoie
donc l'état complet à chaque run plutôt que son propre diff — une seule source de vérité,
et un état perdu (push refusé, cache vidé) se rattrape tout seul au run suivant.

`POST /api/scrapper/log` reçoit `{status, description, time}` : une ligne `SUCCESS` ou
`ERROR` par exécution, résumé compris. Elle part **aussi** quand le relevé échoue, ce qui
en fait le seul endroit où voir une session expirée sans ouvrir l'onglet Actions.

Deux détails qui ont dicté l'implémentation :

- **Les tailles indéterminables ne sont jamais poussées.** Quand ni `p.stock` ni le bouton
  ne permettent de trancher, `in_stock` vaut `None`. L'envoyer en `false` ferait croire à
  une rupture, puis à un restock au run suivant : une alerte mensongère à tous les abonnés.
  Ces tailles sont écartées du lot — mieux vaut une donnée absente qu'une donnée fausse.
- **Le champ `time` part sans fuseau.** Firestore trie `time` avec `orderBy(DESCENDING)` sur
  une *chaîne* : le tri est lexicographique. Un `2026-08-14T10:13:00+02:00` mélangé au
  `2026-08-14T10:13:00` que le backend génère par défaut casserait l'ordre, donc la
  détection de changement. Le script convertit en UTC et supprime l'offset, dans le même
  format que `LocalDateTime.toString()` côté Java. Les exports CSV et JSON, eux, gardent
  l'heure locale avec offset : ils sont faits pour être lus, pas triés par Firestore.

`--no-push` désactive les deux appels sans toucher aux variables — pratique pour un run de
test qui ne doit rien écrire.

## 8. Planification horaire

> **Installation retenue : GitHub Actions.** La procédure complète est dans
> **[SETUP.md](SETUP.md)** — dépôt privé, secrets, premier run de diagnostic.
> Rien à installer, aucune machine à laisser allumée. Les recettes ci-dessous
> ne servent que si vous rapatriez un jour le relevé sur une machine à vous.

**Linux / macOS — cron** (`crontab -e`) :

```cron
17 * * * * cd /chemin/vers/le/script && set -a && . ~/.matcha.env && set +a && /usr/bin/python3 matcha_watch.py --quiet >> matcha.log 2>&1
```

Une minute quelconque (`17`) plutôt que `0` : tout le monde planifie à l'heure ronde,
et ça se repère côté serveur.

**Windows — Planificateur de tâches** : déclencheur « toutes les heures »,
action `python.exe`, arguments `C:\chemin\matcha_watch.py --quiet`,
`MKY_USER` / `MKY_PASS` dans les variables d'environnement utilisateur.

---

## 9. Ce que le script ne fait pas

Il **ne contourne aucune protection anti-bot**. Face à une page de vérification
(Cloudflare, captcha) il s'arrête, le dit, et n'insiste pas. Par défaut : `User-Agent`
réaliste, pause aléatoire de 4 à 9 s entre deux fiches, backoff exponentiel, respect de
`Retry-After` sur `429` / `503`.

Quatre produits toutes les heures ≈ 100 requêtes/jour, négligeable. Tout le catalogue
≈ 1 100/jour : jouable, mais gardez `--delay` généreux. Si des `429` apparaissent :
`--delay 10-20` et réduisez avec `--only`.

---

## 10. État de validation

| Cas | Statut |
|---|---|
| Taille en rupture | validé sur HTML réel (Kiwami Choan) |
| Taille disponible | validé sur HTML réel (Low Caffeine Matcha, 20g) |
| Libellé non standard (`Limit one per person`) | validé — lu comme disponible |
| Limite d'achat capturée | validé |
| Page visiteur non connecté | validé |
| Produit entièrement en rupture | validé |
| Diff rupture → disponible | validé |
| Mapping vers `matchaAvailability` (champs, types) | validé par `--self-test` |
| Taille indéterminable écartée du push | validé par `--self-test` |
| Format `time` compatible avec le tri Firestore | validé par `--self-test` |
| **Appels HTTP réels vers le backend** | **non validé** — testés sur un serveur factice uniquement |
| **Connexion automatique** | **non validé** — voir ci-dessous |

La seule pièce non éprouvée est le POST de connexion : je n'ai jamais vu la page
`/english/shop/account` en HTML brut, donc les noms de champs sont détectés
dynamiquement plutôt que connus. C'est robuste par construction, mais ça reste à
confirmer au premier run réel :

```bash
python3 matcha_watch.py --verbose --only "low caffeine"
```

Si la connexion échoue, deux issues : enregistrez la page `account` et envoyez-la
(même méthode que pour les fiches produit), ou passez simplement par l'export de
cookies décrit en section 4, qui contourne entièrement le problème.

---

## 11. Dépannage

| Symptôme | Cause probable |
|---|---|
| `Session non connectée` | identifiants absents/faux, ou cookies expirés → `--verbose` |
| `Connexion refusée : …` | le message d'erreur du site est repris tel quel |
| `Page de vérification anti-bot` | trop de requêtes → augmentez `--delay`, attendez |
| `structure de page non reconnue` | le thème a changé → `--parse-file` sur la page en cause |
| toutes les tailles en `?` | ni `p.stock` ni bouton trouvés dans la ligne → envoyez-moi la page |
| `push MatchAlert echoue : HTTP 401` | `SCRAPPER_API_KEY` différente de celle du backend |
| `push MatchAlert echoue : HTTP 500` | côté backend, `SCRAPPER_API_KEY` non défini au démarrage |
| `push MatchAlert echoue : ConnectionError` | backend injoignable depuis les runners GitHub (URL, pare-feu) |
| relevé correct mais rien en base | disponibilités inchangées : le backend n'écrit que sur transition |

Le formulaire de connexion est analysé **dynamiquement** (tous les champs cachés, nonce
compris) : un renommage de champ côté site ne cassera pas le script.
