#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_shopify.py — socle commun aux boutiques Shopify.

POURQUOI CE FICHIER EXISTE. Six des sept marchands suivis tournent sur Shopify,
qui expose le meme catalogue public en JSON : `/collections/<handle>/products.json`
rend, pour chaque variante, le champ `available` que le theme utilise lui-meme
pour griser le bouton « Sold out ». Le relevé y coute UNE requete par
collection, sans session ni parsing HTML.

Ecrire six fois ce meme telechargement, cette meme lecture de `available` et ce
meme auto-test aurait produit six fichiers de 300 lignes a corriger six fois.
Ce module porte donc tout ce qui ne depend pas de la boutique ; chaque
`matcha_watch_<boutique>.py` ne declare plus qu'une configuration, une fixture
reelle et ses assertions propres.

CE QUI DIFFERE REELLEMENT D'UNE BOUTIQUE SHOPIFY A L'AUTRE, et que la
configuration doit donc exprimer :

  * la DEVISE. Kettl, Rocky's, Mizuba, Naoki et Matchaeologist vendent en
    dollars, Ippodo en yens. Le montant part dans un champ distinct de `Variant`
    pour que le prix affiche dans une alerte ne soit jamais ambigu.
  * l'endroit ou vit la TAILLE. Shopify permet de la porter en variante
    (Kettl : « 20g », « 1kg »), mais la moitie des marchands n'utilise pas ce
    mecanisme et l'ecrit dans le titre du produit (Ippodo : « Kanza 20g Box »),
    voire dans les tags (Mizuba). D'ou `size_sources`, une liste de sources
    essayees dans l'ordre.
  * les produits a ECARTER. Une collection « matcha » contient aussi des
    abonnements et des coffrets de theiere, qui n'ont pas de stock au sens ou
    l'entend une alerte de restock.

Nakamura Tokichi (`matcha_watch_tokichi.py`) est du Shopify lui aussi mais garde
son propre script : sa taille se lit sur la DERNIERE VIRGULE du titre
(« Matcha Starter,100g Bag 2-bag set »), un cas qu'aucun autre marchand ne
presente et qui ne merite pas une source de plus ici.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Dependance manquante. Lancez : pip install requests")

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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# Plafond Shopify par page de products.json. Aucun catalogue suivi n'en
# approche, mais la pagination reste geree : un marchand qui grossit ne doit pas
# se faire tronquer en silence.
PAGE_LIMIT = 250

# Etiquette employee quand la boutique ne vend le produit qu'en un seul
# conditionnement. La MEME chaine que matcha_watch_tokichi.py : (magasin, nom,
# size) est la cle metier cote backend, et deux libelles differents pour la
# meme idee creeraient deux lignes pour un seul produit.
TAILLE_UNIQUE = "Unique"

# Valeur que Shopify donne au titre d'une variante quand le produit n'a aucune
# option reelle. Ce n'est pas une taille, c'est l'absence de taille.
VARIANTE_SANS_OPTION = "Default Title"

# Taille en fin de titre : « Kanza 20g Box », « ... Matcha 100g », « ... 1kg ».
# Ancre sur la fin de chaine, et exige un blanc puis un chiffre : « Matcha
# To-Go Packets (2g x 10 packets) » ne matche donc pas — la parenthese precede
# le chiffre — et retombe sur TAILLE_UNIQUE plutot que de produire une taille
# inventee a partir d'une quantite par sachet.
TAILLE_EN_FIN = re.compile(r"\s+(\d+(?:[.,]\d+)?\s*(?:g|kg)\b[^,]*)$", re.IGNORECASE)

