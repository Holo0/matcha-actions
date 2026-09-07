#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_mizuba.py — releve de stock de la boutique Mizuba Tea Co.

Shopify public, aucune connexion. Le socle partage (matcha_shopify.py) porte
tout le commun ; ce fichier ne declare que ce qui est propre a Mizuba.

CE QUI EST PROPRE A MIZUBA, et c'est le cas le plus tordu des six. La boutique
n'ecrit le conditionnement NI en variante NI dans le titre : « Yama Matcha
Green Tea » ne dit pas son poids, et sa fiche n'a qu'un « Default Title ». Le
poids vit dans les TAGS — « 30g », « 40g », « 100g » — melange a des tags qui
n'ont rien d'un poids (« Ceremonial Grade », « Single Cultivar », « verishop »).

D'ou deux sources de taille, essayees dans cet ordre :

  1. la VARIANTE quand elle existe reellement. « Organic Signature Matcha » est
     la seule fiche a proposer deux formats (« 100 gram », « 50 gram ») : c'est
     la source la plus sure, elle porte l'etat de stock propre a chaque format.
  2. le TAG de poids, sinon. L'expression du socle est volontairement stricte —
     le tag doit n'etre QU'un poids — pour que « Matcha Large Packs » ne
     devienne jamais un conditionnement.

Une fiche sans variante ni tag de poids retombe sur « Unique ». C'est exact :
le marchand ne vend alors ce matcha que d'une seule facon.

LES COFFRETS SONT ECARTES, et pas par gout du menage. Leurs variantes ne
nomment pas un poids mais un CHOIX — « Yama Matcha », « Speckled Chawan »,
« Set Only ». Les laisser passer ferait entrer « Speckled Chawan » dans la
colonne `size` du backend, donc dans la cle metier (magasin, nom, size) et dans
le texte des alertes. Les mots ecartes couvrent les coffrets, bundles, trios et
lots (« 5-pack »), en plus des types « Gift » et « Tea Sets ».

Usage rapide
------------
    pip install requests
    python3 matcha_watch_mizuba.py --self-test     # aucun reseau
    python3 matcha_watch_mizuba.py --only shirakawa --no-push --verbose

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
    size_from_tags,
)

SHOP = ShopifyShop(
    magasin="7",
    shop_name="Mizuba Tea Co.",
    base="https://mizubatea.com",
    collections=("matcha-green-tea",),
    state_file=Path("mizuba_state.json"),
    only_example="shirakawa,yorokobi",
    currency="USD",
    size_sources=("variant", "tag"),
    exclude_types=frozenset({"gift", "tea sets"}),
    # « set », « bundle », « trio », « 5-pack » : voir l'en-tete. Ce ne sont pas
    # des matcha vendus au poids, et leurs variantes nommeraient un chawan.
    exclude_title=("subscription", "set", "bundle", "trio", "5-pack"),
)


# Extrait reel de /collections/matcha-green-tea/products.json. Retenu pour les
# trois chemins de taille (variante reelle, tag de poids, aucun des deux), un
# coffret dont la variante nomme une ceramique, et un abonnement.
FIXTURE_CATALOG = {
    "products": [
        {
            # Taille par TAG : le titre ne dit pas le poids.
            "id": 1, "handle": "yama-matcha-green-tea",
            "title": "Yama Matcha Green Tea", "product_type": "Matcha Green Tea",
            "tags": ["30g", "Ceremonial Grade", "Matcha", "Mizuba Tea Co.", "Usucha"],
            "variants": [{"id": 11, "title": "Default Title", "sku": "864",
                          "price": "33.00", "available": True}],
        },
        {
            # Taille par VARIANTE : la seule fiche a deux formats.
            "id": 2, "handle": "organic-signature-matcha",
            "title": "Organic Signature Matcha", "product_type": "Matcha Green Tea",
            "tags": ["100g", "Lattes", "Matcha", "Organic"],
            "variants": [
                {"id": 12, "title": "100 gram", "sku": "901",
                 "price": "72.00", "available": True},
                {"id": 13, "title": "50 gram", "sku": "902",
                 "price": "42.00", "available": False},
            ],
        },
        {
            # Ni variante reelle ni tag de poids : « Unique ».
            "id": 3, "handle": "yorokobi-organic-matcha-green-tea",
            "title": "Yorokobi Organic Matcha Green Tea",
            "product_type": "Matcha Green Tea",
            "tags": ["Ceremonial Grade", "Organic"],
            "variants": [{"id": 14, "title": "Default Title", "sku": "577",
                          "price": "48.00", "available": True}],
        },
        {
            "id": 4, "handle": "gokou-matcha", "title": "Gokou Matcha",
            "product_type": "Matcha Green Tea",
            "tags": ["Ceremonial Grade", "Koicha", "Single Cultivar"],
            "variants": [{"id": 15, "title": "Default Title", "sku": "970",
                          "price": "60.00", "available": False}],
        },
        {
            # Coffret : sa variante nomme une ceramique, pas un poids.
            "id": 5, "handle": "matcha-gift-set-wolf-ceramics",
            "title": "Matcha Gift Set with Wolf Ceramics Chawan",
            "product_type": "Gift", "tags": ["40g", "Gift", "Sets"],
            "variants": [
                {"id": 16, "title": "Speckled Chawan", "sku": "G1",
                 "price": "150.00", "available": False},
                {"id": 17, "title": "Striped Chawan", "sku": "G2",
                 "price": "150.00", "available": True},
            ],
        },
        {
            # Coffret dont le product_type ne trahit rien : seul le titre le dit.
            "id": 6, "handle": "daily-matcha-and-hojicha-set",
            "title": "Daily Matcha and Hojicha Set", "product_type": "Tea & Infusions",
            "tags": [],
            "variants": [{"id": 18, "title": "Default Title", "sku": "967",
                          "price": "49.00", "available": True}],
        },
        {
            "id": 7, "handle": "shirakawa-matcha-subscription",
            "title": "Shirakawa Matcha Subscription", "product_type": "",
            "tags": ["30g", "Ceremonial Grade"],
            "variants": [{"id": 19, "title": "Default Title", "sku": "S1",
                          "price": "55.00", "available": False}],
        },
    ]
}


