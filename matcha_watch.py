#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
matcha_watch.py — releve de stock de la boutique Marukyu-Koyamaen.

La boutique exige une connexion : un visiteur anonyme voit
"You must register and login to shop." et n'a acces ni au detail des tailles ni
a leur disponibilite. Ce script ouvre une session, la conserve entre deux
executions, et releve l'etat de chaque taille.

Parsing base sur la structure REELLE du site (verifiee sur une page enregistree
en session authentifiee) : le theme rend chaque variante en dur cote serveur,
il n'utilise PAS l'attribut standard WooCommerce data-product_variations.

    <form class="variations_form cart" data-product_title="Kiwami Choan">
      <div class="product-form-row woocommerce-variation" data-variation_id="22822">
        <dl class="pa pa-sku"><dd>1G36020C1</dd></dl>
        <dl class="pa pa-size"><dd>20g can</dd></dl>
        <span class="woocs_price_JPY">Y12,600</span>
        <span class="woocs_price_EUR">EUR68.50</span>
        <p class="stock out-of-stock">Out of stock</p>
        <button class="single_add_to_cart_button" disabled>Add to cart</button>
      </div>
      ...

Usage rapide
------------
    pip install requests beautifulsoup4
    export MKY_USER='mon.email@exemple.com'
    export MKY_PASS='mon-mot-de-passe'
    python3 matcha_watch.py --only wako,yugen,isuzu,aoarashi

Il ne contourne aucune protection anti-bot : s'il detecte une page de
verification, il s'arrete proprement et vous le dit.

Peut aussi ecrire le releve dans la base MatchAlert (Firestore, via le backend
Spring) : definissez MATCHALERT_API_URL et SCRAPPER_API_KEY. Sans elles, rien
n'est envoye et le comportement est inchange. --no-push desactive l'envoi.

Codes de sortie : 0 = rien de neuf | 2 = restock detecte | 1 = erreur.
"""

from __future__ import annotations

import argparse
import csv
import http.cookiejar
import json
import os
import random
import re
import smtplib
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Dependances manquantes. Lancez : pip install requests beautifulsoup4")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE = "https://www.marukyu-koyamaen.co.jp"
ACCOUNT_URL = f"{BASE}/english/shop/account"

CATALOG_URLS = [
    f"{BASE}/english/shop/products/catalog/matcha",
]

PRODUCT_HREF_RE = re.compile(r"^/english/shop/products/[0-9a-z]{6,}/?$", re.I)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
)

GUEST_MARKER = "you must register and login to shop"
WHOLE_PRODUCT_OUT = "currently out of stock and unavailable"

CHALLENGE_MARKERS = (
    "just a moment",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "checking your browser before accessing",
    "g-recaptcha",
    "h-captcha",
    "please verify you are a human",
)

DEFAULT_STATE = Path("matcha_state.json")
DEFAULT_COOKIES = Path("matcha_cookies.txt")


class ChallengeDetected(RuntimeError):
    """Page de verification anti-bot. On n'insiste pas."""


class NotAuthenticated(RuntimeError):
    """La session n'est pas (ou plus) connectee."""


# --------------------------------------------------------------------------- #
# Modele
# --------------------------------------------------------------------------- #

@dataclass
class Variant:
    label: str
    in_stock: bool | None          # None = indeterminable
    price_jpy: str | None = None
    price_eur: str | None = None
    sku: str | None = None
    variation_id: str | None = None
    limit: str | None = None       # ex. "Limit one per person"

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
# Reseau
# --------------------------------------------------------------------------- #

def build_session(cookie_file: Path) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    jar = http.cookiejar.MozillaCookieJar(str(cookie_file))
    if cookie_file.exists():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as exc:
            print(f"[warn] cookies illisibles ({exc}), nouvelle session", file=sys.stderr)
    s.cookies = jar  # type: ignore[assignment]
    return s


def save_cookies(session: requests.Session) -> None:
    jar = session.cookies
    if isinstance(jar, http.cookiejar.MozillaCookieJar):
        try:
            jar.save(ignore_discard=True, ignore_expires=True)
        except Exception as exc:  # pragma: no cover
            print(f"[warn] impossible d'enregistrer les cookies : {exc}", file=sys.stderr)


def looks_like_challenge(text: str) -> bool:
    head = text[:6000].lower()
    return any(m in head for m in CHALLENGE_MARKERS)


