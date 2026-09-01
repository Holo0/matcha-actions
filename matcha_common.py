#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Socle commun aux scrapers MatchAlert.

Chaque boutique a sa propre facon d'exposer son stock — session authentifiee et
HTML rendu cote serveur chez Marukyu-Koyamaen, catalogue JSON Shopify chez
Nakamura Tokichi, page categorie a parser chez Les Thes sur Terre. Tout ce qui
suit le relevé, en revanche, est identique : le modele de donnees, la detection
de changement d'etat, les exports, l'envoi vers le backend, la notification.

C'etait recopie a l'identique dans les trois scripts : 16 fonctions et 3 classes
structurellement identiques, soit environ 800 lignes de triplication. Une
correction de bug devait donc etre appliquee trois fois, et une quatrieme
boutique aurait recopie le tout une quatrieme fois.

Repartition retenue :

    matcha_common.py   modele, etat, exports, push, notification, CLI partagee
    matcha_watch*.py   configuration de la boutique, obtention des produits,
                       affichage, auto-test du parsing

La frontiere est « une fois les produits connus » : au-dessus, chaque boutique
fait a sa maniere ; en dessous, tout est commun. C'est ce qui permet a
`finish_run()` de porter les 40 dernieres lignes des trois `run()`.

Rien ici ne connait de boutique en particulier : `magasin` et `shop_name` sont
toujours passes en parametre, jamais lus dans une constante de module. Un defaut
implicite ferait attribuer un relevé a la mauvaise boutique — exactement le
genre d'erreur que le backend refuse desormais en 400.
"""
from __future__ import annotations

import csv
import json
import os
import smtplib
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:                                    # pragma: no cover
    sys.exit("Dependance manquante : pip install requests")


# --------------------------------------------------------------------------- #
# Reglages du dialogue avec le backend                                        #
# --------------------------------------------------------------------------- #

# 3 tentatives : le backend tourne sur une instance Render du plan gratuit, qui
# peut etre en train de se reveiller au moment du push.
MATCHALERT_ATTEMPTS = 3

# 60 s : un reveil a froid prend une cinquantaine de secondes.
MATCHALERT_TIMEOUT = 60

# La description du log est stockee en base ; on la borne pour ne pas y ecrire
# un relevé entier quand le resume enumere beaucoup de changements.
LOG_DESCRIPTION_MAX = 1500


# --------------------------------------------------------------------------- #
# Modele                                                                      #
# --------------------------------------------------------------------------- #

@dataclass
class Variant:
    label: str
    in_stock: bool | None          # None = indeterminable
    price_jpy: str | None = None
    price_eur: str | None = None
    sku: str | None = None
    variation_id: str | None = None
    limit: str | None = None       # ex. "Limit one per person"

    @property
    def key(self) -> str:
        return (self.sku or self.label).strip().lower()


@dataclass
class Product:
    name: str
    url: str
    variants: list[Variant] = field(default_factory=list)
    note: str = ""

    @property
    def any_in_stock(self) -> bool:
        return any(v.in_stock for v in self.variants)


@dataclass
class Event:
    kind: str          # "RESTOCK" ou "SOLD_OUT"
    product: str
    variant: str
    price: str | None
    url: str
    limit: str | None = None

    def line(self) -> str:
        tag = "RESTOCK " if self.kind == "RESTOCK" else "epuise  "
        price = f" — {self.price}" if self.price else ""
        lim = f"  [{self.limit}]" if self.limit else ""
        return f"{tag} {self.product} · {self.variant}{price}{lim}\n          {self.url}"


class ScrapeError(RuntimeError):
    """Structure de page meconnaissable : refonte du site, selecteur obsolete."""


# --------------------------------------------------------------------------- #
# Etat et detection de changement                                             #
# --------------------------------------------------------------------------- #

def snapshot(products: Iterable[Product]) -> dict[str, dict[str, Any]]:
    snap: dict[str, dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            snap[f"{p.url}#{v.key}"] = {
                "product": p.name,
                "variant": v.label,
                "in_stock": v.in_stock,
                "price": v.price_jpy,
                "price_eur": v.price_eur,
                "limit": v.limit,
                "url": p.url,
            }
    return snap


def diff(previous: dict[str, Any], current: dict[str, Any], *,
         report_sold_out: bool) -> list[Event]:
    events: list[Event] = []
    for key, now in current.items():
        before = previous.get(key)
        was = before.get("in_stock") if before else None
        is_ = now["in_stock"]
        if is_ is None or was == is_:
            continue
        if is_:
            if before is None:      # premier run : pas de fausse alerte
                continue
            price = " / ".join(x for x in (now.get("price"), now.get("price_eur")) if x)
            events.append(Event("RESTOCK", now["product"], now["variant"],
                                price or None, now["url"], now.get("limit")))
        elif was is True and report_sold_out:
            events.append(Event("SOLD_OUT", now["product"], now["variant"], None, now["url"]))
    return events


def apply_only_filter(products: list[Product], wanted: list[str]) -> list[Product]:
    """Restreint le relevé aux produits dont le nom OU l'url contient un des mots.

    L'url compte autant que le nom : le slug est souvent plus stable que
    l'intitule commercial, qu'un marchand peut reformuler du jour au lendemain.

    `wanted` doit deja etre en minuscules — la comparaison se fait contre un
    nom et une url minuscules, un mot capitalise ne correspondrait jamais.
    Utiliser `wanted_from(args)` pour l'obtenir plutot que de decouper --only
    a la main.

    Repli volontaire sur le catalogue complet si le filtre ne correspond a rien :
    une liste `--only` devenue obsolete doit degrader vers « tout surveiller »,
    pas vers « ne rien surveiller » — ce dernier cas ne remonterait aucune alerte
    sans que rien ne le signale.
    """
    if not wanted:
        return products
    kept = [p for p in products
            if any(w in p.name.lower() or w in p.url.lower() for w in wanted)]
    return kept or products


# --------------------------------------------------------------------------- #
# Exports                                                                     #
# --------------------------------------------------------------------------- #

def write_json(path: Path, products: list[Product], stamp: str) -> None:
    path.write_text(json.dumps(
        {"checked_at": stamp, "products": [asdict(p) for p in products]},
        ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, products: list[Product], stamp: str) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["checked_at", "product", "size", "in_stock",
                    "price_jpy", "price_eur", "purchase_limit", "sku", "url", "note"])
        for p in products:
            for v in p.variants:
                w.writerow([stamp, p.name, v.label,
                            "" if v.in_stock is None else ("yes" if v.in_stock else "no"),
                            v.price_jpy or "", v.price_eur or "", v.limit or "",
                            v.sku or "", p.url, p.note])


# --------------------------------------------------------------------------- #
# Notification                                                                #
# --------------------------------------------------------------------------- #

def notify(events: list[Event], shop_name: str, *, verbose: bool = False) -> None:
    """Webhook et/ou email, selon les variables d'environnement definies.

    `shop_name` est la seule chose qui distinguait les trois versions de cette
    fonction : il apparait dans le sujet, pour qu'une alerte dise de quelle
    boutique elle parle.
    """
    if not events:
        return
    body = "\n".join(e.line() for e in events)
    subject = f"{shop_name} : {len(events)} changement(s) de stock"

    hook = os.environ.get("MATCHA_WEBHOOK_URL")
    if hook:
        try:
            if "ntfy" in hook:
                requests.post(hook, data=body.encode(), timeout=15,
                              headers={"Title": "Restock matcha", "Priority": "high"})
            else:
                requests.post(hook, json={"text": f"*{subject}*\n```{body}```",
                                          "content": f"**{subject}**\n```{body}```"},
                              timeout=15)
            if verbose:
                print("[info] webhook envoye")
        except requests.RequestException as exc:
            print(f"[warn] webhook echoue : {exc}", file=sys.stderr)

    host, to_addr = os.environ.get("SMTP_HOST"), os.environ.get("MAIL_TO")
    if host and to_addr:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = os.environ.get("MAIL_FROM", to_addr)
        msg["To"] = to_addr
        msg.set_content(body)
        try:
            with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as smtp:
                smtp.starttls()
                u, p = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
                if u and p:
                    smtp.login(u, p)
                smtp.send_message(msg)
            if verbose:
                print("[info] email envoye")
        except Exception as exc:
            print(f"[warn] envoi email echoue : {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Envoi vers le backend MatchAlert                                            #
# --------------------------------------------------------------------------- #

def api_timestamp(moment: datetime | None = None) -> str:
    """Horodatage au format attendu par le backend.

    Le backend compare ce champ a l'ISO local que produit LocalDateTime cote
    Java. Un ISO avec offset ("2026-08-14T10:13:00+02:00") melange a un ISO sans
    offset casserait l'ordre chronologique — donc la detection de changement,
    donc les alertes. On envoie toujours un ISO local, sans offset, en UTC.
    """
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def matchalert_config() -> tuple[str, str] | None:
    """(url de base, cle API) si les deux sont definis, sinon None."""
    base = os.environ.get("MATCHALERT_API_URL", "").strip().rstrip("/")
    key = os.environ.get("SCRAPPER_API_KEY", "").strip()
    if not base or not key:
        return None
    # Les chemins ci-dessous incluent deja /api : on tolere une URL de base
    # renseignee avec ou sans, plutot que de produire un /api/api/... muet.
    if base.endswith("/api"):
        base = base[:-4]
    return base, key


def push_enabled(args) -> bool:
    return not args.no_push and matchalert_config() is not None


def availability_payload(products: Iterable[Product], stamp: str,
                         magasin: str) -> list[dict[str, Any]]:
    """Aplati le relevé en documents matchaAvailability.

    Les tailles dont l'etat est indeterminable (in_stock is None) sont
    ECARTEES. Les pousser en `false` ferait croire au backend a une rupture,
    puis a un restock au run suivant : une alerte mensongere envoyee a tous
    les abonnes. Une donnee absente vaut mieux qu'une donnee fausse.

    Doublon (nom, size) dans un meme lot : on ne garde que le dernier.

    `magasin` est un parametre obligatoire, jamais une constante de module :
    (magasin, nom, size) est la cle metier cote backend, et une ligne sans
    magasin y est refusee en 400.
    """
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            if v.in_stock is None:
                continue
            items[(p.name, v.label)] = {
                "magasin": magasin,
                "nom": p.name,
                "size": v.label,
                "time": stamp,
                "isAvailable": bool(v.in_stock),
            }
    return list(items.values())


def catalog_payload(products: Iterable[Product], magasin: str) -> list[dict[str, Any]]:
    """Aplati le relevé en lignes de catalogue (table matcha cote backend).

    Le couple (nom, size) est construit depuis les MEMES objets que
    availability_payload : c'est ce qui garantit que le backend retrouve le
    produit pour composer le lien d'achat de l'alerte de restock.

    Contrairement aux disponibilites, une taille indeterminable est CONSERVEE
    ici : elle n'affirme aucun etat de stock, elle rend juste le produit
    visible dans l'application.
    """
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            items[(p.name, v.label)] = {
                "magasin": magasin,
                "nom": p.name,
                "size": v.label,
                "url": p.url,
            }
    return list(items.values())


def matchalert_post(path: str, payload: Any, *, verbose: bool = False) -> tuple[bool, str]:
    cfg = matchalert_config()
    if cfg is None:
        return False, "MatchAlert non configure"
    base, key = cfg

    url = f"{base}{path}"
    headers = {"X-API-KEY": key, "Content-Type": "application/json"}
    detail = "aucune tentative"

    for attempt in range(1, MATCHALERT_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=MATCHALERT_TIMEOUT)
        except requests.RequestException as exc:
            detail = f"{type(exc).__name__} : {exc}"
        else:
            if resp.status_code < 300:
                return True, resp.text[:300]
            detail = f"HTTP {resp.status_code} — {resp.text[:200]}"
            # Cle refusee ou corps invalide : reessayer ne changera rien.
            if resp.status_code in (400, 401, 403):
                break
        if verbose:
            print(f"[warn] {path} essai {attempt}/{MATCHALERT_ATTEMPTS} : {detail}", file=sys.stderr)
        if attempt < MATCHALERT_ATTEMPTS:
            time.sleep(2 * attempt)

    return False, detail


def push_availabilities(products: list[Product], args, magasin: str) -> str:
    """Envoie l'etat de toutes les tailles. Retourne une note pour le log.

    N'echoue jamais le run : la base est une destination secondaire, l'alerte
    reste la fonction principale du script.
    """
    items = availability_payload(products, api_timestamp(), magasin)
    if not items:
        return " ; base : rien a envoyer (aucune taille exploitable)"

    ok, detail = matchalert_post("/api/matcha-availability/push-batch", items,
                                 verbose=args.verbose)
    if not ok:
        print(f"[warn] push MatchAlert echoue : {detail}", file=sys.stderr)
        return f" ; base : ECHEC ({detail[:200]})"

    saved = None
    try:
        saved = json.loads(detail).get("saved")
    except (json.JSONDecodeError, AttributeError):
        pass
    note = (f"{saved} ecriture(s) sur {len(items)} envoyee(s)"
            if saved is not None else f"{len(items)} envoyee(s)")
    if not args.quiet:
        print(f"[info] MatchAlert : {note}")
    return f" ; base : {note}"


def push_catalog(products: list[Product], args, magasin: str) -> None:
    """Tient a jour le catalogue produit du magasin cote backend.

    Best effort : un echec est journalise mais ne fait jamais echouer le run,
    l'alerte restant la fonction principale du script.
    """
    items = catalog_payload(products, magasin)
    if not items:
        return

    ok, detail = matchalert_post("/api/matchas/push-catalog", items, verbose=args.verbose)
    if not ok:
        print(f"[warn] push catalogue echoue : {detail}", file=sys.stderr)
    elif args.verbose:
        print(f"[info] catalogue : {detail[:200]}")


def push_log(status: str, description: str, *, magasin: str,
             verbose: bool = False) -> bool:
    """Trace le run dans la table scrapper_log. Best effort, jamais bloquant.

    `magasin` est obligatoire : sans lui, les trois scrapers ecrivent des lignes
    indiscernables et le backend ne peut pas voir qu'une boutique en particulier
    s'est tue. C'est exactement la panne que la supervision doit attraper, donc
    l'oubli ne doit pas etre possible.
    """
    payload = {
        "status": status,
        "description": description[:LOG_DESCRIPTION_MAX],
        "time": api_timestamp(),
        "magasin": magasin,
    }
    ok, detail = matchalert_post("/api/scrapper/log", payload, verbose=verbose)
    if not ok:
        print(f"[warn] log MatchAlert echoue : {detail}", file=sys.stderr)
    elif verbose:
        print(f"[info] log MatchAlert enregistre ({status})")
    return ok


# --------------------------------------------------------------------------- #
# Fin de relevé : exports, etat, push, notification                           #
# --------------------------------------------------------------------------- #

def finish_run(products: list[Product], args, *, magasin: str, shop_name: str,
               summary: str | None = None,
               detail_with_variant: bool = True) -> tuple[int, str]:
    """Tout ce qui suit l'obtention des produits, identique aux trois boutiques.

    Retourne (code de sortie, resume pour le log) : 0 rien de neuf, 2 restock
    detecte. Le resume est la description envoyee a /api/scrapper/log, il doit
    tenir en une ligne lisible.

    `summary` remplace le resume par defaut pour une boutique dont les produits
    n'ont qu'une taille — compter les tailles n'y voudrait rien dire.
    `detail_with_variant` retire la taille de l'enumeration des changements,
    pour la meme raison.

    Seul changement de comportement de l'extraction : le message « aucun
    changement » porte desormais l'horodatage pour les trois boutiques. Marukyu
    l'affichait deja, Tokichi et Les Thes non ; la version informative gagne,
    puisque c'est ce message qui remonte dans le resume de l'Action.
    """
    stamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    if args.json:
        write_json(args.json, products, stamp)
    if args.csv:
        write_csv(args.csv, products, stamp)

    current = snapshot(products)
    previous: dict[str, Any] = {}
    if args.state.exists():
        try:
            previous = json.loads(args.state.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print("[warn] etat precedent illisible, il sera reecrit", file=sys.stderr)

    events = diff(previous, current, report_sold_out=args.sold_out_too)
    args.state.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    # Le push precede la notification : si la base tombe, on veut quand meme
    # que l'alerte parte. L'inverse ferait rater le restock, qui est l'objet
    # meme du script.
    if push_enabled(args):
        # Le catalogue precede les disponibilites : sans lui, un produit
        # nouvellement apparu n'aurait pas de ligne matcha, donc pas de lien
        # d'achat dans l'alerte qui suit immediatement.
        push_catalog(products, args, magasin)
    db_note = push_availabilities(products, args, magasin) if push_enabled(args) else ""

    if summary is None:
        n_sizes = sum(len(p.variants) for p in products)
        summary = f"{len(products)} produit(s), {n_sizes} taille(s) relevee(s)"

    if events:
        print("\n" + "=" * 64)
        for e in events:
            print(e.line())
        print("=" * 64)
        notify(events, shop_name, verbose=args.verbose)
        if detail_with_variant:
            detail = " ; ".join(f"{e.kind} {e.product} {e.variant}" for e in events)
        else:
            detail = " ; ".join(f"{e.kind} {e.product}" for e in events)
        summary = f"{summary} ; {len(events)} changement(s) : {detail}{db_note}"
        return (2 if any(e.kind == "RESTOCK" for e in events) else 0), summary

    if not args.quiet:
        print(f"\nAucun changement ({stamp})")
    return 0, f"{summary} ; aucun changement{db_note}"


# --------------------------------------------------------------------------- #
# Ligne de commande                                                           #
# --------------------------------------------------------------------------- #

def add_common_arguments(ap, *, only_example: str, default_state: Path) -> None:
    """Options presentes a l'identique dans les trois scrapers."""
    ap.add_argument("--only", default="",
                    help=f"filtre par nom, separe par des virgules (ex: {only_example})")
    ap.add_argument("--state", type=Path, default=default_state)
    ap.add_argument("--json", type=Path, help="ecrit le relevé complet en JSON")
    ap.add_argument("--csv", type=Path, help="ecrit le relevé complet en CSV")
    ap.add_argument("--sold-out-too", action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="ne rien envoyer a MatchAlert, meme si l'API est configuree")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="teste le parsing hors ligne puis quitte")


