#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch_lesthes.py — releve de stock de la boutique Les Thes sur Terre.

Boutique Wix francaise, sans connexion : la page categorie est rendue COTE
SERVEUR, donc un simple GET suffit pour lire nom, prix et statut de rupture de
chaque produit. Aucun login, aucune session a maintenir.

Deux limites reelles du site, verifiees avant d'ecrire ce script :

1. PAS DE STOCK PAR TAILLE. La page produit ne rend pas son widget de detail
   cote serveur (variantes chargees en JavaScript), et les donnees de warmup
   Wix ne contiennent aucune information d'inventaire. Il n'existe donc aucune
   source serveur pour le stock taille par taille : `size` vaut toujours
   "Unique", et `in_stock` signifie « au moins une variante achetable ». Meme
   semantique degradee que le mode visiteur de Marukyu-Koyamaen. Les produits
   multi-variantes sont reperes par leur fourchette de prix et signales dans
   le champ `note`.

2. PAGINATION COTE CLIENT. `?page=2` renvoie les memes produits que `?page=1` :
   le serveur rend toute la categorie d'un coup. C'est vrai aux 13 produits
   actuels ; si la categorie grossit beaucoup, le script previent (voir
   SUSPICIOUS_COUNTS) plutot que de tronquer en silence.

Le parsing s'appuie EXCLUSIVEMENT sur les attributs data-hook / data-slug.
Les classes CSS de Wix sont des hashes obfusques (`s_wnSvX o__34T_FM---...`)
qui changent a chaque redeploiement du site : s'y fier casserait le releve
sans prevenir.

Usage rapide
------------
    pip install requests beautifulsoup4
    python3 matcha_watch_lesthes.py --self-test          # aucun reseau
    python3 matcha_watch_lesthes.py --no-push --verbose

Peut aussi ecrire le releve dans la base MatchAlert (memes secrets que les
autres scrappers) : MATCHALERT_API_URL et SCRAPPER_API_KEY. --no-push
desactive l'envoi meme si configure.

