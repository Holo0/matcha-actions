#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_naoki.py — releve de stock de la boutique Naoki Matcha.

Shopify public, aucune connexion. Le socle partage (matcha_shopify.py) porte
tout le commun ; ce fichier ne declare que ce qui est propre a Naoki.

CE QUI EST PROPRE A NAOKI. Deux choses :

  1. LE MATCHA EST ECLATE SUR TROIS COLLECTIONS — « ceremonial-grade-matcha »,
     « masters-collection-matcha », « organic-matcha » — et un meme produit
     figure dans deux d'entre elles. Il n'existe pas de collection « matcha »
     unique chez ce marchand : elle repond bien 200, avec zero produit. Le
     socle deduplique par identifiant Shopify, sans quoi chaque produit partage
     produirait deux lignes de catalogue cote backend.
  2. LES TAILLES SONT EN ONCES, libellees « 1.4 Ounce (Pack of 1) ». Elles sont
     reprises TELLES QUELLES et non converties en grammes : ce libelle entre
     dans la cle metier (magasin, nom, size) et doit correspondre a ce que
     l'abonne lit sur la fiche du marchand. Une conversion maison ferait
     divergerle catalogue du site le jour ou l'arrondi change.

Usage rapide
------------
    pip install requests
    python3 matcha_watch_naoki.py --self-test     # aucun reseau
    python3 matcha_watch_naoki.py --only chiran --no-push --verbose

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
    parse_product,
)

SHOP = ShopifyShop(
    magasin="8",
    shop_name="Naoki Matcha",
    base="https://naokimatcha.com",
    collections=("ceremonial-grade-matcha", "masters-collection-matcha", "organic-matcha"),
    state_file=Path("naoki_state.json"),
    only_example="chiran,yame",
    currency="USD",
    size_sources=("variant",),
)


# Extrait reel des trois collections. Retenu pour le multi-tailles en onces, un
# produit sans option reelle, et une reference presente dans deux collections —
# le cas que la deduplication doit absorber.
FIXTURE_CATALOG = {
    "products": [
        {
            "id": 1, "handle": "superior-blend-matcha",
            "title": "Superior Blend Matcha", "product_type": "Grocery", "tags": [],
            "variants": [
                {"id": 11, "title": "1.4 Ounce (Pack of 1)", "sku": "O4-JD03-P0NA",
                 "price": "24.99", "available": True},
                {"id": 12, "title": "1.75 Ounce (Pack of 1)", "sku": "C6-77Y5-7EEI",
                 "price": "25.99", "available": True},
                {"id": 13, "title": "3.5 Ounce (Pack of 1)", "sku": "EL-68RD-SPOS",
                 "price": "39.99", "available": False},
            ],
        },
        {
            "id": 2, "handle": "organic-first-spring-blend-matcha",
            "title": "Organic First Spring Blend Matcha", "product_type": "Grocery",
            "tags": [],
            "variants": [
                {"id": 14, "title": "1.4 Ounce (Pack of 1)", "sku": "SC-O48D-GXB8",
                 "price": "25.99", "available": True},
                {"id": 15, "title": "3.5 Ounce (Pack of 1)", "sku": "3F-1GSB-YVJD",
                 "price": "42.99", "available": True},
            ],
        },
        {
            # Pas d'option Shopify, et un « (4x20g) » dans le titre que la
            # source « variant » n'ira pas chercher : taille « Unique ».
            "id": 3, "handle": "yame-single-cultivar-matcha-set",
            "title": "Yame Single-Cultivar Matcha Set (4x20g)", "product_type": "",
            "tags": [],
            "variants": [{"id": 16, "title": "Default Title", "sku": None,
                          "price": "85.00", "available": True}],
        },
        {
            "id": 4, "handle": "chiran-harvest-matcha",
            "title": "Chiran Harvest Matcha", "product_type": "Grocery", "tags": [],
            "variants": [{"id": 17, "title": "1.4 Ounce (Pack of 1)", "sku": "UZ-MVK5-ZMSS",
                          "price": "33.99", "available": False}],
        },
    ]
}


def self_test() -> int:
    check = Checker()
    products = parse_fixture(FIXTURE_CATALOG, SHOP)

    check(len(products) == 4, "les 4 produits de la fixture sont parses",
          f"{len(products)} produit(s)")

    par_nom = {p.name: p for p in products}

    superior = par_nom.get("Superior Blend Matcha")
    check(superior is not None
          and [v.label for v in superior.variants]
          == ["1.4 Ounce (Pack of 1)", "1.75 Ounce (Pack of 1)", "3.5 Ounce (Pack of 1)"],
          "les trois tailles en onces sont reprises telles quelles",
          f"tailles incorrectes : {[v.label for v in superior.variants] if superior else None}")
    check(superior is not None and superior.variants[2].in_stock is False,
          "rupture d'une seule des trois tailles conservee",
          "les etats des tailles ont ete confondus")
    check(superior is not None and superior.name == "Superior Blend Matcha",
          "le titre reste entier quand la taille vient de la variante",
          f"nom tronque : {superior.name if superior else None!r}")

    # Le « (4x20g) » du titre ne doit pas devenir une taille : la source
    # « variant » est la seule declaree, et la fiche n'a pas d'option reelle.
    coffret = par_nom.get("Yame Single-Cultivar Matcha Set (4x20g)")
    check(coffret is not None and coffret.variants[0].label == "Unique",
          "titre non consulte pour la taille chez ce marchand",
          f"taille inattendue : {coffret.variants[0].label if coffret else None!r}")
    check(coffret is not None and coffret.variants[0].sku is None,
          "SKU absent accepte sans casser le relevé", "SKU absent mal gere")
    # Sans SKU, la cle d'etat retombe sur le libelle de taille : elle doit
    # rester non vide, sinon deux produits partageraient une cle.
    check(coffret is not None and coffret.variants[0].key == "unique",
          "cle d'etat de repli construite depuis la taille",
          f"cle inattendue : {coffret.variants[0].key if coffret else None!r}")

    chiran = par_nom.get("Chiran Harvest Matcha")
    check(chiran is not None and chiran.variants[0].price_usd == "$33.99",
          "prix en dollars, avec sa devise",
          f"prix incorrect : {chiran.variants[0].price_usd if chiran else None!r}")

    # La deduplication est LE point de ce marchand : le meme produit revient
    # dans deux collections, et le compter deux fois doublerait ses lignes.
    doublon = dict(FIXTURE_CATALOG["products"][0])
    vus: dict[object, dict] = {}
    for raw in [*FIXTURE_CATALOG["products"], doublon]:
        vus.setdefault(raw["id"], raw)
    dedupliques = [p for p in (parse_product(raw, SHOP) for raw in vus.values()) if p]
    check(len(dedupliques) == 4,
          "un produit present dans deux collections ne compte qu'une fois",
          f"{len(dedupliques)} produit(s) apres deduplication")

    check_shared_invariants(check, products, SHOP)
    return check.report()


main = build_main(
    SHOP, self_test,
    description="Releve le stock des matcha Naoki Matcha (catalogue Shopify public).")


if __name__ == "__main__":
    sys.exit(main())