def wanted_from(args) -> list[str]:
    """Mots du filtre --only, normalises en minuscules."""
    return [w.strip().lower() for w in args.only.split(",") if w.strip()]


def run_cli(args, run_fn, *, magasin: str) -> int:
    """Enchainement final de main() : relevé, puis journalisation du run.

    `run_fn` est appele sans argument et doit retourner (code, resume).

    ATTENTION : la journalisation n'a lieu qu'apres le retour de `run_fn`. Un
    script qui leve, un site injoignable ou un cron qui ne part pas n'ecrivent
    donc AUCUNE ligne — pas meme une ligne ERROR. C'est pourquoi la supervision
    cote backend (ScrapperHealthService) se fonde sur l'absence de succes
    recent et non sur la presence d'erreurs : chercher des lignes ERROR ne
    verrait jamais ces cas-la, qui sont les plus probables.
    """
    if args.verbose and not push_enabled(args):
        print("[info] MatchAlert desactive : "
              + ("--no-push" if args.no_push else
                 "MATCHALERT_API_URL / SCRAPPER_API_KEY non definis"))

    code, summary = run_fn()

    if push_enabled(args):
        push_log("SUCCESS" if code in (0, 2) else "ERROR", summary,
                 magasin=magasin, verbose=args.verbose)

    return code
