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
import csv
import json
import os
import re
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
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Dependances manquantes. Lancez : pip install requests beautifulsoup4")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE = "https://www.lesthes-surterre.com"
CATEGORY_URL = f"{BASE}/category/matcha"

# Identifiant du magasin cote backend (table `magasin`, V9__magasin_lesthes.sql).
MAGASIN_ID = "3"

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


class ScrapeError(RuntimeError):
    """Structure de page inattendue : on refuse de deduire un etat de stock."""


# --------------------------------------------------------------------------- #
# Modele — memes formes que les autres scrappers, pour un contrat identique.
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
    roots = soup.select('[data-hook="product-item-root"]')

    if not roots:
        raise ScrapeError(
            "aucun bloc produit (data-hook=\"product-item-root\") dans la page : "
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


def apply_only_filter(products: list[Product], wanted: list[str]) -> list[Product]:
    """Comme les autres scrappers : un filtre qui ne matche rien retombe sur la
    liste complete, plutot que de surveiller silencieusement zero produit.
    """
    if not wanted:
        return products
    kept = [p for p in products
            if any(w in p.name.lower() or w in p.url.lower() for w in wanted)]
    return kept or products


def fetch_catalog(session: requests.Session, *, verbose: bool = False) -> list[Product]:
    resp = session.get(CATEGORY_URL, timeout=45)
    resp.raise_for_status()
    if verbose:
        print(f"[info] page categorie recuperee ({len(resp.text)} caracteres)")
    return parse_catalog(resp.text, verbose=verbose)


# --------------------------------------------------------------------------- #
# Etat local + diff (identique aux autres scrappers)
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
# Notifications (identique aux autres scrappers)
# --------------------------------------------------------------------------- #

def notify(events: list[Event], *, verbose: bool = False) -> None:
    if not events:
        return
    body = "\n".join(e.line() for e in events)
    subject = f"Les Thes sur Terre : {len(events)} changement(s) de stock"

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
# MatchAlert — push vers le backend
# --------------------------------------------------------------------------- #

MATCHALERT_TIMEOUT = 60
MATCHALERT_ATTEMPTS = 3
LOG_DESCRIPTION_MAX = 1500


def api_timestamp(moment: datetime | None = None) -> str:
    """ISO local sans offset, en UTC — le backend trie `time` comme une chaine."""
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
    """(magasin, nom, size) est la cle metier cote backend depuis le chantier
    multi-magasins. Les tailles indeterminables sont ecartees : les pousser en
    `false` provoquerait une fausse alerte de restock au run suivant.
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
# Sorties (identique aux autres scrappers)
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

    payload = availability_payload(products, "2026-08-14T10:13:00")
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

    wanted = [w.strip().lower() for w in args.only.split(",") if w.strip()]
    products = apply_only_filter(products, wanted)

    if not args.quiet:
        print(f"{len(products)} produit(s) surveille(s)"
              + (f" (filtre : {', '.join(wanted)})" if wanted else ""))
        for p in products:
            v = p.variants[0]
            etat = "DISPO" if v.in_stock else "rupture"
            prix = f"  {v.price_eur}" if v.price_eur else ""
            print(f"  {etat:<8} {p.name}{prix}")

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
    db_note = push_availabilities(products, args) if push_enabled(args) else ""

    summary = f"{len(products)} produit(s) releve(s)"

    if events:
        print("\n" + "=" * 64)
        for e in events:
            print(e.line())
        print("=" * 64)
        notify(events, verbose=args.verbose)
        detail = " ; ".join(f"{e.kind} {e.product}" for e in events)
        summary = f"{summary} ; {len(events)} changement(s) : {detail}{db_note}"
        return (2 if any(e.kind == "RESTOCK" for e in events) else 0), summary

    if not args.quiet:
        print("\nAucun changement")
    return 0, f"{summary} ; aucun changement{db_note}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Releve le stock des matcha Les Thes sur Terre (page categorie publique).")
    ap.add_argument("--only", default="",
                    help="filtre par nom, separe par des virgules (ex: kagoshima,yame)")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--json", type=Path, help="ecrit le releve complet en JSON")
    ap.add_argument("--csv", type=Path, help="ecrit le releve complet en CSV")
    ap.add_argument("--sold-out-too", action="store_true")
    ap.add_argument("--no-push", action="store_true",
                    help="ne rien envoyer a MatchAlert, meme si l'API est configuree")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="teste le parsing hors ligne puis quitte")
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
