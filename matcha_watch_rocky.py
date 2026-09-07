#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_rocky.py — releve de stock de la boutique Rocky's Matcha.

Shopify public, aucune connexion. Le socle partage (matcha_shopify.py) porte
tout le commun ; ce fichier ne declare que ce qui est propre a Rocky's.

CE QUI EST PROPRE A ROCKY'S. Deux choses, et les deux touchent au NOM, donc a
la cle metier (magasin, nom, size) cote backend :

  1. TOUS les titres sont prefixes de la marque — « rocky's matcha Ceremonial
     Blend Matcha 20g ». Garder le prefixe ferait lire « RESTOCK rocky's matcha
     Ceremonial Blend Matcha chez Rocky's Matcha » dans l'alerte. Il est donc
     retire, et l'expression accepte l'apostrophe droite ET la courbe : le
     catalogue melange les deux, parfois dans le meme produit.
  2. Les KITS de theiere (`product_type` « Matcha Kit ») sont ecartes. Un kit
     part en rupture comme le reste, mais ce n'est pas du matcha : un abonne
     qui suit « Ceremonial Blend » ne veut pas etre reveille pour un fouet.

Le conditionnement vit en fin de titre (« ... Matcha 20g », « ... Houjicha
100g »), jamais en variante : toutes les fiches n'ont qu'un « Default Title ».
Les produits sans poids — les sachets unitaires, les bundles — retombent donc
sur la taille « Unique », ce qui est exact.

Usage rapide
------------
    pip install requests
    python3 matcha_watch_rocky.py --self-test     # aucun reseau
    python3 matcha_watch_rocky.py --only okumidori --no-push --verbose

