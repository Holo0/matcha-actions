#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_ippodo.py — releve de stock de la boutique Ippodo Tea (Kyoto).

Shopify public, aucune connexion : la collection « matcha » rend tout le
catalogue en une requete. Le socle Shopify partage (matcha_shopify.py) porte le
telechargement, la lecture de `available`, le push et l'auto-test commun ; ce
fichier ne declare que ce qui est propre a Ippodo.

CE QUI EST PROPRE A IPPODO. Aucun de ses produits n'utilise les options
Shopify : chaque fiche n'a qu'une variante « Default Title » et le
conditionnement vit dans le TITRE — « Sayaka-no-mukashi 100g Bag », « Kanza 20g
Box ». La taille se lit donc en fin de titre, pas en variante.

Le piege du catalogue est « Matcha To-Go Packets (2g x 10 packets) » : le seul
titre ou un chiffre suivi de « g » ne designe pas le conditionnement vendu mais
la dose d'un sachet. L'expression de matcha_shopify exige un blanc AVANT le
chiffre, ce que la parenthese empeche : ce produit retombe donc sur la taille
« Unique », ce qui est exact — il se vend en une seule presentation.

Usage rapide
------------
    pip install requests
    python3 matcha_watch_ippodo.py --self-test     # aucun reseau
    python3 matcha_watch_ippodo.py --only sayaka --no-push --verbose

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
    split_title_size,
)

SHOP = ShopifyShop(
    magasin="4",
    shop_name="Ippodo Tea",
    base="https://global.ippodo-tea.co.jp",
    collections=("matcha",),
    state_file=Path("ippodo_state.json"),
    only_example="sayaka,ummon",
    # /meta.json declare JPY. Le montant de products.json est deja en yens :
    # voir l'avertissement de format_price, verifie sur og:price:amount.
    currency="JPY",
    size_sources=("title",),
)


# Extrait reel de /collections/matcha/products.json, releve en investigation.
# Retenu pour les quatre formes de titre du catalogue — sac, boite, grand
# format, et le cas « (2g x 10 packets) » sans conditionnement lisible — plus un
# produit force en rupture : aucun ne l'etait au moment d'ecrire ce script, et
# un auto-test qui ne voit jamais de rupture ne prouve rien.
FIXTURE_CATALOG = {
    "products": [
        {
            "id": 1, "handle": "sayaka-no-mukashi-100g-bag",
            "title": "Sayaka-no-mukashi 100g Bag", "product_type": "Matcha",
            "tags": ["Matcha", "b_100g", "Matcha Large Packs"],
            "variants": [{"id": 11, "title": "Default Title", "sku": "5017357",
                          "price": "11000", "available": True}],
        },
        {
            "id": 2, "handle": "kanza-20g-box",
            "title": "Kanza 20g Box", "product_type": "Matcha",
            "tags": ["Matcha", "Koicha"],
            "variants": [{"id": 12, "title": "Default Title", "sku": "5038731",
                          "price": "10000", "available": True}],
        },
        {
            "id": 3, "handle": "uji-shimizu-400g-bag",
            "title": "Uji-Shimizu 400g Bag", "product_type": "Matcha",
            "tags": ["Matcha", "Sugared tea"],
            "variants": [{"id": 13, "title": "Default Title", "sku": "5017857",
                          "price": "3200", "available": True}],
        },
        {
            "id": 4, "handle": "matcha-to-go-packets",
            "title": "Matcha To-Go Packets (2g x 10 packets)", "product_type": "Matcha",
            "tags": ["Matcha", "Packets"],
            "variants": [{"id": 14, "title": "Default Title", "sku": "5031031",
                          "price": "1300", "available": True}],
        },
        {
            "id": 5, "handle": "ummon-no-mukashi-20g-box",
            "title": "Ummon-no-mukashi 20g Box", "product_type": "Matcha",
            "tags": ["Matcha", "Boxes"],
            "variants": [{"id": 15, "title": "Default Title", "sku": "5017157",
                          "price": "4500", "available": False}],
        },
    ]
}


def self_test() -> int:
    check = Checker()
    products = parse_fixture(FIXTURE_CATALOG, SHOP)

    check(len(products) == 5, "les 5 produits de la fixture sont parses",
          f"{len(products)} produit(s)")

    par_nom = {p.name: p for p in products}

    sayaka = par_nom.get("Sayaka-no-mukashi")
    check(sayaka is not None and sayaka.variants[0].label == "100g Bag",
          "taille lue en fin de titre, retiree du nom",
          f"nom/taille incorrects : {sorted(par_nom)}")
    check(sayaka is not None and sayaka.variants[0].price_jpy == "¥11,000",
          "prix en yens repris tel quel, sans division par cent",
          f"prix incorrect : {sayaka.variants[0].price_jpy if sayaka else None!r}")
    check(sayaka is not None and sayaka.variants[0].price_usd is None,
          "aucun montant range dans le champ dollar pour une boutique en yens",
          "devise melangee")
    check(sayaka is not None
          and sayaka.url == "https://global.ippodo-tea.co.jp/products/sayaka-no-mukashi-100g-bag",
          "URL construite depuis le handle", f"URL incorrecte : {sayaka.url if sayaka else None!r}")

    check("Kanza" in par_nom and par_nom["Kanza"].variants[0].label == "20g Box",
          "boite de 20 g reconnue comme conditionnement", "taille de la boite incorrecte")
    check("Uji-Shimizu" in par_nom and par_nom["Uji-Shimizu"].variants[0].label == "400g Bag",
          "grand format 400 g reconnu", "taille du grand format incorrecte")

    # Le cas piegeux du catalogue : le « 2g » est une dose par sachet, pas le
    # conditionnement vendu. Le nom doit rester entier.
    togo = par_nom.get("Matcha To-Go Packets (2g x 10 packets)")
    check(togo is not None and togo.variants[0].label == "Unique",
          "« (2g x 10 packets) » n'est pas pris pour une taille",
          f"taille inventee : {sorted(par_nom)}")

    check(split_title_size("Hatsu-mukashi 20g Box") == ("Hatsu-mukashi", "20g Box"),
          "separation nom/taille sur un titre nu", "separation incorrecte")
    check(split_title_size("Kuon") == ("Kuon", None),
          "titre sans taille laisse le nom entier", "separation incorrecte sans taille")

    ummon = par_nom.get("Ummon-no-mukashi")
    check(ummon is not None and ummon.variants[0].in_stock is False,
          "available=false lu comme rupture", "rupture non detectee")

    check_shared_invariants(check, products, SHOP)
    return check.report()


main = build_main(
    SHOP, self_test,
    description="Releve le stock des matcha Ippodo Tea (catalogue Shopify public).")


if __name__ == "__main__":
    sys.exit(main())
