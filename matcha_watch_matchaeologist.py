#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_matchaeologist.py — releve de stock de la boutique Matchaeologist.

Shopify public, aucune connexion. Le socle partage (matcha_shopify.py) porte
tout le commun ; ce fichier ne declare que ce qui est propre a Matchaeologist.

CE QUI EST PROPRE A MATCHAEOLOGIST, et c'est un piege qu'il faut nommer. La
boutique utilise les options Shopify, mais PAS pour un conditionnement : le
titre de variante est une NOTE DE DEGUSTATION — « Ambrosial with sweet umami
undertones », « Intense with a mellow roasted flavor ». Un scraper qui lirait
la variante comme les autres boutiques Shopify ecrirait cette phrase dans la
colonne `size` du backend, donc dans la cle metier (magasin, nom, size) et dans
le texte de chaque alerte.

La source « variant » n'est donc PAS declaree ici. Le poids vit dans le titre —
« Meiko™ Ceremonial Matcha 100g », « Maya™ Ceremonial Matcha 1kg » — et un meme
matcha se decline en 20 g, 100 g et 1 kg sur trois fiches distinctes, que la
lecture du titre separe correctement.

Le symbole ™ fait partie du nom et est CONSERVE : il apparait ainsi sur le site,
et le retirer creerait un deuxieme nom pour le meme produit.

Usage rapide
------------
    pip install requests
    python3 matcha_watch_matchaeologist.py --self-test     # aucun reseau
    python3 matcha_watch_matchaeologist.py --only meiko --no-push --verbose

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
    magasin="9",
    shop_name="Matchaeologist",
    base="https://www.matchaeologist.com",
    collections=("matcha",),
    state_file=Path("matchaeologist_state.json"),
    only_example="meiko,misaki",
    currency="USD",
    # « variant » VOLONTAIREMENT ABSENTE : voir l'en-tete, le titre de variante
    # est une note de degustation, pas une taille.
    size_sources=("title",),
)


# Extrait reel de /collections/matcha/products.json. Retenu pour les trois
# formats du meme matcha, la note de degustation en titre de variante, et une
# reference reellement en rupture au moment du releve.
FIXTURE_CATALOG = {
    "products": [
        {
            "id": 1, "handle": "misaki-ceremonial-matcha-20g",
            "title": "Misaki™ Ceremonial Matcha 20g", "product_type": "Matcha",
            "tags": ["meta-related-collection-featured"],
            "variants": [{"id": 11, "title": "Ambrosial with sweet umami undertones",
                          "sku": "MA-MISA20-01", "price": "28.00", "available": True}],
        },
        {
            "id": 2, "handle": "matsu-ceremonial-matcha-20g",
            "title": "Matsu™ Ceremonial Matcha 20g", "product_type": "Matcha",
            "tags": ["meta-related-collection-featured"],
            "variants": [{"id": 12, "title": "Intense with a mellow roasted flavor",
                          "sku": "MA-MATS20-01", "price": "22.00", "available": False}],
        },
        {
            "id": 3, "handle": "meiko-ceremonial-matcha-20g",
            "title": "Meiko™ Ceremonial Matcha 20g", "product_type": "Matcha",
            "tags": ["meta-related-collection-featured"],
            "variants": [{"id": 13, "title": "Full-bodied with subtle floral aromas",
                          "sku": "MA-MEIK20-01", "price": "20.00", "available": True}],
        },
        {
            "id": 4, "handle": "meiko-ceremonial-matcha-100g",
            "title": "Meiko™ Ceremonial Matcha 100g", "product_type": "Matcha",
            "tags": ["meta-related-collection-featured"],
            "variants": [{"id": 14, "title": "Full-bodied with subtle floral aromas",
                          "sku": "MA-MEIK100-01", "price": "85.00", "available": True}],
        },
        {
            "id": 5, "handle": "meiko-ceremonial-matcha-1kg",
            "title": "Meiko™ Ceremonial Matcha 1kg", "product_type": "Matcha",
            "tags": ["1kg", "meta-related-collection-featured"],
            "variants": [{"id": 15, "title": "Full-bodied with subtle floral aromas",
                          "sku": "MA-MEIK1000-01", "price": "700.00", "available": True}],
        },
    ]
}


def self_test() -> int:
    check = Checker()
    products = parse_fixture(FIXTURE_CATALOG, SHOP)

    check(len(products) == 5, "les 5 produits de la fixture sont parses",
          f"{len(products)} produit(s)")

    tailles = {v.label for p in products for v in p.variants}
    # LE test de ce marchand : aucune note de degustation ne doit atteindre la
    # colonne des tailles, sinon elle entre dans la cle metier et dans le texte
    # des alertes.
    check(tailles == {"20g", "100g", "1kg"},
          "seules des tailles reelles, aucune note de degustation",
          f"tailles polluees : {sorted(tailles)}")
    check(not any("umami" in v.label.lower() or "flavor" in v.label.lower()
                  for p in products for v in p.variants),
          "le titre de variante n'est jamais lu comme une taille",
          "une note de degustation a ete prise pour un conditionnement")

    par_cle = {(p.name, p.variants[0].label): p for p in products}

    check({k for k in par_cle if k[0] == "Meiko™ Ceremonial Matcha"}
          == {("Meiko™ Ceremonial Matcha", t) for t in ("20g", "100g", "1kg")},
          "les trois formats du meme matcha sont trois lignes distinctes",
          f"cles obtenues : {sorted(k for k in par_cle if 'Meiko' in k[0])}")
    check(all("™" in p.name for p in products),
          "le symbole ™ est conserve dans le nom, comme sur le site",
          "le ™ a ete retire, creant un deuxieme nom pour le meme produit")

    matsu = par_cle.get(("Matsu™ Ceremonial Matcha", "20g"))
    check(matsu is not None and matsu.variants[0].in_stock is False,
          "rupture reelle du catalogue lue comme telle", "rupture non detectee")

    misaki = par_cle.get(("Misaki™ Ceremonial Matcha", "20g"))
    check(misaki is not None and misaki.variants[0].price_usd == "$28.00",
          "prix en dollars, avec sa devise",
          f"prix incorrect : {misaki.variants[0].price_usd if misaki else None!r}")

    check_shared_invariants(check, products, SHOP)
    return check.report()


main = build_main(
    SHOP, self_test,
    description="Releve le stock des matcha Matchaeologist (catalogue Shopify public).")


if __name__ == "__main__":
    sys.exit(main())
