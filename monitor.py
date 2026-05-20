"""
Amazon Promotion Page Monitor v3.0

Surveille https://www.amazon.fr/promotion/psp/A26013IELTKPDW en interceptant
l'API interne productInfoList — pas de scraping DOM, pas d'attente de
stabilisation, cycle en 3-5 secondes.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from playwright.async_api import async_playwright, Browser, BrowserContext, Page


# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

TARGET_URL = "https://www.amazon.fr/promotion/psp/A26013IELTKPDW"
DISCORD_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1504574897576480910/"
    "9x4V088uAeLHdDpiRm2QVV9nwkxb20wryvf4dnSu32CH8V9TIzQ_RMhfBM4yl8YNGCkV"
)
ASSOCIATE_TAG = "botbluray-21"
INTERVAL_SECONDS = 0

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1920, "height": 1080}
LOCALE = "fr-FR"
TIMEZONE = "Europe/Paris"
EXTRA_HEADERS = {"Accept-Language": "fr-FR,fr;q=0.9"}

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "state.json"
AMAZON_BASE = "https://www.amazon.fr"
EMBED_COLOR = 0xFF9900

PRODUCT_INFO_URL = "promotion/psp/productInfoList"

# Merchant IDs correspondant a Amazon directement (pas vendeurs tiers)
AMAZON_MERCHANT_IDS = {"A1X6FK5RDHNB96"}

NORMAL_AVAILABILITY = {
    "Habituellement expédié sous 1 à 2 mois",
    "Habituellement expédié sous 3 à 7 mois",
    "Habituellement expédié sous 6 à 7 mois",
    "Habituellement expédié sous 1 à 3 mois",
}

# Regex pour extraire la date de livraison du HTML deliveryBlock
_RE_DELIVERY_DATE = re.compile(
    r'class="[^"]*a-text-bold[^"]*"[^>]*>(.*?)</span>', re.DOTALL
)
_RE_DELIVERY_MSG = re.compile(
    r'udm-primary-delivery-message[^>]*>.*?<div[^>]*>(.*?)</div>', re.DOTALL
)
_RE_STRIP_TAGS = re.compile(r'<[^>]+>')


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{timestamp}] {message}", flush=True)
    except UnicodeEncodeError:
        safe = message.encode("ascii", errors="replace").decode("ascii")
        print(f"[{timestamp}] {safe}", flush=True)


# ---------------------------------------------------------------------------
# Persistance de l'etat
# ---------------------------------------------------------------------------


def load_state() -> Dict[str, Dict[str, Any]]:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️ Erreur lecture state.json : {exc}")
        return {}


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"⚠️ Erreur ecriture state.json : {exc}")


# ---------------------------------------------------------------------------
# Parsing API
# ---------------------------------------------------------------------------


def _parse_delivery_block(html: str) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Extrait (commandable, dateLivraison, messageLivraison) depuis le HTML
    du champ deliveryBlock retourne par l'API productInfoList.
    """
    if not html:
        return False, None, None

    date_match = _RE_DELIVERY_DATE.search(html)
    date = date_match.group(1).strip() if date_match else None

    msg_match = _RE_DELIVERY_MSG.search(html)
    if msg_match:
        raw = _RE_STRIP_TAGS.sub("", msg_match.group(1))
        msg = " ".join(raw.split())
    else:
        msg = None

    return bool(date), date, msg


def _parse_api_batches(batches: list[dict]) -> Dict[str, Dict[str, Any]]:
    """Construit le dict produits depuis les batches productInfoList."""
    products: Dict[str, Dict[str, Any]] = {}
    position = 0

    for batch in batches:
        items = batch.get("viewModels", {}).get("PRODUCT_INFO_LIST", [])
        for item in items:
            asin = (item.get("asin") or "").strip()
            if not asin:
                continue

            href = item.get("detailPageLink") or ""
            link = (AMAZON_BASE + href) if href else f"{AMAZON_BASE}/dp/{asin}"

            price_info = item.get("priceInfo") or {}
            price_to_pay = price_info.get("priceToPay") or {}
            prix = (price_to_pay.get("displayString") or "").replace("\xa0", " ").strip() or None

            commandable, date, msg = _parse_delivery_block(item.get("deliveryBlock") or "")

            products[asin] = {
                "titre": item.get("title") or None,
                "lien": link,
                "image": item.get("imgURL") or None,
                "position": position,
                "disponibilite": item.get("availabilityMessage") or None,
                "prix": prix,
                "merchantId": item.get("merchantId") or None,
                "offerListingId": item.get("offerListingId") or None,
                "canAddToCart": item.get("canAddToCart"),
                "blockATCAsin": item.get("blockATCAsin"),
                "prime": item.get("prime"),
                "commandable": commandable,
                "dateLivraison": date,
                "messageLivraison": msg,
            }
            position += 1

    return products