def self_test() -> int:
    check = Checker()
    products = parse_fixture(FIXTURE_CATALOG, SHOP)

    check(len(products) == 4, "coffrets et abonnement ecartes, les 4 matcha gardes",
          f"{len(products)} produit(s) : {[p.name for p in products]}")
    # Le vrai risque du catalogue Mizuba : une ceramique dans la colonne size.
    tailles = {v.label for p in products for v in p.variants}
    check(not {"Speckled Chawan", "Striped Chawan"} & tailles,
          "aucun nom de ceramique n'a atteint la colonne des tailles",
          f"tailles polluees : {sorted(tailles)}")
    check(all("Set" not in p.name and "Subscription" not in p.name for p in products),
          "ni coffret ni abonnement dans le relevé",
          f"produits inattendus : {[p.name for p in products]}")

    par_nom = {p.name: p for p in products}

    yama = par_nom.get("Yama Matcha Green Tea")
    check(yama is not None and yama.variants[0].label == "30g",
          "taille lue depuis le tag de poids",
          f"taille incorrecte : {yama.variants[0].label if yama else None!r}")

    signature = par_nom.get("Organic Signature Matcha")
    check(signature is not None and [v.label for v in signature.variants] == ["100 gram", "50 gram"],
          "la variante reelle a priorite sur le tag de poids",
          f"tailles incorrectes : {[v.label for v in signature.variants] if signature else None}")
    # Le tag « 100g » de cette fiche ne doit PAS ecraser la variante « 50 gram » :
    # les deux formats ont des etats de stock differents.
    check(signature is not None
          and signature.variants[0].in_stock is True
          and signature.variants[1].in_stock is False,
          "etats des deux formats conserves separement",
          "les etats des deux formats ont ete confondus")

    yorokobi = par_nom.get("Yorokobi Organic Matcha Green Tea")
    check(yorokobi is not None and yorokobi.variants[0].label == "Unique",
          "fiche sans variante ni tag de poids affectee a « Unique »",
          f"taille inventee : {yorokobi.variants[0].label if yorokobi else None!r}")

    # L'expression du socle doit rester stricte : sans cela, « Matcha Large
    # Packs » ou « Single Cultivar » deviendraient des conditionnements.
    check(size_from_tags(["Ceremonial Grade", "Matcha Large Packs", "verishop"]) is None,
          "un tag qui n'est pas qu'un poids n'est pas pris pour une taille",
          "un tag descriptif a ete lu comme un conditionnement")
    check(size_from_tags(["Organic", "1kg", "Matcha"]) == "1kg",
          "tag de poids reconnu au milieu de tags descriptifs",
          "tag de poids manque")

    gokou = par_nom.get("Gokou Matcha")
    check(gokou is not None and gokou.variants[0].in_stock is False,
          "rupture reelle du catalogue lue comme telle", "rupture non detectee")
    check(yama is not None and yama.variants[0].price_usd == "$33.00",
          "prix en dollars, avec sa devise",
          f"prix incorrect : {yama.variants[0].price_usd if yama else None!r}")

    check_shared_invariants(check, products, SHOP)
    return check.report()


main = build_main(
    SHOP, self_test,
    description="Releve le stock des matcha Mizuba Tea Co. (catalogue Shopify public).")


if __name__ == "__main__":
    sys.exit(main())