def polite_get(session: requests.Session, url: str, *, timeout: int = 30,
               retries: int = 3, verbose: bool = False) -> str:
    delay = 5.0
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            if verbose:
                print(f"[warn] {url} : {exc} (essai {attempt}/{retries})", file=sys.stderr)
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code in (429, 503):
            wait = min(float(r.headers.get("Retry-After", delay)), 300.0)
            print(f"[warn] HTTP {r.status_code} sur {url} — pause {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)
            delay *= 2
            continue

        if r.status_code == 404:
            raise RuntimeError(f"404 sur {url}")
        r.raise_for_status()

        if looks_like_challenge(r.text):
            raise ChallengeDetected(
                f"Page de verification anti-bot renvoyee par {url}.\n"
                "Le script s'arrete volontairement : il ne tente pas de la contourner.\n"
                "Augmentez --delay, reduisez la liste avec --only, ou reconnectez-vous "
                "depuis votre navigateur et reexportez les cookies."
            )
        return r.text

    raise RuntimeError(f"Echec sur {url} apres {retries} essais ({last_exc})")


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _text(node) -> str | None:
    if node is None:
        return None
    t = re.sub(r"\s+", " ", node.get_text(" ", strip=True))
    return t or None


def _price(node) -> str | None:
    """Les prix sont eclates en plusieurs balises
    (<span>€</span>68<small>.50</small>) : on concatene sans separateur."""
    if node is None:
        return None
    t = re.sub(r"\s+", "", node.get_text("", strip=True))
    return t or None


def product_name(soup: BeautifulSoup, url: str) -> str:
    """Le <h1> du site vaut 'Product Detail' : inutilisable. On prend
    data-product_title, puis le <title>, puis le slug."""
    form = soup.select_one("form.variations_form")
    if form and form.get("data-product_title"):
        return form["data-product_title"].strip()
    title = soup.find("title")
    if title:
        return title.get_text(strip=True).split("|")[0].strip()
    return url.rstrip("/").rsplit("/", 1)[-1]


def parse_variation_row(row) -> Variant:
    size = _text(row.select_one("dl.pa-size dd")) or _text(row.select_one("dl.pa dd"))
    sku = _text(row.select_one("dl.pa-sku dd"))
    label = size or row.get("data-variation_id") or "variante"

    stock_p = row.select_one("p.stock")
    classes = set(stock_p.get("class") or []) if stock_p is not None else set()
    btn = row.select_one("button.single_add_to_cart_button") or row.select_one("button[type=submit]")

    # On se fie aux CLASSES, jamais au texte : une taille disponible peut afficher
    # "Limit one per person" au lieu de "In stock".
    if "out-of-stock" in classes:
        in_stock: bool | None = False
    elif "in-stock" in classes:
        in_stock = True
    elif btn is not None:
        in_stock = not btn.has_attr("disabled")
    else:
        in_stock = None

    # Garde-fou : un bouton desactive prime sur un libelle optimiste.
    if in_stock and btn is not None and btn.has_attr("disabled"):
        in_stock = False

    # Limite d'achat eventuelle (classe sold-individually / texte du p.stock).
    limit = None
    if in_stock and stock_p is not None:
        txt = _text(stock_p)
        if txt and txt.strip().lower() not in ("in stock", ""):
            limit = txt
    qty = row.select_one("input.qty, input[name^=quantity]")
    if limit is None and qty is not None and qty.has_attr("readonly"):
        limit = f"quantite figee a {qty.get('value', '1')}"

    return Variant(
        label=label,
        in_stock=in_stock,
        price_jpy=_price(row.select_one("span.woocs_price_JPY")),
        price_eur=_price(row.select_one("span.woocs_price_EUR")),
        sku=sku,
        variation_id=row.get("data-variation_id"),
        limit=limit,
    )


def parse_product(html_text: str, url: str) -> Product:
    soup = BeautifulSoup(html_text, "html.parser")
    name = product_name(soup, url)
    form = soup.select_one("form.variations_form")

    # --- Cas 1 : structure reelle du site, une ligne par taille --------------
    rows = []
    if form is not None:
        rows = form.select("div.product-form-row.woocommerce-variation")
        if not rows:
            rows = form.select("[data-variation_id]")
    if rows:
        return Product(name, url, [parse_variation_row(r) for r in rows])

    # --- Cas 2 : WooCommerce standard (au cas ou le theme changerait) --------
    if form is not None and form.has_attr("data-product_variations"):
        raw = form["data-product_variations"]
        if raw and raw not in ("false", "[]"):
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                return Product(name, url, [], note=f"JSON des variations illisible ({exc})")
            variants = []
            for v in data:
                attrs = v.get("attributes", {}) or {}
                lbl = " / ".join(
                    str(x).replace("-", " ") for x in attrs.values() if x
                ) or f"variation {v.get('variation_id')}"
                variants.append(Variant(
                    label=lbl,
                    in_stock=bool(v.get("is_in_stock")),
                    price_jpy=str(v.get("display_price")) if v.get("display_price") else None,
                    sku=v.get("sku") or None,
                    variation_id=str(v.get("variation_id") or "") or None,
                ))
            return Product(name, url, variants)

    page_text = soup.get_text(" ", strip=True).lower()

    # --- Cas 3 : session non connectee --------------------------------------
    if GUEST_MARKER in page_text:
        raise NotAuthenticated(
            f"'{name}' affiche « You must register and login to shop. » : la session "
            "n'est pas connectee. Sans authentification le detail des tailles est absent "
            "de la page."
        )

    # --- Cas 4 : produit entierement en rupture -----------------------------
    if WHOLE_PRODUCT_OUT in page_text or soup.select_one("p.stock.out-of-stock"):
        return Product(name, url, [Variant("toutes tailles", False)],
                       note="produit entierement en rupture")

    # --- Cas 5 : produit simple ---------------------------------------------
    btn = soup.select_one("button[name=add-to-cart], input[name=add-to-cart], "
                          "button.single_add_to_cart_button")
    if btn is not None and not btn.has_attr("disabled"):
        return Product(name, url, [Variant(
            "produit simple", True,
            price_jpy=_price(soup.select_one("span.woocs_price_JPY")) or _price(soup.select_one("p.price")),
        )])

    return Product(name, url, [Variant("inconnu", None)],
                   note="structure de page non reconnue")


def discover_products(session: requests.Session, *,
                      verbose: bool = False) -> list[tuple[str, str]]:
    """Retourne [(url, libelle du lien)] depuis les pages catalogue.

    Le libelle sert a filtrer AVANT de telecharger les fiches : sans lui, un
    --only sur 4 produits declencherait quand meme 47 requetes par execution.
    """
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for catalog in CATALOG_URLS:
        soup = BeautifulSoup(polite_get(session, catalog, verbose=verbose), "html.parser")
        for a in soup.find_all("a", href=True):
            path = urlparse(urljoin(BASE, a["href"])).path
            if not PRODUCT_HREF_RE.match(path):
                continue
            full = urljoin(BASE, path)
            if full in seen:
                continue
            seen.add(full)
            found.append((full, re.sub(r"\s+", " ", a.get_text(" ", strip=True))))
    return found


def select_targets(found: list[tuple[str, str]], wanted: list[str], *,
                   verbose: bool = False) -> list[str]:
    """Applique le filtre --only sur le libelle du catalogue ou le slug.

    Si aucun libelle ne correspond (catalogue rendu differemment, liens sans
    texte), on retombe sur la liste complete plutot que de ne rien surveiller :
    mieux vaut une execution plus lente qu'une surveillance silencieusement vide.
    """
    if not wanted:
        return [u for u, _ in found]

    kept = [u for u, label in found
            if any(w in label.lower() or w in u.rsplit("/", 1)[-1].lower() for w in wanted)]
    if kept:
        if verbose:
            print(f"[info] filtre applique avant telechargement : {len(kept)} fiche(s) sur {len(found)}")
        return kept

    print("[warn] le filtre --only n'a rien trouve dans le catalogue ; "
          "toutes les fiches seront relevees puis filtrees apres coup.", file=sys.stderr)
    return [u for u, _ in found]


# --------------------------------------------------------------------------- #
# Authentification
# --------------------------------------------------------------------------- #

def probe_authenticated(session: requests.Session, product_url: str, *,
                        verbose: bool = False) -> bool:
    """Le seul test fiable : une fiche produit montre-t-elle les variantes ?

    Le site n'expose pas de lien 'logout' exploitable ; on se base donc sur le
    contenu reellement utile.
    """
    html_text = polite_get(session, product_url, verbose=verbose)
    if GUEST_MARKER in html_text.lower():
        return False
    soup = BeautifulSoup(html_text, "html.parser")
    return bool(soup.select("form.variations_form [data-variation_id]"))


def login(session: requests.Session, user: str, password: str, *, verbose: bool = False) -> None:
    """Connexion WooCommerce, formulaire analyse dynamiquement (nonce inclus)."""
    def fetch_form():
        soup = BeautifulSoup(polite_get(session, ACCOUNT_URL, verbose=verbose), "html.parser")
        for candidate in soup.find_all("form"):
            if candidate.find("input", attrs={"type": "password"}):
                return candidate
        return None

    form = fetch_form()

    # Une session perimee ou corrompue fait souvent disparaitre le formulaire :
    # le site croit reconnaitre un utilisateur et n'affiche plus la connexion,
    # alors que les fiches produit restent en mode invite. On repart a zero.
    if form is None and len(session.cookies) > 0:
        if verbose:
            print("[info] formulaire absent avec les cookies existants — session purgee, nouvel essai")
        session.cookies.clear()
        form = fetch_form()

    if form is None:
        raise NotAuthenticated(
            "Formulaire de connexion introuvable sur " + ACCOUNT_URL + ".\n"
            "Enregistrez cette page depuis le navigateur et inspectez-la, ou passez "
            "par l'export de cookies."
        )

    payload: dict[str, str] = {}
    user_field = password_field = None
    for inp in form.find_all(("input", "select", "textarea")):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "").lower()
        if itype == "password":
            password_field = name
            continue
        if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        if user_field is None and itype in ("text", "email"):
            user_field = name
            continue
        payload[name] = inp.get("value", "")
    for btn in form.find_all("button"):
        if btn.get("name"):
            payload[btn["name"]] = btn.get("value", "")

    if not user_field or not password_field:
        raise NotAuthenticated("Champs identifiant/mot de passe non reconnus.")

    payload[user_field] = user
    payload[password_field] = password

    action = urljoin(ACCOUNT_URL, form.get("action") or ACCOUNT_URL)
    if verbose:
        print(f"[info] POST {action} champs={sorted(payload)}")

    resp = session.post(action, data=payload,
                        headers={"Referer": ACCOUNT_URL, "Origin": BASE},
                        timeout=30, allow_redirects=True)
    resp.raise_for_status()

    if looks_like_challenge(resp.text):
        raise ChallengeDetected(
            "La page de connexion renvoie une verification anti-bot. Connectez-vous "
            "manuellement dans le navigateur puis exportez vos cookies (voir README)."
        )

    err = BeautifulSoup(resp.text, "html.parser").select_one("ul.woocommerce-error li, .woocommerce-error")
    if err is not None:
        raise NotAuthenticated(f"Connexion refusee : {_text(err)}")

    save_cookies(session)
    if verbose:
        print("[info] formulaire de connexion accepte")