# ---------------------------------------------------------------------------
# Scraping via interception API
# ---------------------------------------------------------------------------


async def scrape_products(page: Page) -> Dict[str, Dict[str, Any]]:
    """
    Charge la page et intercepte les reponses productInfoList.
    Pas de scraping DOM — l'API livre tous les produits en 2 batches.
    """
    api_batches: list[dict] = []

    async def _on_response(response) -> None:
        if PRODUCT_INFO_URL not in response.url:
            return
        try:
            data = await response.json()
            nb = len(data.get("viewModels", {}).get("PRODUCT_INFO_LIST", []))
            log(f"  → batch API recu : {nb} produit(s)")
            api_batches.append(data)
        except Exception as exc:
            log(f"  ⚠️ Erreur parsing productInfoList : {exc}")

    page.on("response", _on_response)
    try:
        await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)
        # Petit delai pour les batches tardifs
        await asyncio.sleep(1)
    finally:
        page.remove_listener("response", _on_response)

    if not api_batches:
        log("⚠️ Aucun batch productInfoList recu (probable anti-bot)")
        return {}

    products = _parse_api_batches(api_batches)
    with_date = sum(1 for p in products.values() if p.get("commandable"))
    log(f"  → {len(products)} produits — {with_date} commandables")
    return products


# ---------------------------------------------------------------------------
# Notification Discord
# ---------------------------------------------------------------------------


def _cart_url(product: Dict[str, Any], asin: str) -> Optional[str]:
    """
    Retourne l'URL d'ajout au panier avec l'offerListingId specifique
    pour cibler l'offre Amazon exacte de la page promo.
    Retourne None si le produit n'est pas eligible (pas vendu par Amazon).
    """
    offer_id = product.get("offerListingId")
    merchant_id = product.get("merchantId")

    if not offer_id or merchant_id not in AMAZON_MERCHANT_IDS:
        return None

    return (
        f"{AMAZON_BASE}/gp/aws/cart/add.html"
        f"?OfferListingId.1={offer_id}&Quantity.1=2&AssociateTag={ASSOCIATE_TAG}"
    )


def notify_discord(
    asin: str,
    product: Dict[str, Any],
    reason: str = "nouveau",
    prev: Optional[Dict[str, Any]] = None,
) -> None:
    title = (product.get("titre") or f"Produit {asin}")[:256]
    url = product.get("lien") or f"{AMAZON_BASE}/dp/{asin}"
    image = product.get("image")
    prix = product.get("prix")
    commandable = product.get("commandable", False)
    delivery_date = product.get("dateLivraison")
    msg_livraison = product.get("messageLivraison")
    availability = product.get("disponibilite") or "Non renseignee"

    cart = _cart_url(product, asin)
    cart_link = (
        f"[🛒 Ajouter 2 ex. au panier (ATC)]({cart})"
        if cart
        else "⚠️ Vendu par un tiers — achat sur la page produit"
    )

    event_titles = {
        "nouveau":       "🆕 Nouveau produit détecté !",
        "commandable":   "🟢 Produit de retour en stock !",
        "tete_de_liste": "⬆️ Passé en tête de liste !",
        "disponibilite": "📦 Changement de disponibilité",
    }
    event_title = event_titles.get(reason, "ℹ️ Mise à jour produit")

    lines = [f"**{event_title}**", "", cart_link, ""]

    if reason == "disponibilite" and prev:
        prev_avail = prev.get("disponibilite") or "—"
        lines += [
            "**🔄 Changement de Statut**",
            f"🔴 Avant : *{prev_avail}*",
            f"🟢 Maintenant : **{availability}**",
            "",
        ]

    if reason == "commandable" and prev:
        prev_msg = prev.get("messageLivraison") or "Livraison non disponible"
        curr_msg = msg_livraison or (
            f"Livraison GRATUITE {delivery_date} pour les membres Prime"
            if delivery_date else "—"
        )
        lines += [
            "**🚚 Infos de Livraison**",
            f"🔴 Avant : *{prev_msg}*",
            f"🟢 Maintenant : **{curr_msg}**",
            "",
        ]
    elif commandable and msg_livraison:
        lines += [f"🟢 {msg_livraison}", ""]
    elif not commandable:
        lines += ["🔴 Non commandable", ""]

    description = "\n".join(lines).strip()

    fields = []
    if prix:
        fields.append({"name": "💰 Prix", "value": prix, "inline": True})
    fields.append({"name": "🆔 ASIN", "value": asin, "inline": True})

    embed: Dict[str, Any] = {
        "title": title,
        "url": url,
        "color": EMBED_COLOR,
        "description": description,
        "fields": fields,
        "footer": {
            "text": "Bot Amazon Promotion Bluray • amazon.fr/promotion/psp/A26013IELTKPDW"
        },
    }
    if image:
        embed["image"] = {"url": image}

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10,
        )
        if response.status_code >= 400:
            log(
                f"⚠️ Erreur Discord HTTP {response.status_code} : "
                f"{response.text[:200]}"
            )
    except requests.RequestException as exc:
        log(f"⚠️ Erreur Discord : {exc}")


