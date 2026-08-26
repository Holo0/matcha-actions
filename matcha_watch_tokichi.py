#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_tokichi.py — releve de stock de la boutique Nakamura Tokichi.

Contrairement a Marukyu-Koyamaen (matcha_watch.py), aucune connexion n'est
necessaire : la boutique tourne sur Shopify, qui expose un catalogue public en
JSON donnant directement, pour chaque variante, sa disponibilite — le meme
champ que le theme utilise lui-meme pour griser le bouton "Sold out" sur le
site. Une seule requete recupere tout le catalogue matcha, sans boucle de
telechargement fiche par fiche.

Chaque produit Shopify n'a ici qu'une seule variante ("Default Title") : il
n'y a pas de vraie option de taille cote Shopify, le poids est encode dans le
titre du produit lui-meme, ex. "Matcha Ukishima-no-Shiro, 30g Can". Nom et
taille sont donc separes sur la DERNIERE virgule du titre — verifie sur les
20 produits du catalogue, cas limite "Matcha Starter,100g Bag 2-bag set"
(sans espace apres la virgule) compris.

Usage rapide
------------
    pip install requests
    python3 matcha_watch_tokichi.py --self-test     # aucun reseau
    python3 matcha_watch_tokichi.py --only wako,yugen --no-push --verbose

Peut aussi ecrire le releve dans la base MatchAlert (memes secrets que
matcha_watch.py) : definissez MATCHALERT_API_URL et SCRAPPER_API_KEY. Sans
elles, rien n'est envoye. --no-push desactive l'envoi meme si configure.