# --------------------------------------------------------------------------- #
# Etat / diff
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
# Notifications
# --------------------------------------------------------------------------- #

def notify(events: list[Event], *, verbose: bool = False) -> None:
    if not events:
        return
    body = "\n".join(e.line() for e in events)
    subject = f"Marukyu-Koyamaen : {len(events)} changement(s) de stock"

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
# MatchAlert — push vers le backend Spring / Firestore
# --------------------------------------------------------------------------- #
#
# Deux endpoints, tous deux proteges par le seul header X-API-KEY :
#
#   POST /api/matcha-availability/push-batch
#        corps : [ {nom, size, time, isAvailable}, ... ]  -> collection matchaAvailability
#        Le backend deduplique lui-meme : il relit la derniere entree (nom,size)
#        et n'ecrit que si isAvailable a change. On envoie donc l'etat complet
#        a chaque run, sans se soucier du diff.
#
#   POST /api/scrapper/log
#        corps : {status, description, time}              -> collection logs
#
# Actif seulement si MATCHALERT_API_URL et SCRAPPER_API_KEY sont definis.

MATCHALERT_TIMEOUT = 60
MATCHALERT_ATTEMPTS = 3
LOG_DESCRIPTION_MAX = 1500

# Identifiant du magasin cote backend (table `magasin`, seme par V2). Il etait
# jusqu'ici implicite : le backend repliait toute ligne sans magasin sur '1'.
# Il devient explicite parce que le push de catalogue en a besoin, et parce
# qu'un repli implicite est un mauvais defaut maintenant que trois magasins
# coexistent.
MAGASIN_ID = "1"