Codes de sortie : 0 = rien de neuf | 2 = restock detecte | 1 = erreur.
"""

from __future__ import annotations

import sys
from pathlib import Path

from matcha_shopify import (
    Checker,
    ShopifyShop,
    build_main,
    check_shared_invariants,
    parse_fixture,
)

SHOP = ShopifyShop(
    magasin="5",
    shop_name="Rocky's Matcha",
    base="https://rockysmatcha.com",
    collections=("matcha",),
    state_file=Path("rocky_state.json"),
    only_example="okumidori,koshun",
    currency="USD",
    size_sources=("title",),
    # L'apostrophe courbe (U+2019) autant que la droite : les deux apparaissent
    # dans le catalogue, et un prefixe non retire creerait un DEUXIEME nom pour
    # le meme produit le jour ou le marchand change de caractere.
    strip_prefix=r"^rocky[’'ʼ]s\s+matcha\s+",
    exclude_types=frozenset({"matcha kit"}),
)


# Extrait reel de /collections/matcha/products.json. Retenu pour les deux
# formes d'apostrophe, un produit sans poids dans le titre, un kit a ecarter, et
# les deux etats de stock — plusieurs references etaient reellement en rupture
# au moment du releve, elles sont reprises telles quelles.
FIXTURE_CATALOG = {
    "products": [
        {
            "id": 1, "handle": "ceremonial-blend-matcha-20g",
            "title": "rocky's matcha Ceremonial Blend Matcha 20g",
            "product_type": "Matcha", "tags": ["Matcha"],
            "variants": [{"id": 11, "title": "Default Title",
                          "sku": "RM-MATCHA-CEREMONIAL-20",
                          "price": "36.00", "available": True}],
        },
        {
            "id": 2, "handle": "ceremonial-blend-matcha-100g",
            "title": "rocky's matcha Ceremonial Blend Matcha 100g",
            "product_type": "Matcha", "tags": ["Matcha"],
            "variants": [{"id": 12, "title": "Default Title",
                          "sku": "RM-MATCHA-CEREMONIAL-100",
                          "price": "140.00", "available": True}],
        },
        {
            # Apostrophe COURBE, et un « for X » qui suit le prefixe.
            "id": 3, "handle": "dreamin-man-ceremonial-blend-20g",
            "title": "rocky’s matcha for Dreamin’ Man Ceremonial Blend Matcha 20g",
            "product_type": "Matcha", "tags": ["Matcha"],
            "variants": [{"id": 13, "title": "Default Title",
                          "sku": "RM-MATCHA-DREAMIN-20",
                          "price": "30.00", "available": True}],
        },
        {
            "id": 4, "handle": "tsujiki-ceremonial-blend-matcha-100g",
            "title": "rocky's matcha Tsujiki Ceremonial Blend Matcha 100g",
            "product_type": "Matcha", "tags": ["Matcha"],
            "variants": [{"id": 14, "title": "Default Title",
                          "sku": "RM-MATCHA-TSUJIKI-100",
                          "price": "120.00", "available": False}],
        },
        {
            # Aucun poids dans le titre : taille « Unique », pas une invention.
            "id": 5, "handle": "daily-ceremonial-single-serve",
            "title": "rocky’s matcha Daily Ceremonial Blend Single Serve Packets",
            "product_type": "Matcha", "tags": ["Matcha"],
            "variants": [{"id": 15, "title": "Default Title",
                          "sku": "RM-MATCHA-STICK-DAILY-CEREMONIAL-30",
                          "price": "32.00", "available": True}],
        },
        {
            # Kit de theiere : doit disparaitre du relevé.
            "id": 6, "handle": "essential-tea-kit",
            "title": "rocky's matcha Essential Tea Kit",
            "product_type": "Matcha Kit", "tags": ["Matcha"],
            "variants": [{"id": 16, "title": "Default Title", "sku": "RM-KIT-ESSENTIAL",
                          "price": "110.00", "available": True}],
        },
    ]
}


def self_test() -> int:
    check = Checker()
    products = parse_fixture(FIXTURE_CATALOG, SHOP)

    check(len(products) == 5, "le kit est ecarte, les 5 matcha sont gardes",
          f"{len(products)} produit(s) : {[p.name for p in products]}")
    check(all("Kit" not in p.name for p in products),
          "aucun kit de theiere dans le relevé", "un kit a survecu au filtre")

    par_cle = {(p.name, p.variants[0].label): p for p in products}

    check(("Ceremonial Blend Matcha", "20g") in par_cle,
          "prefixe de marque retire, taille lue en fin de titre",
          f"cles obtenues : {sorted(par_cle)}")
    check(("Ceremonial Blend Matcha", "100g") in par_cle,
          "le meme nom porte deux tailles distinctes",
          "les deux formats ne se distinguent pas")
    # Sans cette distinction, les deux formats s'ecraseraient l'un l'autre :
    # (magasin, nom, size) est la cle metier, un nom identique ne suffit pas.
    check(len({k for k in par_cle if k[0] == "Ceremonial Blend Matcha"}) == 2,
          "20 g et 100 g restent deux lignes separees",
          "les deux formats se sont confondus")

    check(("for Dreamin’ Man Ceremonial Blend Matcha", "20g") in par_cle,
          "apostrophe courbe reconnue par le retrait de prefixe",
          f"prefixe non retire : {sorted(par_cle)}")

    check(("Daily Ceremonial Blend Single Serve Packets", "Unique") in par_cle,
          "produit sans poids affecte a la taille « Unique »",
          "taille inventee pour un produit sans poids")

    tsujiki = par_cle.get(("Tsujiki Ceremonial Blend Matcha", "100g"))
    check(tsujiki is not None and tsujiki.variants[0].in_stock is False,
          "rupture reelle du catalogue lue comme telle", "rupture non detectee")

    ceremonial = par_cle.get(("Ceremonial Blend Matcha", "20g"))
    check(ceremonial is not None and ceremonial.variants[0].price_usd == "$36.00",
          "prix en dollars, avec sa devise",
          f"prix incorrect : {ceremonial.variants[0].price_usd if ceremonial else None!r}")
    check(ceremonial is not None and ceremonial.variants[0].price_jpy is None,
          "aucun montant range dans le champ yen pour une boutique en dollars",
          "devise melangee")

    check_shared_invariants(check, products, SHOP)
    return check.report()


main = build_main(
    SHOP, self_test,
    description="Releve le stock des matcha Rocky's Matcha (catalogue Shopify public).")


if __name__ == "__main__":
    sys.exit(main())