# ---------------------------------------------------------------------------
# Boucle principale
# ---------------------------------------------------------------------------


async def run_cycle(
    page: Page,
    state: Dict[str, Dict[str, Any]],
    cycle_index: int,
) -> Dict[str, Dict[str, Any]]:
    try:
        products = await scrape_products(page)
    except Exception as exc:
        log(f"⚠️ Erreur scrape : {exc}")
        return state

    log(f"Cycle #{cycle_index} — {len(products)} produits")

    if not products:
        log("⚠️ Aucun produit — etat conserve")
        return state

    for asin in [a for a in products if a not in state]:
        p = products[asin]
        title = p.get("titre") or asin
        log(
            f"🆕 Nouveau : {title} ({asin}) | "
            f"canAddToCart={p.get('canAddToCart')} "
            f"blockATCAsin={p.get('blockATCAsin')} "
            f"prime={p.get('prime')} "
            f"merchantId={p.get('merchantId')}"
        )
        try:
            notify_discord(asin, p, reason="nouveau")
        except Exception as exc:
            log(f"⚠️ Erreur notification : {exc}")

    for asin, product in products.items():
        if asin not in state:
            continue
        prev = state[asin]

        if product.get("position") == 0 and prev.get("position") not in (None, 0):
            title = product.get("titre") or asin
            log(f"⬆️ Tete de liste : {title} ({asin})")
            try:
                notify_discord(asin, product, reason="tete_de_liste", prev=prev)
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

        prev_avail = prev.get("disponibilite")
        curr_avail = product.get("disponibilite")
        if (
            curr_avail
            and curr_avail not in NORMAL_AVAILABILITY
            and curr_avail != prev_avail
        ):
            title = product.get("titre") or asin
            log(f"📦 Disponibilite : {curr_avail} — {title} ({asin})")
            try:
                notify_discord(asin, product, reason="disponibilite", prev=prev)
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

        if product.get("commandable") and not prev.get("commandable"):
            title = product.get("titre") or asin
            log(f"🛒 Commandable : {title} ({asin}) — {product.get('dateLivraison', '')}")
            try:
                notify_discord(asin, product, reason="commandable", prev=prev)
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

    save_state(products)
    return products


async def main() -> None:
    log("Demarrage du monitoring v3.0 (mode API)...")
    state = load_state()
    initial_run = len(state) == 0 and not STATE_FILE.exists()

    async with async_playwright() as p:
        browser: Browser = await p.chromium.launch(headless=True)
        context: BrowserContext = await browser.new_context(
            user_agent=USER_AGENT,
            viewport=VIEWPORT,
            locale=LOCALE,
            timezone_id=TIMEZONE,
            extra_http_headers=EXTRA_HEADERS,
        )
        page: Page = await context.new_page()

        cycle_index = 0
        try:
            if initial_run:
                try:
                    products = await scrape_products(page)
                    log(f"Premier run — {len(products)} produits :")
                    for asin, p in products.items():
                        log(
                            f"  {p.get('titre') or asin} ({asin}) | "
                            f"canAddToCart={p.get('canAddToCart')} "
                            f"blockATCAsin={p.get('blockATCAsin')} "
                            f"prime={p.get('prime')} "
                            f"merchantId={p.get('merchantId')}"
                        )
                    if products:
                        state = products
                        save_state(state)
                    else:
                        log("⚠️ Aucun produit au premier run (probable anti-bot)")
                except Exception as exc:
                    log(f"⚠️ Erreur : {exc}")

            while True:
                await asyncio.sleep(INTERVAL_SECONDS)
                cycle_index += 1
                state = await run_cycle(page, state, cycle_index)
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Monitoring arrete.")
        sys.exit(0)