Codes de sortie : 0 = rien de neuf | 2 = restock detecte | 1 = erreur.
"""

from __future__ import annotations

import argparse
import copy
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Dependances manquantes. Lancez : pip install requests beautifulsoup4")

# Socle partage par les trois scrapers : modele de donnees, comparaison a l'etat
# precedent, exports, push vers le backend, notification, options de CLI
# communes. Voir matcha_common.py pour la frontiere exacte.
from matcha_common import (
    Product,
    ScrapeError,
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

BASE = "https://www.lesthes-surterre.com"
CATEGORY_URL = f"{BASE}/category/matcha"

# Identifiant du magasin cote backend (table `magasin`, V9__magasin_lesthes.sql).
MAGASIN_ID = "3"

# Nom affiche dans le sujet des alertes.
SHOP_NAME = "Les Thes sur Terre"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

# La categorie /category/matcha melange des thes et des accessoires (fouet,
# chawan, chashaku, tamis, repose-fouet, boite). On ne surveille que les
# consommables : un chashaku qui revient en stock n'interesse personne.
#
# Liste volontairement explicite plutot qu'heuristique sur le nom : un
# "Bol a the vert matcha" contient "matcha" sans etre du matcha. Le revers est
# qu'un nouveau the ajoute par la boutique doit etre ajoute ici — le script
# affiche donc les slugs ignores en mode --verbose pour le rendre visible.
CONSUMABLE_SLUGS = {
    "matcha-premium-latte",
    "matcha-japon-kagoshima",
    "the-matcha-chiran-japon",
    "matcha-haut-de-gamme-japonais",
    "hojicha-en-poudre",
    "coffret-ceremonie-matcha",
}

DEFAULT_STATE = Path("lesthes_state.json")

# Paliers de pagination Wix courants. Voir tomber pile sur l'un d'eux est le
# signe que la page est peut-etre tronquee et qu'il faudrait paginer.
SUSPICIOUS_COUNTS = {20, 24, 25, 50, 100}

# Hook du bloc produit. Source unique : le selecteur du parsing et le test de
# presence brut ci-dessous doivent designer la meme chose.
PRODUCT_ROOT_HOOK = "product-item-root"

# Une page totalement depourvue de bloc produit n'est pas forcement une refonte.
# Les runners GitHub sortent par des IP de datacenter, que Wix sert parfois
# autrement qu'un poste ordinaire, et le rendu peut aussi arriver incomplet. Ces
# deux cas passent au deuxieme essai ; une refonte, non.
CATALOG_ATTEMPTS = 3
CATALOG_RETRY_DELAY = 5


# --------------------------------------------------------------------------- #
# Parsing de la page categorie
# --------------------------------------------------------------------------- #

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_catalog(html: str, *, verbose: bool = False) -> list[Product]:
    """Extrait les produits consommables de la page categorie.

    Leve ScrapeError si la structure est meconnaissable : sans cette garde, une
    refonte du site produirait un releve vide, interprete comme « tout en
    rupture », puis une vague de fausses alertes de restock au retour.
    """
    soup = BeautifulSoup(html, "html.parser")
    roots = soup.select(f'[data-hook="{PRODUCT_ROOT_HOOK}"]')

    if not roots:
        raise ScrapeError(
            f"aucun bloc produit (data-hook=\"{PRODUCT_ROOT_HOOK}\") dans la page : "
            "structure du site probablement modifiee, releve abandonne")

    if len(roots) in SUSPICIOUS_COUNTS:
        print(f"[warn] {len(roots)} produits vus, soit exactement un palier de pagination Wix : "
              "la page est peut-etre tronquee (la pagination du site est cote client).",
              file=sys.stderr)

    products: list[Product] = []
    ignored: list[str] = []

    for root in roots:
        slug = (root.get("data-slug") or "").strip()
        if not slug:
            print("[warn] bloc produit sans data-slug, ignore", file=sys.stderr)
            continue
        if slug not in CONSUMABLE_SLUGS:
            ignored.append(slug)
            continue

        name_el = root.select_one('[data-hook="product-item-name"]')
        if name_el is None:
            print(f"[warn] {slug} : nom introuvable, produit ignore", file=sys.stderr)
            continue
        name = _clean(name_el.get_text(" ", strip=True))

        link = root.select_one('[data-hook="product-item-container"], '
                               '[data-hook="product-item-product-details-link"]')
        href = (link.get("href") if link else None) or f"{BASE}/product-page/{slug}"

        # Le libelle de rupture depend de la langue de la boutique ; le hook non.
        out_of_stock = root.select_one('[data-hook="product-item-out-of-stock"]') is not None

        note = ""
        price_el = root.select_one('[data-hook="product-item-price-to-pay"]')
        range_el = root.select_one('[data-hook="price-range-from"]')
        if price_el is not None:
            price = _clean(price_el.get("data-wix-price") or price_el.get_text(strip=True))
        elif range_el is not None:
            # Fourchette de prix = plusieurs variantes. On garde le prix « a
            # partir de » tel qu'affiche (donnee reelle, prefixe explicite),
            # et on note que in_stock porte sur le produit entier.
            #
            # Le hook st-price-range est imbrique dans price-range-from et ne
            # porte qu'un libelle pour lecteurs d'ecran (« Prix promotionnel ») :
            # sans l'ecarter, il se retrouve colle au prix.
            range_copy = copy.copy(range_el)
            for sr_only in range_copy.select('[data-hook="st-price-range"]'):
                sr_only.decompose()
            price = _clean(range_copy.get_text(" ", strip=True))
            note = "plusieurs variantes : disponibilite au niveau du produit, pas par taille"
        else:
            price = None

        products.append(Product(
            name=name,
            url=href,
            note=note,
            variants=[Variant(
                # Pas de stock par taille disponible cote serveur (cf. docstring).
                label="Unique",
                in_stock=not out_of_stock,
                price_eur=price,
                sku=slug,
            )],
        ))

    if not products:
        raise ScrapeError(
            f"aucun produit consommable reconnu sur {len(roots)} bloc(s) : les slugs de la "
            "boutique ont probablement change, mettez CONSUMABLE_SLUGS a jour "
            f"(slugs vus : {', '.join(sorted(ignored)) or 'aucun'})")

    if verbose and ignored:
        print(f"[info] {len(ignored)} produit(s) hors perimetre ignore(s) : "
              f"{', '.join(sorted(ignored))}")

    return products


def describe_page(resp: requests.Response) -> str:
    """Ce que la page contenait vraiment, en une ligne.

    Sans ces reperes, « le site a ete refondu » et « le runner s'est fait servir
    une page de blocage » produisent exactement le meme message d'erreur. Le
    second est indiagnosticable apres coup : la reponse n'existe plus nulle
    part, et la page se reaffiche normalement depuis n'importe quel navigateur.
    """
    texte = resp.text
    reperes = {mot: texte.count(mot) for mot in
               ("data-hook", "product-item", "captcha", "Access Denied", "unusual traffic")}
    return (f"HTTP {resp.status_code}, {len(texte)} caracteres, "
            f"type {resp.headers.get('Content-Type', '?')}, reperes {reperes}")


def fetch_catalog(session: requests.Session, *, verbose: bool = False) -> list[Product]:
    """Recupere la page categorie et en extrait les produits.

    Reessaye tant que la page ne contient aucun bloc produit — signature d'un
    blocage ou d'un rendu incomplet. Un slug qui a change, lui, ne se repare pas
    en reessayant : ce cas tombe directement dans parse_catalog, qui sait dire
    quels slugs il a vus.
    """
    for tentative in range(1, CATALOG_ATTEMPTS + 1):
        resp = session.get(CATEGORY_URL, timeout=45)
        resp.raise_for_status()
        if verbose:
            print(f"[info] page categorie recuperee ({len(resp.text)} caracteres)")

        if f'data-hook="{PRODUCT_ROOT_HOOK}"' not in resp.text and tentative < CATALOG_ATTEMPTS:
            print(f"[warn] aucun bloc produit (tentative {tentative}/{CATALOG_ATTEMPTS}), "
                  f"nouvel essai dans {CATALOG_RETRY_DELAY}s — {describe_page(resp)}",
                  file=sys.stderr)
            time.sleep(CATALOG_RETRY_DELAY)
            continue

        try:
            return parse_catalog(resp.text, verbose=verbose)
        except ScrapeError as exc:
            # La description remonte telle quelle dans scrapper_log : c'est la
            # seule trace qui survivra au run.
            raise ScrapeError(f"{exc} [{describe_page(resp)}]") from exc

    raise AssertionError("boucle de tentatives sortie sans retour ni exception")


# --------------------------------------------------------------------------- #
# Etat local + diff (identique aux autres scrappers)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Notifications (identique aux autres scrappers)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# MatchAlert — push vers le backend
# --------------------------------------------------------------------------- #

MATCHALERT_TIMEOUT = 60
MATCHALERT_ATTEMPTS = 3
LOG_DESCRIPTION_MAX = 1500


# --------------------------------------------------------------------------- #
# Sorties (identique aux autres scrappers)
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Fixtures + auto-test (aucun reseau)
# --------------------------------------------------------------------------- #

# Balisage reduit mais FIDELE a la page reelle : memes data-hook, meme
# imbrication, memes classes obfusquees (presentes exactement pour verifier
# qu'on ne s'appuie PAS dessus). Quatre cas : en stock, en rupture,
# fourchette de prix (multi-variantes), et un accessoire a exclure.
FIXTURE_CATALOG = """
<html><body>
<div data-slug="hojicha-en-poudre" data-hook="product-item-root" class="ETPbIy KJlsir">
  <a href="https://www.lesthes-surterre.com/product-page/hojicha-en-poudre"
     class="AJctir" data-hook="product-item-container"></a>
  <div data-hook="not-image-container" class="CZ0KIs">
    <div class="t2u_rw" data-hook="product-item-product-details">
      <p class="s_wnSvX o__34T_FM---typography-11-runningText" data-hook="product-item-name">Hojicha en poudre - Fukuoka, Japon</p>
      <div class="UqnnNN briESr" data-hook="prices-container">
        <span data-hook="price-range-from" class="WuSRvG SO2_DK">À partir de <!-- -->12,00 €<span class="iI5avH" data-hook="st-price-range">Prix promotionnel</span></span>
      </div>
    </div>
  </div>