def api_timestamp(moment: datetime | None = None) -> str:
    """Horodatage au format attendu par le backend.

    Firestore trie ce champ avec orderBy("time", DESCENDING) sur une CHAINE :
    le tri est lexicographique, pas chronologique. Un ISO avec offset
    ("2026-08-14T10:13:00+02:00") melange a l'ISO local que le backend genere
    par defaut ("2026-08-14T10:13:00") casserait cet ordre — donc la detection
    de changement, donc les alertes. On envoie toujours le meme format que
    LocalDateTime.toString() cote Java : ISO local, sans offset, en UTC.
    """
    moment = moment or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def matchalert_config() -> tuple[str, str] | None:
    """(url de base, cle API) si les deux sont definis, sinon None."""
    base = os.environ.get("MATCHALERT_API_URL", "").strip().rstrip("/")
    key = os.environ.get("SCRAPPER_API_KEY", "").strip()
    if not base or not key:
        return None
    # Les chemins ci-dessous incluent deja /api : on tolere une URL de base
    # renseignee avec ou sans, plutot que de produire un /api/api/... muet.
    if base.endswith("/api"):
        base = base[:-4]
    return base, key


def push_enabled(args) -> bool:
    return not args.no_push and matchalert_config() is not None


def availability_payload(products: Iterable[Product], stamp: str) -> list[dict[str, Any]]:
    """Aplati le releve en documents matchaAvailability.

    Les tailles dont l'etat est indeterminable (in_stock is None) sont
    ECARTEES. Les pousser en `false` ferait croire au backend a une rupture,
    puis a un restock au run suivant : une alerte mensongere envoyee a tous
    les abonnes. Une donnee absente vaut mieux qu'une donnee fausse.

    Doublon (nom, size) dans un meme lot : on ne garde que le dernier. Le
    backend compare chaque element a l'etat DEJA en base, pas aux precedents
    du lot — deux entrees identiques passeraient donc toutes les deux.
    """
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            if v.in_stock is None:
                continue
            items[(p.name, v.label)] = {
                "nom": p.name,
                "size": v.label,
                "time": stamp,
                "isAvailable": bool(v.in_stock),
            }
    return list(items.values())


