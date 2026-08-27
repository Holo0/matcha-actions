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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Dependance manquante. Lancez : pip install requests")

# Socle partage par les trois scrapers : modele de donnees, comparaison a l'etat
# precedent, exports, push vers le backend, notification, options de CLI
# communes. Voir matcha_common.py pour la frontiere exacte.
from matcha_common import (
    Product,
    Variant,
    add_common_arguments,
    api_timestamp,
    apply_only_filter,
    availability_payload,
    catalog_payload,
    diff,
    finish_run,
    run_cli,
    snapshot,
    wanted_from,
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE = "https://global.tokichi.jp"
COLLECTION_URL = f"{BASE}/collections/matcha/products.json"

# Identifiant du magasin cote backend (table `magasin`, migration
# V8__magasin_tokichi.sql). Envoye explicitement sur chaque ligne : le backend
# refuse desormais en 400 un relevé qui n'en porte pas.
MAGASIN_ID = "2"

# Nom affiche dans le sujet des alertes.
SHOP_NAME = "Nakamura Tokichi"

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

    payload = availability_payload(products, "2026-08-14T10:13:00", MAGASIN_ID)
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

    catalog = catalog_payload(products, MAGASIN_ID)
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

    wanted = wanted_from(args)
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

    return finish_run(products, args, magasin=MAGASIN_ID, shop_name=SHOP_NAME)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Releve le stock des matcha Nakamura Tokichi (catalogue Shopify public).")
    add_common_arguments(ap, only_example="ukishima,fuji", default_state=DEFAULT_STATE)
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    return run_cli(args, lambda: run(args))


if __name__ == "__main__":
    sys.exit(main())