</div>
<div data-slug="matcha-japon-kagoshima" data-hook="product-item-root" class="ETPbIy KJlsir">
  <a href="https://www.lesthes-surterre.com/product-page/matcha-japon-kagoshima"
     class="AJctir" data-hook="product-item-container"></a>
  <div data-hook="not-image-container" class="CZ0KIs">
    <div class="t2u_rw" data-hook="product-item-product-details">
      <p class="s_wnSvX FzO_a9" data-hook="product-item-name">Matcha premium - Kagoshima, Japon</p>
      <div class="UqnnNN briESr z3Ybtk" data-hook="prices-container">
        <span class="iI5avH" data-hook="sr-product-item-price-to-pay">Prix</span>
        <span data-hook="product-item-price-to-pay" class="cfpn1d" data-wix-price="34,90 €">34,90 €</span>
      </div>
      <div class="kzWTCn" data-hook="out-of-stock-text-container">
        <span data-hook="product-item-out-of-stock" class="_yRiWr">Rupture de stock</span>
      </div>
    </div>
  </div>
</div>
<div data-slug="the-matcha-chiran-japon" data-hook="product-item-root" class="ETPbIy">
  <a href="https://www.lesthes-surterre.com/product-page/the-matcha-chiran-japon"
     class="AJctir" data-hook="product-item-container"></a>
  <div data-hook="not-image-container">
    <div data-hook="product-item-product-details">
      <p data-hook="product-item-name">Thé Matcha Chiran -  Kagoshima, Japon</p>
      <div data-hook="prices-container">
        <span data-hook="product-item-price-to-pay" data-wix-price="38,90 €">38,90 €</span>
      </div>
    </div>
  </div>