Codes de sortie : 0 = rien de neuf | 2 = restock detecte | 1 = erreur.
"""

from __future__ import annotations

import argparse
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
except ImportError:  # pragma: no cover
    sys.exit("Dependance manquante. Lancez : pip install requests")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE = "https://global.tokichi.jp"
COLLECTION_URL = f"{BASE}/collections/matcha/products.json"

# Identifiant du magasin cote backend (table `magasin`, migration
# V8__magasin_tokichi.sql). matcha_watch.py (Marukyu) n'envoie pas encore ce
# champ et compte sur le repli serveur a '1' ; ce script l'envoie explicitement.
MAGASIN_ID = "2"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

DEFAULT_STATE = Path("tokichi_state.json")

# Plafond Shopify par page de l'API products.json ; le catalogue matcha
# actuel tient sur une seule page (20 produits), la pagination reste geree
# par precaution si le catalogue grossit.
PAGE_LIMIT = 250


# --------------------------------------------------------------------------- #
# Modele — memes formes que matcha_watch.py, pour un contrat identique.
# --------------------------------------------------------------------------- #

@dataclass
class Variant:
    label: str
    in_stock: bool | None          # None = indeterminable
    price_jpy: str | None = None
    price_eur: str | None = None
    sku: str | None = None
    variation_id: str | None = None
    limit: str | None = None

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


# --------------------------------------------------------------------------- #
# Catalogue Shopify
# --------------------------------------------------------------------------- #

def split_name_size(title: str) -> tuple[str, str]:
    """Separe nom et taille sur la DERNIERE virgule du titre produit.

    Le poids est encode dans le titre ("Nom, 30g Can"), jamais dans une
    variante Shopify reelle sur ce catalogue. Sans virgule, le titre entier
    devient le nom et la taille est marquee "Unique" plutot que d'inventer
    une valeur.
    """
    if "," in title:
        nom, _, size = title.rpartition(",")
        return nom.strip(), size.strip()
    return title.strip(), "Unique"


def parse_product(raw: dict[str, Any]) -> Product:
    handle = raw.get("handle", "")
    url = f"{BASE}/products/{handle}"
    nom, size_from_title = split_name_size(raw.get("title", ""))

    variants: list[Variant] = []
    for v in raw.get("variants", []):
        variant_title = (v.get("title") or "").strip()
        # "Default Title" = pas de vraie option Shopify sur ce catalogue : la
        # taille vient alors du titre du produit lui-meme.
        size = variant_title if variant_title and variant_title != "Default Title" else size_from_title

        price = v.get("price")
        price_jpy = f"¥{int(price) / 100:,.0f}" if price is not None else None

        available = v.get("available")
        variants.append(Variant(
            label=size,
            in_stock=bool(available) if available is not None else None,
            price_jpy=price_jpy,
            sku=v.get("sku") or None,
            variation_id=str(v["id"]) if v.get("id") is not None else None,
        ))

    return Product(name=nom, url=url, variants=variants)


def fetch_catalog(session: requests.Session, *, verbose: bool = False) -> list[Product]:
    products: list[Product] = []
    page = 1
    while True:
        resp = session.get(COLLECTION_URL, params={"limit": PAGE_LIMIT, "page": page}, timeout=30)
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        if not batch:
            break
        products.extend(parse_product(raw) for raw in batch)
        if verbose:
            print(f"[info] page {page} : {len(batch)} produit(s)")
        if len(batch) < PAGE_LIMIT:
            break
        page += 1
    return products


def apply_only_filter(products: list[Product], wanted: list[str]) -> list[Product]:
    """Meme philosophie que select_targets() dans matcha_watch.py : si le
    filtre ne matche rien, on retombe sur le catalogue complet plutot que de
    surveiller silencieusement zero produit.
    """
    if not wanted:
        return products
    kept = [p for p in products if any(w in p.name.lower() or w in p.url.lower() for w in wanted)]
    return kept or products


# --------------------------------------------------------------------------- #
# Etat local + diff (identique a matcha_watch.py)
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


# --------------------------------------------------------------------------- #
# Notifications (identique a matcha_watch.py)
# --------------------------------------------------------------------------- #

def notify(events: list[Event], *, verbose: bool = False) -> None:
    if not events:
        return
    body = "\n".join(e.line() for e in events)
    subject = f"Nakamura Tokichi : {len(events)} changement(s) de stock"

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
# MatchAlert — push vers le backend (identique a matcha_watch.py, magasin en plus)
# --------------------------------------------------------------------------- #

MATCHALERT_TIMEOUT = 60
MATCHALERT_ATTEMPTS = 3
LOG_DESCRIPTION_MAX = 1500


def api_timestamp(moment: datetime | None = None) -> str:
    """Meme format que matcha_watch.py : ISO local sans offset, en UTC — le
    backend trie `time` comme une chaine, un offset melangerait l'ordre.
    """
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def matchalert_config() -> tuple[str, str] | None:
    base = os.environ.get("MATCHALERT_API_URL", "").strip().rstrip("/")
    key = os.environ.get("SCRAPPER_API_KEY", "").strip()
    if not base or not key:
        return None
    if base.endswith("/api"):
        base = base[:-4]
    return base, key


def push_enabled(args) -> bool:
    return not args.no_push and matchalert_config() is not None


def availability_payload(products: Iterable[Product], stamp: str) -> list[dict[str, Any]]:
    """Comme matcha_watch.py, avec le magasin explicite dans chaque document :
    (magasin, nom, size) est la cle metier depuis le chantier multi-magasins.
    """
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            if v.in_stock is None:
                continue
            items[(p.name, v.label)] = {
                "magasin": MAGASIN_ID,
                "nom": p.name,
                "size": v.label,
                "time": stamp,
                "isAvailable": bool(v.in_stock),
            }
    return list(items.values())


def catalog_payload(products: Iterable[Product]) -> list[dict[str, Any]]:
    """Aplati le releve en lignes de catalogue (table matcha cote backend).

    Le couple (nom, size) est construit depuis les MEMES objets que
    availability_payload : c'est ce qui garantit que le backend retrouve le
    produit pour composer le lien d'achat de l'alerte de restock. Une taille
    indeterminable est conservee ici, contrairement aux disponibilites : elle
    n'affirme aucun etat de stock, elle rend juste le produit visible.
    """
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            items[(p.name, v.label)] = {
                "magasin": MAGASIN_ID,
                "nom": p.name,
                "size": v.label,
                "url": p.url,
            }
    return list(items.values())


def _matchalert_post(path: str, payload: Any, *, verbose: bool = False) -> tuple[bool, str]:
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
            if resp.status_code in (400, 401, 403):
                break
        if verbose:
            print(f"[warn] {path} essai {attempt}/{MATCHALERT_ATTEMPTS} : {detail}", file=sys.stderr)
        if attempt < MATCHALERT_ATTEMPTS:
            time.sleep(2 * attempt)

    return False, detail


def push_availabilities(products: list[Product], args) -> str:
    items = availability_payload(products, api_timestamp())
    if not items:
        return " ; base : rien a envoyer (aucune taille exploitable)"

    ok, detail = _matchalert_post("/api/matcha-availability/push-batch", items,
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


def push_catalog(products: list[Product], args) -> None:
    """Tient a jour le catalogue produit du magasin cote backend.

    Best effort et volontairement silencieux en cas d'echec : le catalogue ne
    sert qu'a l'affichage et au lien d'achat, alors que l'alerte de restock est
    la raison d'etre du script. Un backend indisponible ne doit pas empecher
    l'alerte de partir.
    """
    items = catalog_payload(products)
    if not items:
        return

    ok, detail = _matchalert_post("/api/matchas/push-catalog", items, verbose=args.verbose)
    if not ok:
        print(f"[warn] push catalogue echoue : {detail}", file=sys.stderr)
    elif args.verbose:
        print(f"[info] catalogue : {detail[:200]}")


def push_log(status: str, description: str, *, verbose: bool = False) -> bool:
    payload = {
        "status": status,
        "description": description[:LOG_DESCRIPTION_MAX],
        "time": api_timestamp(),
    }
    ok, detail = _matchalert_post("/api/scrapper/log", payload, verbose=verbose)
    if not ok:
        print(f"[warn] log MatchAlert echoue : {detail}", file=sys.stderr)
    elif verbose:
        print(f"[info] log MatchAlert enregistre ({status})")
    return ok


# --------------------------------------------------------------------------- #
# Sorties (identique a matcha_watch.py)
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
# Fixtures + auto-test (aucun reseau)
# --------------------------------------------------------------------------- #

# Extrait reel du catalogue (products.json), verifie en investigation. Inclut
# le cas limite "Matcha Starter,100g Bag 2-bag set" (sans espace apres la
# virgule) et un produit force en rupture (aucun ne l'est en conditions
# reelles au moment d'ecrire ce script).
FIXTURE_CATALOG = {
    "products": [
        {
            "handle": "mc1",
            "title": "Matcha Ukishima-no-Shiro, 30g Can",
            "variants": [
                {"id": 44231891583228, "title": "Default Title", "sku": "MC1",
                 "price": 150000, "available": True},
            ],
        },
        {
            "handle": "as400",
            "title": "Matcha Starter,100g Bag 2-bag set",
            "variants": [
                {"id": 1, "title": "Matcha Starter 100g Bag 2-bag set", "sku": "AS400",
                 "price": 1142800, "available": True},
            ],
        },
        {
            "handle": "mc99",
            "title": "Matcha Test-no-Rupture, 30g Can",
            "variants": [
                {"id": 2, "title": "Default Title", "sku": "MC99",
                 "price": 200000, "available": False},
            ],
        },
    ]
}


def self_test() -> int:
    failures = 0

    def check(cond: bool, ok: str, ko: str) -> None:
        nonlocal failures
        if cond:
            print(f"ok   {ok}")
        else:
            print(f"FAIL {ko}")
            failures += 1

    nom, size = split_name_size("Matcha Ukishima-no-Shiro, 30g Can")
    check(nom == "Matcha Ukishima-no-Shiro" and size == "30g Can",
          "nom/taille separes sur la derniere virgule",
          f"split incorrect : {nom!r} / {size!r}")

    nom2, size2 = split_name_size("Matcha Starter,100g Bag 2-bag set")
    check(nom2 == "Matcha Starter" and size2 == "100g Bag 2-bag set",
          "cas limite sans espace apres la virgule gere",
          f"split incorrect : {nom2!r} / {size2!r}")

    nom3, size3 = split_name_size("SansVirgule")
    check(nom3 == "SansVirgule" and size3 == "Unique",
          "repli sur 'Unique' quand le titre n'a pas de virgule",
          f"repli incorrect : {nom3!r} / {size3!r}")

    products = [parse_product(raw) for raw in FIXTURE_CATALOG["products"]]
    check(len(products) == 3, "les 3 produits de la fixture sont parses", f"{len(products)} produit(s)")

    p1 = products[0]
    check(p1.name == "Matcha Ukishima-no-Shiro" and p1.variants[0].label == "30g Can",
          "nom et taille corrects sur un produit reel", f"{p1.name!r} / {p1.variants[0].label!r}")
    check(p1.variants[0].in_stock is True, "available=true lu comme disponible", "disponibilite incorrecte")
    check(p1.variants[0].price_jpy == "¥1,500", "prix converti depuis les centimes Shopify",
          f"prix incorrect : {p1.variants[0].price_jpy!r}")
    check(p1.url == "https://global.tokichi.jp/products/mc1", "URL construite depuis le handle",
          f"URL incorrecte : {p1.url!r}")

    p3 = products[2]
    check(p3.variants[0].in_stock is False, "available=false lu comme rupture", "rupture non detectee")

    payload = availability_payload(products, "2026-08-14T10:13:00")
    check(len(payload) == 3, "chaque taille devient un document matchaAvailability",
          f"payload inattendu : {payload}")
    check(all(set(d) == {"magasin", "nom", "size", "time", "isAvailable"} for d in payload),
          "champs exactement conformes au modele MatchaAvailability (magasin inclus)",
          f"champs inattendus : {[sorted(d) for d in payload]}")
    check(all(d["magasin"] == MAGASIN_ID for d in payload),
          "magasin Tokichi envoye explicitement sur chaque document",
          "magasin manquant ou incorrect")

    ts = api_timestamp(datetime(2026, 8, 14, 10, 13, 0, tzinfo=timezone.utc))
    check(ts == "2026-08-14T10:13:00",
          "horodatage API sans offset (format LocalDateTime cote Java)",
          f"format inattendu : {ts!r}")

    catalog = catalog_payload(products)
    check(all(set(d) == {"magasin", "nom", "size", "url"} for d in catalog),
          "payload catalogue conforme au modele Matcha du backend",
          f"champs inattendus : {[sorted(d) for d in catalog]}")
    # Invariant critique : sans une correspondance exacte, le backend ne
    # retrouve pas le produit et l'alerte de restock repart sur l'URL de repli.
    check({(d["nom"], d["size"]) for d in catalog}
          >= {(d["nom"], d["size"]) for d in payload},
          "catalogue et disponibilites partagent exactement les memes (nom, size)",
          "divergence (nom, size) entre catalogue et disponibilites")

    prev = {"https://global.tokichi.jp/products/mc1#mc1": {
        "in_stock": False, "product": "Matcha Ukishima-no-Shiro", "variant": "30g Can",
        "price": None, "url": "https://global.tokichi.jp/products/mc1"}}
    cur = snapshot([p1])
    evs = diff(prev, cur, report_sold_out=False)
    check(len(evs) == 1 and evs[0].kind == "RESTOCK",
          "transition rupture -> dispo declenche un RESTOCK", f"diff incorrect : {evs}")
    check(not diff({}, cur, report_sold_out=False),
          "premier run silencieux (pas de fausse alerte)", "le premier run alerte a tort")

    kept = apply_only_filter(products, ["ukishima"])
    check(len(kept) == 1 and kept[0].name == "Matcha Ukishima-no-Shiro",
          "filtre --only applique correctement", f"{[p.name for p in kept]}")
    check(len(apply_only_filter(products, ["rien-qui-matche"])) == 3,
          "repli sur le catalogue complet si le filtre ne matche rien",
          "le filtre sans correspondance produirait une surveillance vide")

    print(f"\n{'TOUS LES TESTS PASSENT' if failures == 0 else f'{failures} ECHEC(S)'}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def run(args) -> tuple[int, str]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        products = fetch_catalog(session, verbose=args.verbose)
    except requests.RequestException as exc:
        print(f"\nErreur reseau : {exc}", file=sys.stderr)
        return 1, f"Erreur reseau : {exc}"
    except (ValueError, KeyError) as exc:
        print(f"\nReponse JSON inattendue : {exc}", file=sys.stderr)
        return 1, f"Reponse JSON inattendue : {exc}"

    wanted = [w.strip().lower() for w in args.only.split(",") if w.strip()]
    products = apply_only_filter(products, wanted)

    if not args.quiet:
        print(f"{len(products)} produit(s) au catalogue"
              + (f" (filtre : {', '.join(wanted)})" if wanted else ""))
        for p in products:
            dispo = [v.label for v in p.variants if v.in_stock]
            rupture = [v.label for v in p.variants if v.in_stock is False]
            etat = ("DISPO : " + ", ".join(dispo)) if dispo else "rupture"
            print(f"  {p.name:<46} {etat}")
            if rupture and dispo:
                print(f"{'':9}rupture : {', '.join(rupture)}")

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
    # que l'alerte parte.
    if push_enabled(args):
        # Le catalogue precede les disponibilites : sans lui, un produit
        # nouvellement apparu n'aurait pas de ligne matcha, donc pas de lien
        # d'achat dans l'alerte qui suit immediatement.
        push_catalog(products, args)
    db_note = push_availabilities(products, args) if push_enabled(args) else ""

    n_sizes = sum(len(p.variants) for p in products)
    summary = f"{len(products)} produit(s), {n_sizes} taille(s) relevee(s)"

    if events:
        print("\n" + "=" * 64)
        for e in events:
            print(e.line())
        print("=" * 64)
        notify(events, verbose=args.verbose)
        detail = " ; ".join(f"{e.kind} {e.product} {e.variant}" for e in events)
        summary = f"{summary} ; {len(events)} changement(s) : {detail}{db_note}"
        return (2 if any(e.kind == "RESTOCK" for e in events) else 0), summary

    if not args.quiet:
        print("\nAucun changement")
    return 0, f"{summary} ; aucun changement{db_note}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Releve le stock des matcha Nakamura Tokichi (catalogue Shopify public).")
    ap.add_argument("--only", default="",
                    help="filtre par nom, separe par des virgules (ex: ukishima,fuji)")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--json", type=Path, help="ecrit le releve complet en JSON")
    ap.add_argument("--csv", type=Path, help="ecrit le releve complet en CSV")
    ap.add_argument("--sold-out-too", action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="ne rien envoyer a MatchAlert, meme si l'API est configuree")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="teste le parsing hors ligne puis quitte")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.verbose and not push_enabled(args):
        print("[info] MatchAlert desactive : "
              + ("--no-push" if args.no_push else
                 "MATCHALERT_API_URL / SCRAPPER_API_KEY non definis"))

    code, summary = run(args)

    if push_enabled(args):
        push_log("SUCCESS" if code in (0, 2) else "ERROR", summary, verbose=args.verbose)

    return code


if __name__ == "__main__":
    sys.exit(main())
