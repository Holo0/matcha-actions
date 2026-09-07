#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_kettl.py — releve de stock de la boutique Kettl.

Shopify public, aucune connexion. Le socle partage (matcha_shopify.py) porte
tout le commun ; ce fichier ne declare que ce qui est propre a Kettl.

CE QUI EST PROPRE A KETTL. C'est la seule des six boutiques Shopify a utiliser
les options Shopify COMME PREVU : la taille est une vraie variante — « 20g »,
« 40g », « 100g », « 1kg » — et un meme produit en porte souvent plusieurs avec
des etats de stock differents (« Hinata Matcha » : 40 g disponible, 1 kg en
rupture). La taille se lit donc en variante, jamais dans le titre.

Deux consequences a ne pas perdre de vue :

  * C'EST LE CATALOGUE LE PLUS UTILE A SURVEILLER. Une cinquantaine de
    references, dont la moitie reellement en rupture a tout moment : c'est la
    que les alertes de restock ont le plus de matiere.
  * LES ABONNEMENTS SONT ECARTES (`product_type` « Subscription matcha »). Un
    abonnement affiche `available: false` en permanence tant que le marchand ne
    rouvre pas les inscriptions ; le laisser passer produirait un faux restock
    le jour de la reouverture et polluerait le catalogue en attendant.

Kettl ecrit son `product_type` tantot « Matcha » tantot « matcha » selon la
fiche : la comparaison du socle est faite sans casse, sinon le filtre
d'abonnement laisserait passer une fiche sur deux.

Usage rapide
------------
    pip install requests
    python3 matcha_watch_kettl.py --self-test     # aucun reseau
    python3 matcha_watch_kettl.py --only kiwami,tsubaki --no-push --verbose

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
    magasin="6",
    shop_name="Kettl",
    base="https://kettl.co",
    collections=("matcha-green-tea",),
    state_file=Path("kettl_state.json"),
    only_example="kiwami,shirakawa",
    currency="USD",
    # La seule boutique du lot ou cette source suffit : toutes les fiches
    # portent une variante nommee par son poids.
    size_sources=("variant",),
    exclude_types=frozenset({"subscription matcha"}),
)


# Extrait reel de /collections/matcha-green-tea/products.json. Retenu pour le
# multi-tailles avec etats divergents, la casse flottante de product_type,
# l'abonnement a ecarter, et un « 1KG » en majuscules — le catalogue melange
# « 1kg » et « 1KG » d'une fiche a l'autre.
FIXTURE_CATALOG = {
    "products": [
        {
            "id": 1, "handle": "kiwami-matcha", "title": "Kiwami Matcha",
            "product_type": "Matcha", "tags": ["matcha"],
            "variants": [{"id": 11, "title": "20g", "sku": "183954372093",
                          "price": "80.00", "available": False}],
        },
        {
            # Deux tailles, deux etats : le cas que le modele doit rendre
            # taille par taille et non produit par produit.
            "id": 2, "handle": "shirakawa-legacy-matcha-blend",
            "title": "Shirakawa Legacy Matcha Blend",
            "product_type": "Matcha", "tags": ["matcha"],
            "variants": [
                {"id": 12, "title": "100g", "sku": "022971156784",
                 "price": "95.00", "available": True},
                {"id": 13, "title": "1KG", "sku": "022971435063",
                 "price": "900.00", "available": False},
            ],
        },
        {
            "id": 3, "handle": "hinata-matcha", "title": "Hinata Matcha",
            "product_type": "Matcha", "tags": ["matcha"],
            "variants": [
                {"id": 14, "title": "40g", "sku": "022974266100",
                 "price": "45.00", "available": True},
                {"id": 15, "title": "1kg", "sku": "022974266101",
                 "price": "820.00", "available": False},
            ],
        },
        {
            # product_type en minuscules : meme fiche, autre casse.
            "id": 4, "handle": "tenkuu-matcha", "title": "Tenkuu Matcha",
            "product_type": "matcha", "tags": ["matcha"],
            "variants": [{"id": 16, "title": "20g", "sku": "022974266268",
                          "price": "38.00", "available": True}],
        },
        {
            # Abonnement : doit disparaitre du relevé.
            "id": 5, "handle": "new-horizons-matcha-subscription",
            "title": "New Horizons Matcha Subscription",
            "product_type": "Subscription matcha", "tags": [],
            "variants": [
                {"id": 17, "title": "Single Selection", "sku": "022978366995",
                 "price": "38.00", "available": False},
                {"id": 18, "title": "Two Selections", "sku": "022972680325",
                 "price": "68.00", "available": False},
            ],
        },
    ]
}


def self_test() -> int:
    check = Checker()
    products = parse_fixture(FIXTURE_CATALOG, SHOP)

    check(len(products) == 4, "l'abonnement est ecarte, les 4 matcha sont gardes",
          f"{len(products)} produit(s) : {[p.name for p in products]}")
    check(all("Subscription" not in p.name for p in products),
          "aucun abonnement dans le relevé",
          "un abonnement a survecu au filtre malgre la casse de product_type")

    par_nom = {p.name: p for p in products}

    shirakawa = par_nom.get("Shirakawa Legacy Matcha Blend")
    check(shirakawa is not None and [v.label for v in shirakawa.variants] == ["100g", "1KG"],
          "les deux tailles sont lues depuis les variantes, dans l'ordre du marchand",
          f"tailles incorrectes : {[v.label for v in shirakawa.variants] if shirakawa else None}")
    # Le coeur du modele : un produit partiellement disponible ne doit pas etre
    # resume a « disponible », sinon le retour du 1 kg ne declencherait rien.
    check(shirakawa is not None
          and shirakawa.variants[0].in_stock is True
          and shirakawa.variants[1].in_stock is False,
          "etats divergents conserves taille par taille",
          "les etats des deux tailles ont ete confondus")
    check(shirakawa is not None and shirakawa.name == "Shirakawa Legacy Matcha Blend",
          "le titre reste entier quand la taille vient de la variante",
          f"nom tronque : {shirakawa.name if shirakawa else None!r}")

    check("Tenkuu Matcha" in par_nom,
          "fiche au product_type en minuscules conservee",
          "la casse de product_type a fait ecarter un vrai matcha")

    kiwami = par_nom.get("Kiwami Matcha")
    check(kiwami is not None and kiwami.variants[0].price_usd == "$80.00",
          "prix en dollars, avec sa devise",
          f"prix incorrect : {kiwami.variants[0].price_usd if kiwami else None!r}")
    check(kiwami is not None and kiwami.variants[0].sku == "183954372093",
          "SKU repris : c'est lui qui identifie la taille dans l'etat",
          "SKU manquant")

    # Sans SKU distinct, deux tailles d'un meme produit partageraient une cle
    # d'etat et l'une masquerait l'autre au diff.
    cles = {v.key for p in products for v in p.variants}
    check(len(cles) == sum(len(p.variants) for p in products),
          "chaque taille a une cle d'etat distincte",
          "deux tailles partagent la meme cle d'etat")

    check_shared_invariants(check, products, SHOP)
    return check.report()


main = build_main(
    SHOP, self_test,
    description="Releve le stock des matcha Kettl (catalogue Shopify public).")


if __name__ == "__main__":
    sys.exit(main())