</div>
<div data-slug="chashaku" data-hook="product-item-root" class="ETPbIy">
  <a href="https://www.lesthes-surterre.com/product-page/chashaku"
     class="AJctir" data-hook="product-item-container"></a>
  <div data-hook="not-image-container">
    <div data-hook="product-item-product-details">
      <p data-hook="product-item-name">Chashaku</p>
      <div data-hook="prices-container">
        <span data-hook="product-item-price-to-pay" data-wix-price="6,00 €">6,00 €</span>
      </div>
    </div>
  </div>
</div>
</body></html>
"""

FIXTURE_BROKEN = "<html><body><div>Site en maintenance</div></body></html>"

FIXTURE_ONLY_ACCESSORIES = """
<html><body>
<div data-slug="un-slug-inconnu" data-hook="product-item-root">
  <a href="/product-page/un-slug-inconnu" data-hook="product-item-container"></a>
  <div data-hook="product-item-product-details">
    <p data-hook="product-item-name">Produit renomme</p>
  </div>
</div>
</body></html>
"""


def self_test() -> int:
    failures = 0

    def check(cond: bool, ok: str, ko: str) -> None:
        nonlocal failures
        if cond:
            print(f"ok   {ok}")
        else:
            print(f"FAIL {ko}")
            failures += 1

    products = parse_catalog(FIXTURE_CATALOG)
    by_name = {p.name: p for p in products}

    check(len(products) == 3,
          "les 3 consommables sont retenus, l'accessoire (chashaku) est exclu",
          f"{len(products)} produit(s) retenu(s) : {[p.name for p in products]}")
    check("Chashaku" not in by_name, "accessoire hors perimetre exclu", "un accessoire a ete retenu")

    kago = by_name.get("Matcha premium - Kagoshima, Japon")
    check(kago is not None, "produit en rupture identifie par son nom", "produit Kagoshima introuvable")
    check(kago is not None and kago.variants[0].in_stock is False,
          "hook product-item-out-of-stock lu comme rupture", "rupture non detectee")
    check(kago is not None and kago.variants[0].price_eur == "34,90 €",
          "prix lu depuis data-wix-price", f"prix incorrect : {kago.variants[0].price_eur if kago else None!r}")
    check(kago is not None and kago.url.endswith("/product-page/matcha-japon-kagoshima"),
          "URL produit lue depuis le href du bloc", "URL incorrecte")

    chiran = by_name.get("Thé Matcha Chiran - Kagoshima, Japon")
    check(chiran is not None,
          "espaces multiples du nom normalises (« Chiran -  Kagoshima » -> un seul espace)",
          f"noms vus : {sorted(by_name)}")
    check(chiran is not None and chiran.variants[0].in_stock is True,
          "absence du hook de rupture lue comme disponible", "disponibilite incorrecte")

    hoji = by_name.get("Hojicha en poudre - Fukuoka, Japon")
    check(hoji is not None and hoji.variants[0].price_eur == "À partir de 12,00 €",
          "fourchette de prix propre, sans le libelle lecteur d'ecran imbrique",
          f"prix de fourchette incorrect : {hoji.variants[0].price_eur if hoji else None!r}")
    check(hoji is not None and "plusieurs variantes" in hoji.note,
          "produit multi-variantes signale dans la note (in_stock au niveau produit)",
          f"note manquante : {hoji.note if hoji else None!r}")

    check(all(v.label == "Unique" for p in products for v in p.variants),
          "taille 'Unique' partout (pas de stock par taille cote serveur)",
          "une taille inattendue a ete produite")

    # --- Garde-fous ------------------------------------------------------- #

    try:
        parse_catalog(FIXTURE_BROKEN)
        check(False, "", "une page meconnaissable n'a pas leve d'erreur")
    except ScrapeError:
        check(True, "page sans bloc produit -> ScrapeError (pas de faux 'tout en rupture')", "")

    try:
        parse_catalog(FIXTURE_ONLY_ACCESSORIES)
        check(False, "", "un catalogue sans slug connu n'a pas leve d'erreur")
    except ScrapeError:
        check(True, "slugs tous inconnus -> ScrapeError (signale un renommage cote boutique)", "")

    # --- Payload / horodatage --------------------------------------------- #

    payload = availability_payload(products, "2026-08-14T10:13:00", MAGASIN_ID)
    check(len(payload) == 3, "chaque produit devient un document matchaAvailability",
          f"payload inattendu : {len(payload)} element(s)")
    check(all(set(d) == {"magasin", "nom", "size", "time", "isAvailable"} for d in payload),
          "champs exactement conformes au modele MatchaAvailability (magasin inclus)",
          f"champs inattendus : {[sorted(d) for d in payload]}")
    check(all(d["magasin"] == MAGASIN_ID for d in payload),
          "magasin Les Thes sur Terre envoye explicitement", "magasin manquant ou incorrect")
    check(next(d for d in payload if d["nom"].startswith("Matcha premium"))["isAvailable"] is False,
          "isAvailable envoye en booleen JSON", "isAvailable mal type")

    ts = api_timestamp(datetime(2026, 8, 14, 10, 13, 0, tzinfo=timezone.utc))
    check(ts == "2026-08-14T10:13:00",
          "horodatage API sans offset (format LocalDateTime cote Java)",
          f"format inattendu : {ts!r}")

    catalog = catalog_payload(products, MAGASIN_ID)
    check(all(set(d) == {"magasin", "nom", "size", "url"} for d in catalog),
          "payload catalogue conforme au modele Matcha du backend",
          f"champs inattendus : {[sorted(d) for d in catalog]}")
    # Invariant critique : sans une correspondance exacte, le backend ne
    # retrouve pas le produit et l'alerte de restock repart sur l'URL de repli
    # (qui pointe la boutique Marukyu).
    check({(d["nom"], d["size"]) for d in catalog}
          == {(d["nom"], d["size"]) for d in payload},
          "catalogue et disponibilites partagent exactement les memes (nom, size)",
          "divergence (nom, size) entre catalogue et disponibilites")
    check(all(d["url"].startswith(BASE) for d in catalog),
          "chaque ligne de catalogue porte l'URL produit de cette boutique",
          f"URL inattendue : {[d['url'] for d in catalog]}")

    # --- Diff -------------------------------------------------------------- #

    cur = snapshot([chiran])
    prev_key = next(iter(cur))
    prev = {prev_key: {"in_stock": False, "product": chiran.name, "variant": "Unique",
                       "price": None, "url": chiran.url}}
    evs = diff(prev, cur, report_sold_out=False)
    check(len(evs) == 1 and evs[0].kind == "RESTOCK",
          "transition rupture -> dispo declenche un RESTOCK", f"diff incorrect : {evs}")
    check(not diff({}, cur, report_sold_out=False),
          "premier run silencieux (pas de fausse alerte)", "le premier run alerte a tort")

    kept = apply_only_filter(products, ["kagoshima"])
    check(len(kept) == 2,
          "filtre --only applique sur le nom (2 produits mentionnent Kagoshima)",
          f"{[p.name for p in kept]}")
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
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })

    try:
        products = fetch_catalog(session, verbose=args.verbose)
    except requests.RequestException as exc:
        print(f"\nErreur reseau : {exc}", file=sys.stderr)
        return 1, f"Erreur reseau : {exc}"
    except ScrapeError as exc:
        print(f"\nErreur de structure : {exc}", file=sys.stderr)
        return 1, f"Structure de page inattendue : {exc}"

    wanted = wanted_from(args)
    products = apply_only_filter(products, wanted)

    if not args.quiet:
        print(f"{len(products)} produit(s) surveille(s)"
              + (f" (filtre : {', '.join(wanted)})" if wanted else ""))
        for p in products:
            v = p.variants[0]
            etat = "DISPO" if v.in_stock else "rupture"
            prix = f"  {v.price_eur}" if v.price_eur else ""
            print(f"  {etat:<8} {p.name}{prix}")

    # Chaque produit n'a ici qu'une seule taille : compter les tailles n'aurait
    # aucun sens dans le resume, et la nommer dans le detail des changements
    # n'apporterait rien. D'ou les deux ecarts au comportement par defaut.
    return finish_run(products, args, magasin=MAGASIN_ID, shop_name=SHOP_NAME,
                      summary=f"{len(products)} produit(s) releve(s)",
                      detail_with_variant=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Releve le stock des matcha Les Thes sur Terre (page categorie publique).")
    add_common_arguments(ap, only_example="kagoshima,yame", default_state=DEFAULT_STATE)
    ap.add_argument("--parse-file", type=Path,
                    help="analyse une page categorie enregistree et affiche le resultat (aucun reseau)")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.parse_file:
        html = args.parse_file.read_text(encoding="utf-8", errors="replace")
        try:
            products = parse_catalog(html, verbose=True)
        except ScrapeError as exc:
            print(f"Structure non reconnue : {exc}", file=sys.stderr)
            return 1
        for p in products:
            v = p.variants[0]
            etat = "DISPO" if v.in_stock else "rupture"
            print(f"  {etat:<8} {p.name:<44} {v.price_eur or '':<22} {p.note}")
        return 0

    return run_cli(args, lambda: run(args), magasin=MAGASIN_ID)


if __name__ == "__main__":
    sys.exit(main())