def catalog_payload(products: Iterable[Product]) -> list[dict[str, Any]]:
    """Aplati le releve en lignes de catalogue (table matcha cote backend).

    Le couple (nom, size) est construit depuis les MEMES objets que
    availability_payload : c'est ce qui garantit que le backend retrouve le
    produit pour composer le lien d'achat de l'alerte de restock.

    Contrairement aux disponibilites, une taille indeterminable est CONSERVEE
    ici : elle n'affirme aucun etat de stock, elle rend juste le produit
    visible dans l'application.
    """
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for p in products:
        for v in p.variants:
            items[(p.name, v.label)] = {
                "magasin": MAGASIN_ID,
                "nom": p.name,
                "size": v.label,
                "url": p.url,
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
            # Cle refusee ou corps invalide : reessayer ne changera rien.
            if resp.status_code in (400, 401, 403):
                break
        if verbose:
            print(f"[warn] {path} essai {attempt}/{MATCHALERT_ATTEMPTS} : {detail}", file=sys.stderr)
        if attempt < MATCHALERT_ATTEMPTS:
            time.sleep(2 * attempt)

    return False, detail


def push_availabilities(products: list[Product], args) -> str:
    """Envoie l'etat de toutes les tailles. Retourne une note pour le log.

    N'echoue jamais le run : la base est une destination secondaire, l'alerte
    reste la fonction principale du script.
    """
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


def push_catalog(products: list[Product], args) -> None:
    """Tient a jour le catalogue produit du magasin cote backend.

    Best effort : le seed initial ne couvrait que 18 lignes alors que le
    releve porte desormais sur tout le catalogue, donc les produits hors seed
    n'avaient aucun lien d'achat. Un echec est journalise mais ne fait jamais
    echouer le run : l'alerte reste la fonction principale du script.
    """
    items = catalog_payload(products)
    if not items:
        return

    ok, detail = _matchalert_post("/api/matchas/push-catalog", items, verbose=args.verbose)
    if not ok:
        print(f"[warn] push catalogue echoue : {detail}", file=sys.stderr)
    elif args.verbose:
        print(f"[info] catalogue : {detail[:200]}")


def push_log(status: str, description: str, *, verbose: bool = False) -> bool:
    """Trace le run dans la collection `logs`. Best effort, jamais bloquant."""
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
# Sorties
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

# Reproduit fidelement le balisage releve sur une page enregistree en session
# authentifiee (Kiwami Choan, 2 tailles, tout en rupture).
FIXTURE_REAL_OUT = """
<html><head><title>Kiwami Choan | Matcha | Marukyu Koyamaen Online Shop</title></head><body>
<h1>Product Detail</h1>
<form action="https://x/1g36020c1" class="variations_form cart" method="post"
      data-product_id="22817" data-product_title="Kiwami Choan">
 <div class="product-form-rows">
  <div class="product-form-row woocommerce-variation woocommerce-variation-22822" data-variation_id="22822">
   <div class="product-attributes">
     <dl class="pa pa-sku"><dt>SKU</dt><dd>1G36020C1</dd></dl>
     <dl class="pa pa-size"><dt>Size</dt><dd>20g can</dd></dl></div>
   <div class="price">
     <span class="woocs_price_code woocs_price_JPY"><bdi>&yen;12,600</bdi></span>
     <span class="woocs_price_code woocs_price_EUR"><bdi>&euro;68.50</bdi></span></div>
   <div class="woocommerce-variation-add-to-cart variations_button">
     <p class="stock out-of-stock">Out of stock</p>
     <button class="single_add_to_cart_button button" disabled type="submit">Add to cart</button></div>
  </div>
  <div class="product-form-row woocommerce-variation woocommerce-variation-55089" data-variation_id="55089">
   <div class="product-attributes">
     <dl class="pa pa-sku"><dt>SKU</dt><dd>1G36040C1</dd></dl>
     <dl class="pa pa-size"><dt>Size</dt><dd>40g can</dd></dl></div>
   <div class="price">
     <span class="woocs_price_code woocs_price_JPY"><bdi>&yen;24,720</bdi></span>
     <span class="woocs_price_code woocs_price_EUR"><bdi>&euro;134.40</bdi></span></div>
   <div class="woocommerce-variation-add-to-cart variations_button">
     <p class="stock out-of-stock">Out of stock</p>
     <button class="single_add_to_cart_button button" disabled type="submit">Add to cart</button></div>
  </div>
 </div>
</form></body></html>
"""

# Balisage REEL d'une taille disponible (Low Caffeine Matcha, page enregistree en
# session authentifiee). Point cle : le libelle affiche "Limit one per person" et
# NON "In stock" — se fier au texte donnerait un faux negatif.
FIXTURE_REAL_IN = """
<html><head><title>Low Caffeine Matcha | Matcha | Marukyu Koyamaen Online Shop</title></head><body>
<h1>Product Detail</h1>
<form action="https://x/1f94020c1" class="variations_form cart" method="post"
      data-product_id="23001" data-product_title="Low Caffeine Matcha">
 <div class="product-form-rows">
  <div class="product-form-row woocommerce-variation" data-variation_id="23002">
   <div class="product-attributes">
     <dl class="pa pa-sku"><dt>SKU</dt><dd>1F94020C1</dd></dl>
     <dl class="pa pa-size"><dt>Size</dt><dd>20g can</dd></dl></div>
   <div class="price">
     <span class="woocs_price_code woocs_price_JPY"><bdi>&yen;2,200</bdi></span>
     <span class="woocs_price_code woocs_price_EUR"><bdi>&euro;11.96</bdi></span></div>
   <div class="product-form-input-block woocommerce-variation-add-to-cart variations_button">
     <p class="stock in-stock sold-individually amount-2">Limit one per person</p>
     <div class="quantity sold_individually" data-title="Limit one per person">
       <input class="qty" name="quantity[size]" readonly="readonly" type="number" value="1"/></div>
     <button class="single_add_to_cart_button button" type="submit">Add to cart</button></div>
  </div>
  <div class="product-form-row woocommerce-variation" data-variation_id="23003">
   <div class="product-attributes">
     <dl class="pa pa-sku"><dt>SKU</dt><dd>1F94040C1</dd></dl>
     <dl class="pa pa-size"><dt>Size</dt><dd>40g can</dd></dl></div>
   <div class="price">
     <span class="woocs_price_code woocs_price_JPY"><bdi>&yen;4,120</bdi></span>
     <span class="woocs_price_code woocs_price_EUR"><bdi>&euro;22.40</bdi></span></div>
   <div class="product-form-input-block woocommerce-variation-add-to-cart variations_button">
     <p class="stock out-of-stock">Out of stock</p>
     <button class="single_add_to_cart_button button" disabled type="submit">Add to cart</button></div>
  </div>
 </div>
</form></body></html>
"""

FIXTURE_GUEST = """
<html><head><title>Yugen | Matcha | Marukyu Koyamaen Online Shop</title></head><body>
<h1>Product Detail</h1>
<div class="notice">You must register and login to shop.</div>
</body></html>
"""

FIXTURE_WHOLE_OUT = """
<html><head><title>Aoarashi | Matcha | Marukyu Koyamaen Online Shop</title></head><body>
<h1>Product Detail</h1>
<p class="stock out-of-stock">This product is currently out of stock and unavailable.</p>
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

    p = parse_product(FIXTURE_REAL_OUT, "http://x/1")
    check(p.name == "Kiwami Choan", "nom lu depuis data-product_title (pas le h1 'Product Detail')",
          f"nom incorrect : {p.name!r}")
    check(len(p.variants) == 2, "les 2 lignes de variante sont trouvees", f"{len(p.variants)} variante(s)")
    v = p.variants[0]
    check(v.label == "20g can" and v.sku == "1G36020C1",
          "taille et SKU extraits", f"taille/SKU incorrects : {v.label!r} / {v.sku!r}")
    check(v.price_jpy == "¥12,600" and v.price_eur == "€68.50",
          "prix JPY et EUR extraits", f"prix incorrects : {v.price_jpy!r} / {v.price_eur!r}")
    check(all(x.in_stock is False for x in p.variants),
          "rupture par taille detectee (classe out-of-stock + bouton disabled)", "rupture non detectee")
    check(not p.any_in_stock, "any_in_stock = False", "any_in_stock incorrect")

    p2 = parse_product(FIXTURE_REAL_IN, "http://x/1")
    check(p2.name == "Low Caffeine Matcha", "second produit reel identifie", f"nom : {p2.name!r}")
    states = {x.label: x.in_stock for x in p2.variants}
    check(states == {"20g can": True, "40g can": False},
          "disponibilite differenciee taille par taille sur du HTML reel",
          f"etats incorrects : {states}")
    check(p2.variants[0].limit == "Limit one per person",
          "limite d'achat capturee (« Limit one per person »)",
          f"limite non lue : {p2.variants[0].limit!r}")
    check(p2.variants[0].in_stock is True,
          "libelle 'Limit one per person' lu comme DISPONIBLE (classe, pas texte)",
          "faux negatif sur un libelle non standard")

    try:
        parse_product(FIXTURE_GUEST, "http://x/2")
        check(False, "", "page non connectee non detectee")
    except NotAuthenticated:
        check(True, "page 'non connecte' detectee et signalee", "")

    p3 = parse_product(FIXTURE_WHOLE_OUT, "http://x/3")
    check(p3.variants[0].in_stock is False, "rupture totale detectee", "rupture totale ratee")

    # Etat precedent : la meme taille etait en rupture au run d'avant.
    prev = {"http://x/1#1f94020c1": {"in_stock": False, "product": "Low Caffeine Matcha",
                                     "variant": "20g can", "price": None, "url": "http://x/1"}}
    cur = snapshot([p2])
    evs = diff(prev, cur, report_sold_out=False)
    check(len(evs) == 1 and evs[0].kind == "RESTOCK",
          "transition rupture -> dispo declenche un RESTOCK", f"diff incorrect : {evs}")
    check(not diff({}, cur, report_sold_out=False),
          "premier run silencieux (pas de fausse alerte)", "le premier run alerte a tort")
    check(looks_like_challenge("<html><title>Just a moment...</title>"),
          "page de verification anti-bot reconnue", "challenge non reconnu")

    found = [("https://s/english/shop/products/1161020c1", "Wako ¥2,400"),
             ("https://s/english/shop/products/1171020c1", "Yugen ¥2,000"),
             ("https://s/english/shop/products/1111020c1", "Tenju ¥20,100")]
    check(len(select_targets(found, ["wako", "yugen"])) == 2,
          "filtre applique AVANT telechargement (2 fiches au lieu de 3)",
          "le filtre pre-telechargement ne selectionne pas correctement")
    check(len(select_targets([(u, "") for u, _ in found], ["wako"])) == 3,
          "repli sur la liste complete si le catalogue n'expose pas de libelles",
          "un catalogue sans libelles produirait une surveillance vide")

    # --- Push MatchAlert -------------------------------------------------- #

    ts = api_timestamp(datetime(2026, 8, 14, 10, 13, 0, tzinfo=timezone.utc))
    check(ts == "2026-08-14T10:13:00",
          "horodatage API sans offset (format LocalDateTime cote Java)",
          f"format inattendu : {ts!r}")

    # Firestore trie `time` comme une CHAINE : deux horodatages ecrits depuis
    # des fuseaux differents doivent rester dans l'ordre chronologique.
    tot = api_timestamp(datetime(2026, 8, 14, 2, 30, tzinfo=timezone(timedelta(hours=2))))
    tard = api_timestamp(datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc))
    check(tot < tard,
          "tri lexicographique = tri chronologique, quel que soit le fuseau",
          f"ordre casse : {tot!r} devrait preceder {tard!r}")

    payload = availability_payload([p2], "2026-08-14T10:13:00")
    check(len(payload) == 2 and {d["size"] for d in payload} == {"20g can", "40g can"},
          "chaque taille devient un document matchaAvailability",
          f"payload inattendu : {payload}")
    check(all(set(d) == {"nom", "size", "time", "isAvailable"} for d in payload),
          "champs exactement conformes au modele MatchaAvailability du backend",
          f"champs inattendus : {[sorted(d) for d in payload]}")
    check(next(d for d in payload if d["size"] == "20g can")["isAvailable"] is True,
          "isAvailable envoye en booleen JSON (pas 'true'/'yes')",
          "isAvailable mal type")

    inconnu = Product("Test", "http://x/9", [
        Variant("20g can", None), Variant("40g can", True)])
    payload2 = availability_payload([inconnu], "2026-08-14T10:13:00")
    check(len(payload2) == 1 and payload2[0]["size"] == "40g can",
          "taille indeterminable ecartee (jamais poussee comme rupture)",
          f"une taille inconnue a ete poussee : {payload2}")

    doublon = Product("Test", "http://x/9", [
        Variant("20g can", False), Variant("20g can", True)])
    payload3 = availability_payload([doublon], "2026-08-14T10:13:00")
    check(len(payload3) == 1 and payload3[0]["isAvailable"] is True,
          "doublon (nom, size) reduit au dernier etat dans un meme lot",
          f"doublon non reduit : {payload3}")

    # --- Push du catalogue ------------------------------------------------ #

    catalog = catalog_payload([p2])
    check(all(set(d) == {"magasin", "nom", "size", "url"} for d in catalog),
          "payload catalogue conforme au modele Matcha du backend",
          f"champs inattendus : {[sorted(d) for d in catalog]}")
    check(all(d["magasin"] == MAGASIN_ID for d in catalog),
          "magasin Marukyu envoye explicitement (plus de repli implicite cote serveur)",
          "magasin manquant ou incorrect")
    # Invariant critique : sans une correspondance exacte, le backend ne
    # retrouve pas le produit et l'alerte de restock repart sur l'URL de repli.
    check({(d["nom"], d["size"]) for d in catalog}
          >= {(d["nom"], d["size"]) for d in payload},
          "catalogue et disponibilites partagent exactement les memes (nom, size)",
          "divergence (nom, size) entre catalogue et disponibilites")
    # Une taille indeterminable est ecartee des disponibilites mais conservee
    # au catalogue : elle n'affirme aucun stock, elle rend le produit visible.
    catalog2 = catalog_payload([inconnu])
    check(len(catalog2) == 2 and len(payload2) == 1,
          "taille indeterminable conservee au catalogue mais absente des disponibilites",
          f"catalogue={len(catalog2)} disponibilites={len(payload2)}")

    print("\n" + ("TOUS LES TESTS PASSENT" if failures == 0 else f"{failures} ECHEC(S)"))
    return 0 if failures == 0 else 1


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def run(args, dmin: float, dmax: float) -> tuple[int, str]:
    """Le releve proprement dit. Retourne (code de sortie, resume pour le log).

    Le resume est la description envoyee a /api/scrapper/log : il doit tenir en
    une ligne lisible dans la collection `logs`, sans stack trace.
    """
    session = build_session(args.cookies)

    try:
        found = discover_products(session, verbose=args.verbose)
        if not found:
            msg = "Aucune fiche produit trouvee dans le catalogue."
            print(msg, file=sys.stderr)
            return 1, msg

        wanted = [w.strip().lower() for w in args.only.split(",") if w.strip()]
        urls = select_targets(found, wanted, verbose=args.verbose)

        if not args.no_login:
            if not probe_authenticated(session, urls[0], verbose=args.verbose):
                user, pwd = os.environ.get("MKY_USER"), os.environ.get("MKY_PASS")
                if not (user and pwd):
                    print(
                        "Session non connectee, et MKY_USER / MKY_PASS ne sont pas definis.\n"
                        "Sans connexion le site n'affiche pas le detail des tailles.\n"
                        "Definissez les variables d'environnement, ou exportez vos cookies\n"
                        f"de navigateur vers {args.cookies}. Pour un releve degrade : --no-login",
                        file=sys.stderr)
                    return 1, "Session non connectee et identifiants absents (MKY_USER / MKY_PASS)."
                login(session, user, pwd, verbose=args.verbose)
                if not probe_authenticated(session, urls[0], verbose=args.verbose):
                    print("Connexion effectuee mais les fiches restent en mode invite.\n"
                          "Verifiez les identifiants, ou passez par l'export de cookies.",
                          file=sys.stderr)
                    return 1, "Connexion acceptee mais les fiches restent en mode invite."
            elif args.verbose:
                print("[info] session deja authentifiee (cookies reutilises)")

        if not args.quiet:
            print(f"{len(found)} fiches au catalogue, {len(urls)} a relever"
                  + (f" (filtre : {', '.join(wanted)})" if wanted else ""))

        targets = urls
        products: list[Product] = []
        for i, url in enumerate(targets, 1):
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            try:
                p = parse_product(polite_get(session, url, verbose=args.verbose), url)
            except NotAuthenticated as exc:
                print(f"\n{exc}", file=sys.stderr)
                return 1, f"Session perdue en cours de releve : {exc}"

            if wanted and not any(w in p.name.lower() or w in slug.lower() for w in wanted):
                if i < len(targets):
                    time.sleep(random.uniform(dmin, dmax) * 0.3)
                continue

            products.append(p)
            if not args.quiet:
                dispo = [v.label for v in p.variants if v.in_stock]
                rupture = [v.label for v in p.variants if v.in_stock is False]
                inconnu = [v.label for v in p.variants if v.in_stock is None]
                etat = ("DISPO : " + ", ".join(dispo)) if dispo else "rupture"
                print(f"  [{i:>2}/{len(targets)}] {p.name:<46} "
                      f"{etat}  ({len(dispo)}/{len(p.variants)} dispo)")
                # Detail par taille. Sans lui, une rupture totale et un produit
                # dont aucune taille n'a ete parsee s'affichent exactement pareil
                # ("rupture" tout court) : le compteur et les libelles rendent la
                # difference visible dans le resume de l'Action.
                if rupture:
                    print(f"{'':9}rupture     : {', '.join(rupture)}")
                if inconnu:
                    # Ces tailles sont ECARTEES du push en base (availability_payload) :
                    # les pousser en `false` provoquerait une fausse alerte de restock.
                    print(f"{'':9}indetermine : {', '.join(inconnu)}  (non pousse en base)")
            if i < len(targets):
                time.sleep(random.uniform(dmin, dmax))

        save_cookies(session)

    except ChallengeDetected as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1, f"Page de verification anti-bot : {exc}"
    except (NotAuthenticated, RuntimeError) as exc:
        print(f"\nErreur : {exc}", file=sys.stderr)
        return 1, f"{type(exc).__name__} : {exc}"

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
    # que l'alerte parte. L'inverse ferait rater le restock, qui est l'objet
    # meme du script.
    if push_enabled(args):
        # Le catalogue precede les disponibilites : sans lui, un produit
        # nouvellement apparu n'aurait pas de ligne matcha, donc pas de lien
        # d'achat dans l'alerte qui suit immediatement.
        push_catalog(products, args)
    db_note = push_availabilities(products, args) if push_enabled(args) else ""

    n_sizes = sum(len(p.variants) for p in products)
    summary = f"{len(products)} produit(s), {n_sizes} taille(s) relevee(s)"

    if events:
        print("\n" + "=" * 64)
        for e in events:
            print(e.line())
        print("=" * 64)
        notify(events, verbose=args.verbose)
        detail = " ; ".join(f"{e.kind} {e.product} {e.variant}" for e in events)
        summary = f"{summary} ; {len(events)} changement(s) : {detail}{db_note}"
        return (2 if any(e.kind == "RESTOCK" for e in events) else 0), summary

    if not args.quiet:
        print(f"\nAucun changement ({stamp})")
    return 0, f"{summary} ; aucun changement{db_note}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Releve le stock des matcha Marukyu-Koyamaen (session authentifiee).")
    ap.add_argument("--only", default="",
                    help="filtre par nom, separe par des virgules (ex: wako,yugen,isuzu,aoarashi)")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--cookies", type=Path, default=DEFAULT_COOKIES,
                    help="fichier cookies Netscape, reutilise entre les runs")
    ap.add_argument("--json", type=Path, help="ecrit le releve complet en JSON")
    ap.add_argument("--csv", type=Path, help="ecrit le releve complet en CSV")
    ap.add_argument("--delay", default="4-9", help="pause entre deux fiches (N ou MIN-MAX)")
    ap.add_argument("--sold-out-too", action="store_true")
    ap.add_argument("--no-login", action="store_true",
                    help="ne pas se connecter (releve degrade, tailles inconnues)")
    ap.add_argument("--no-push", action="store_true",
                    help="ne rien envoyer a MatchAlert, meme si l'API est configuree")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true", help="teste le parsing hors ligne puis quitte")
    ap.add_argument("--parse-file", type=Path,
                    help="analyse une page HTML enregistree et affiche le resultat (aucun reseau)")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.parse_file:
        html_text = args.parse_file.read_text(encoding="utf-8", errors="replace")
        try:
            p = parse_product(html_text, str(args.parse_file))
        except NotAuthenticated as exc:
            print(f"Page non connectee : {exc}")
            return 1
        print(f"{p.name}   ({len(p.variants)} taille(s)){'  — ' + p.note if p.note else ''}")
        for v in p.variants:
            etat = "?" if v.in_stock is None else ("DISPO" if v.in_stock else "rupture")
            lim = f"  [{v.limit}]" if v.limit else ""
            print(f"  {etat:<8} {v.label:<14} {v.price_jpy or '':<10} "
                  f"{v.price_eur or '':<10} {v.sku or ''}{lim}")
        return 0

    try:
        lo, _, hi = args.delay.partition("-")
        dmin, dmax = float(lo), float(hi or lo)
    except ValueError:
        ap.error("--delay attend 'N' ou 'MIN-MAX'")

    if args.verbose and not push_enabled(args):
        print("[info] MatchAlert desactive : "
              + ("--no-push" if args.no_push else
                 "MATCHALERT_API_URL / SCRAPPER_API_KEY non definis"))

    code, summary = run(args, dmin, dmax)

    if push_enabled(args):
        push_log("SUCCESS" if code in (0, 2) else "ERROR", summary, verbose=args.verbose)

    return code


if __name__ == "__main__":
    sys.exit(main())