# Tag qui n'est QU'une taille (« 30g », « 1kg »). Volontairement strict : un tag
# comme « Matcha Large Packs » ne doit jamais etre pris pour un
# conditionnement.
TAILLE_EN_TAG = re.compile(r"^\d+(?:[.,]\d+)?\s*(?:g|kg)$", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Configuration d'une boutique                                                #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ShopifyShop:
    """Tout ce qui distingue une boutique Shopify des cinq autres.

    `magasin` est l'identifiant cote backend (table `magasin`) : il part sur
    chaque ligne poussee, le backend refusant en 400 un relevé qui n'en porte
    pas.

    `collections` en accepte plusieurs : Naoki eclate son matcha en
    « ceremonial-grade-matcha », « masters-collection-matcha » et
    « organic-matcha », et un meme produit figure dans deux d'entre elles. Le
    telechargement deduplique donc par identifiant Shopify.
    """

    magasin: str
    shop_name: str
    base: str
    collections: tuple[str, ...]
    state_file: Path
    only_example: str

    # Devise de la boutique, telle que /meta.json la declare. Determine le champ
    # de `Variant` qui recoit le montant et son format d'affichage.
    currency: str = "USD"

    # Sources de la taille, essayees dans l'ordre : "variant", "title", "tag".
    # La premiere qui rend quelque chose gagne ; aucune ne rend rien =
    # TAILLE_UNIQUE.
    size_sources: tuple[str, ...] = ("variant",)

    # Prefixe commercial a retirer du nom, en expression reguliere. Rocky's
    # prefixe TOUS ses titres de « rocky's matcha » : le garder ferait lire
    # « RESTOCK rocky's matcha Ceremonial Blend Matcha chez Rocky's Matcha ».
    strip_prefix: str | None = None

    # `product_type` a ecarter, en minuscules. Kettl ecrit tantot « Matcha »
    # tantot « matcha » : la comparaison est faite sans casse.
    exclude_types: frozenset[str] = frozenset()

    # Mots qui, presents dans le titre, ecartent le produit. Un abonnement n'a
    # pas de stock au sens d'une alerte de restock : il est indisponible tant
    # que le marchand ne le rouvre pas, ce qui n'est pas un restock.
    exclude_title: tuple[str, ...] = ("subscription",)

    def product_url(self, handle: str) -> str:
        return f"{self.base}/products/{handle}"

    def collection_url(self, collection: str) -> str:
        return f"{self.base}/collections/{collection}/products.json"


# --------------------------------------------------------------------------- #
# Prix                                                                        #
# --------------------------------------------------------------------------- #

def format_price(raw: Any, currency: str) -> tuple[str, str] | None:
    """Rend (nom du champ de `Variant`, montant formate), ou None.

    ATTENTION, PIEGE VERIFIE SUR LE SITE. `products.json` rend le montant DANS
    LA DEVISE DE LA BOUTIQUE, pas en centimes — contrairement a
    `/products/<handle>.js`, qui rend toujours des centimes. Verifie sur
    Tokichi : `products.json` donne 55556 la ou la fiche produit affiche
    `og:price:amount = 55,556`, soit bien 55 556 yens. Diviser par 100 ici
    sous-afficherait tous les prix d'un facteur cent.

    Le yen est une devise sans decimale : l'arrondi a l'entier n'est pas une
    troncature de commodite, c'est le format correct.
    """
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None

    if currency == "JPY":
        return "price_jpy", f"¥{value:,.0f}"
    if currency == "EUR":
        return "price_eur", f"{value:,.2f} €".replace(".", ",")
    return "price_usd", f"${value:,.2f}"


# --------------------------------------------------------------------------- #
# Nom et taille                                                               #
# --------------------------------------------------------------------------- #

def split_title_size(title: str) -> tuple[str, str | None]:
    """Detache du titre une taille en fin de chaine.

    Rend (nom, taille) ou (titre entier, None) si le titre n'en porte pas.
    """
    match = TAILLE_EN_FIN.search(title)
    if match is None:
        return title.strip(), None
    return title[: match.start()].strip(), match.group(1).strip()


def size_from_tags(tags: Iterable[str]) -> str | None:
    """Premier tag qui n'est qu'une taille.

    « Premier » et non « le plus grand » : l'ordre rendu par l'API est stable,
    donc le resultat aussi — et la stabilite compte plus que le choix, puisque
    ce libelle entre dans la cle metier (magasin, nom, size).
    """
    for tag in tags:
        texte = str(tag).strip()
        if TAILLE_EN_TAG.match(texte):
            return texte
    return None


def resolve_name_size(raw: dict[str, Any], variant: dict[str, Any],
                      shop: ShopifyShop) -> tuple[str, str]:
    """Nom du produit et libelle de taille, selon les sources de la boutique."""
    titre = (raw.get("title") or "").strip()
    if shop.strip_prefix:
        titre = re.sub(shop.strip_prefix, "", titre, count=1, flags=re.IGNORECASE).strip()

    nom_sans_taille, taille_du_titre = split_title_size(titre)
    variante = (variant.get("title") or "").strip()

    for source in shop.size_sources:
        if source == "variant":
            if variante and variante != VARIANTE_SANS_OPTION:
                # La taille vient de la variante : le titre du produit reste
                # entier, il ne contient alors pas de conditionnement.
                return titre, variante
        elif source == "title":
            if taille_du_titre:
                return nom_sans_taille, taille_du_titre
        elif source == "tag":
            taille = size_from_tags(raw.get("tags") or [])
            if taille:
                return titre, taille
        else:  # pragma: no cover - faute de configuration, pas d'execution
            raise ValueError(f"source de taille inconnue : {source!r}")

    return titre, TAILLE_UNIQUE


# --------------------------------------------------------------------------- #
# Catalogue                                                                   #
# --------------------------------------------------------------------------- #

def is_excluded(raw: dict[str, Any], shop: ShopifyShop) -> bool:
    type_produit = (raw.get("product_type") or "").strip().lower()
    if type_produit in shop.exclude_types:
        return True
    titre = (raw.get("title") or "").lower()
    return any(mot.lower() in titre for mot in shop.exclude_title)


def parse_product(raw: dict[str, Any], shop: ShopifyShop) -> Product | None:
    """Rend un `Product`, ou None si la boutique ecarte ce produit."""
    if is_excluded(raw, shop):
        return None

    url = shop.product_url(raw.get("handle", ""))
    nom = None
    variants: list[Variant] = []

    for v in raw.get("variants", []):
        nom_variante, taille = resolve_name_size(raw, v, shop)
        # Toutes les variantes d'un produit donnent le meme nom : la premiere
        # fixe donc celui du produit.
        nom = nom if nom is not None else nom_variante

        prix = format_price(v.get("price"), shop.currency)
        champs_prix = {prix[0]: prix[1]} if prix else {}

        available = v.get("available")
        variants.append(Variant(
            label=taille,
            # None et non False quand le champ manque : une taille dont l'etat
            # est indeterminable est ecartee du push (voir
            # availability_payload), alors qu'un False inventerait une rupture
            # puis un restock au run suivant — une alerte mensongere.
            in_stock=bool(available) if available is not None else None,
            sku=v.get("sku") or None,
            variation_id=str(v["id"]) if v.get("id") is not None else None,
            **champs_prix,
        ))

    if not variants:
        return None
    return Product(name=nom or "", url=url, variants=variants)


def fetch_catalog(session: requests.Session, shop: ShopifyShop, *,
                  verbose: bool = False) -> list[Product]:
    """Telecharge les collections de la boutique, dedupliquees par produit.

    La deduplication porte sur l'identifiant Shopify et non sur le titre : chez
    Naoki, un meme produit figure dans deux collections, et le compter deux fois
    doublerait ses lignes de catalogue cote backend.
    """
    bruts: dict[Any, dict[str, Any]] = {}

    for collection in shop.collections:
        page = 1
        while True:
            resp = session.get(shop.collection_url(collection),
                               params={"limit": PAGE_LIMIT, "page": page}, timeout=30)
            resp.raise_for_status()
            lot = resp.json().get("products", [])
            if not lot:
                break
            for raw in lot:
                bruts.setdefault(raw.get("id", raw.get("handle")), raw)
            if verbose:
                print(f"[info] {collection} page {page} : {len(lot)} produit(s)")
            if len(lot) < PAGE_LIMIT:
                break
            page += 1

    produits = [parse_product(raw, shop) for raw in bruts.values()]
    return [p for p in produits if p is not None]


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #

def run(args, shop: ShopifyShop) -> tuple[int, str]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

    try:
        products = fetch_catalog(session, shop, verbose=args.verbose)
    except requests.RequestException as exc:
        print(f"\nErreur reseau : {exc}", file=sys.stderr)
        return 1, f"Erreur reseau : {exc}"
    except (ValueError, KeyError) as exc:
        print(f"\nReponse JSON inattendue : {exc}", file=sys.stderr)
        return 1, f"Reponse JSON inattendue : {exc}"

    if not products:
        # Catalogue vide alors que la requete a reussi : la collection a ete
        # renommee ou videe. Sortir en erreur le fait remonter en ligne ERROR
        # dans la supervision, au lieu de faire passer pour « aucun changement »
        # une boutique qu'on ne releve plus du tout.
        message = ("Aucun produit relevé : collection(s) "
                   f"{', '.join(shop.collections)} vide(s) ou renommee(s)")
        print(f"\n{message}", file=sys.stderr)
        return 1, message

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

    return finish_run(products, args, magasin=shop.magasin, shop_name=shop.shop_name)


def build_main(shop: ShopifyShop, self_test: Callable[[], int],
               description: str) -> Callable[[list[str] | None], int]:
    """Fabrique le `main()` d'un scraper Shopify.

    Le corps etait identique d'une boutique a l'autre — memes options, meme
    branchement de l'auto-test, meme appel a `run_cli`. Seuls la configuration,
    la fixture et les assertions restent chez chaque marchand.
    """

    def main(argv: list[str] | None = None) -> int:
        ap = argparse.ArgumentParser(description=description)
        add_common_arguments(ap, only_example=shop.only_example,
                             default_state=shop.state_file)
        args = ap.parse_args(argv)

        if args.self_test:
            return self_test()

        return run_cli(args, lambda: run(args, shop), magasin=shop.magasin)

    return main


# --------------------------------------------------------------------------- #
# Auto-test : invariants communs                                              #
# --------------------------------------------------------------------------- #

@dataclass
class Checker:
    """Compteur d'echecs partage entre les invariants communs et ceux du marchand."""

    failures: int = 0

    def __call__(self, cond: bool, ok: str, ko: str) -> None:
        if cond:
            print(f"ok   {ok}")
        else:
            print(f"FAIL {ko}")
            self.failures += 1

    def report(self) -> int:
        print(f"\n{'TOUS LES TESTS PASSENT' if not self.failures else f'{self.failures} ECHEC(S)'}")
        return 1 if self.failures else 0


def check_shared_invariants(check: Checker, products: list[Product],
                            shop: ShopifyShop) -> None:
    """Invariants que les six boutiques doivent tenir, sans reseau.

    Ce sont ceux dont la violation casse le backend en silence : un document
    sans magasin part en 400, un (nom, size) different entre catalogue et
    disponibilites fait perdre le lien d'achat de l'alerte, un premier run
    bavard alerte tous les abonnes a tort.
    """
    payload = availability_payload(products, "2026-08-14T10:13:00", shop.magasin)

    check(all(set(d) == {"magasin", "nom", "size", "time", "isAvailable"} for d in payload),
          "champs conformes au modele MatchaAvailability",
          f"champs inattendus : {[sorted(d) for d in payload]}")
    check(all(d["magasin"] == shop.magasin for d in payload),
          f"magasin {shop.magasin} envoye explicitement sur chaque document",
          "magasin manquant ou incorrect")
    check(all(d["nom"] and d["size"] for d in payload),
          "aucun nom ni taille vide dans les disponibilites",
          f"nom ou taille vide : {[d for d in payload if not (d['nom'] and d['size'])]}")

    catalog = catalog_payload(products, shop.magasin)
    check(all(set(d) == {"magasin", "nom", "size", "url"} for d in catalog),
          "payload catalogue conforme au modele Matcha",
          f"champs inattendus : {[sorted(d) for d in catalog]}")
    check(all(d["url"].startswith(shop.base + "/products/") for d in catalog),
          "URL d'achat construite depuis le handle de la boutique",
          f"URL inattendue : {[d['url'] for d in catalog][:3]}")
    # Invariant critique : sans correspondance exacte, le backend ne retrouve
    # pas le produit et l'alerte de restock repart sur l'URL de repli.
    check({(d["nom"], d["size"]) for d in catalog} >= {(d["nom"], d["size"]) for d in payload},
          "catalogue et disponibilites partagent les memes (nom, size)",
          "divergence (nom, size) entre catalogue et disponibilites")

    check(api_timestamp(datetime(2026, 8, 14, 10, 13, 0, tzinfo=timezone.utc))
          == "2026-08-14T10:13:00",
          "horodatage API sans offset (format LocalDateTime cote Java)",
          "format d'horodatage inattendu")

    check(not diff({}, snapshot(products), report_sold_out=False),
          "premier run silencieux (pas de fausse alerte)",
          "le premier run alerte a tort")

    # Les deux sens de transition, sur une taille reelle du relevé mais avec des
    # etats IMPOSES. Deduire le sens de la fixture rendait le test dependant du
    # stock du jour : chez Kettl, le premier produit est en rupture, et
    # « rupture -> dispo » y devenait « dispo -> rupture », que
    # report_sold_out=False supprime — le test ne verifiait alors plus rien.
    cle, etat = next(iter(snapshot([products[0]]).items()))
    en_rupture = {cle: {**etat, "in_stock": False}}
    en_stock = {cle: {**etat, "in_stock": True}}

    restock = diff(en_rupture, en_stock, report_sold_out=False)
    check(len(restock) == 1 and restock[0].kind == "RESTOCK",
          "transition rupture -> dispo declenche un RESTOCK",
          f"diff incorrect : {restock}")

    check(not diff(en_stock, en_rupture, report_sold_out=False),
          "une rupture reste muette sans --sold-out-too",
          "une rupture alerte alors que l'option est absente")
    epuise = diff(en_stock, en_rupture, report_sold_out=True)
    check(len(epuise) == 1 and epuise[0].kind == "SOLD_OUT",
          "transition dispo -> rupture declenche un SOLD_OUT avec --sold-out-too",
          f"diff incorrect : {epuise}")

    check(len(apply_only_filter(products, ["rien-qui-matche"])) == len(products),
          "repli sur le catalogue complet si le filtre ne matche rien",
          "un filtre sans correspondance produirait une surveillance vide")

    check(all(v.in_stock in (True, False, None) for p in products for v in p.variants),
          "disponibilite toujours booleenne ou indeterminee",
          "disponibilite d'un type inattendu")


def parse_fixture(fixture: dict[str, Any], shop: ShopifyShop) -> list[Product]:
    """Parse une fixture de catalogue comme le ferait `fetch_catalog`, hors reseau."""
    produits = [parse_product(raw, shop) for raw in fixture["products"]]
    return [p for p in produits if p is not None]
