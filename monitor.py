"""
Amazon Promotion Page Monitor v2.0

Surveille https://www.amazon.fr/promotion/psp/A26013IELTKPDW toutes les
INTERVAL_SECONDS secondes et notifie via webhook Discord lorsqu'un nouveau
produit apparait, passe en tete de liste, change de disponibilite ou devient
commandable.
"""

from __future__ import annotations

import asyncio
import json
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
INTERVAL_SECONDS = 5

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

# Selecteurs — bases sur la structure HTML reelle de la page promo Amazon
PRODUCT_SELECTOR = "li.productGrid[data-asin]"
DELIVERY_SELECTOR = ".udm-primary-delivery-message"
SEE_MORE_SELECTOR = "a[href*='ref=_see_more']"

NORMAL_AVAILABILITY = {
    "Habituellement expédié sous 1 à 2 mois",
    "Habituellement expédié sous 3 à 7 mois",
    "Habituellement expédié sous 6 à 7 mois",
    "Habituellement expédié sous 1 à 3 mois",
}


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
# Scraping
# ---------------------------------------------------------------------------


async def _expand_all(page: Page) -> None:
    """Clique sur tous les boutons 'Afficher plus' jusqu'a epuisement."""
    clicks = 0
    while True:
        try:
            btn = page.locator(SEE_MORE_SELECTOR).first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            clicks += 1
        except Exception:
            break
    if clicks:
        log(f"  → {clicks} clic(s) 'Afficher plus'")


async def _wait_for_delivery_blocks(page: Page) -> None:
    """
    Poll le nombre de blocs livraison jusqu'a stabilisation.

    Amazon injecte .udm-primary-delivery-message en asynchrone apres le
    rendu initial. On attend que le count ne bouge plus pendant 2 secondes
    consecutives avant de lancer l'extraction.
    """
    prev_count = -1
    stable_ticks = 0
    for _ in range(60):  # timeout 30 s
        count = await page.locator(DELIVERY_SELECTOR).count()
        if count == prev_count:
            stable_ticks += 1
            if stable_ticks >= 4:  # stable depuis 2 s
                log(f"  → {count} bloc(s) livraison stables")
                return
        else:
            stable_ticks = 0
            prev_count = count
        await asyncio.sleep(0.5)
    log("  ⚠️ Timeout attente blocs livraison — extraction quand meme")


_EXTRACT_JS = """
() => {
    const BASE = "https://www.amazon.fr";
    const products = {};

    document.querySelectorAll("li.productGrid[data-asin]").forEach((el, i) => {
        const asin = (el.getAttribute("data-asin") || "").trim();
        if (!asin) return;

        const titleAnchor = el.querySelector("a[data-name='productTitle']");
        const title = titleAnchor?.textContent?.trim() || null;
        const rawHref = titleAnchor?.getAttribute("href") || null;
        const link = rawHref
            ? (rawHref.startsWith("http") ? rawHref : BASE + rawHref)
            : null;

        const image =
            el.querySelector("img[name='productImage']")?.getAttribute("src") ||
            null;

        const availability =
            el.querySelector("[data-name='availabilityMessage']")
              ?.textContent?.trim() || null;

        const deliveryBold = el.querySelector(
            ".udm-primary-delivery-message .a-text-bold"
        );
        const deliveryDate = deliveryBold?.textContent?.trim() || null;

        products[asin] = {
            titre: title,
            lien: link,
            image,
            position: i,
            disponibilite: availability,
            commandable: !!deliveryDate,
            dateLivraison: deliveryDate,
        };
    });

    return products;
}
"""


async def scrape_products(page: Page) -> Dict[str, Dict[str, Any]]:
    """Charge la page, attend le DOM complet et extrait tous les produits."""
    await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

    # Attente du grid produit
    try:
        await page.wait_for_selector(PRODUCT_SELECTOR, timeout=30000)
    except Exception:
        log("⚠️ Aucun produit detecte dans le DOM (probable anti-bot)")
        return {}

    nb = await page.locator(PRODUCT_SELECTOR).count()
    log(f"  → {nb} produit(s) charge(s)")

    await _expand_all(page)

    nb = await page.locator(PRODUCT_SELECTOR).count()
    await _wait_for_delivery_blocks(page)

    result = await page.evaluate(_EXTRACT_JS)
    if not isinstance(result, dict):
        return {}

    with_date = sum(1 for p in result.values() if p.get("commandable"))
    log(f"  → {len(result)} produits extraits — {with_date} commandables")
    return result


# ---------------------------------------------------------------------------
# Notification Discord
# ---------------------------------------------------------------------------


def _cart_url(asin: str) -> str:
    return (
        f"{AMAZON_BASE}/gp/aws/cart/add.html"
        f"?ASIN.1={asin}&Quantity.1=2&AssociateTag={ASSOCIATE_TAG}"
    )


def notify_discord(
    asin: str,
    product: Dict[str, Any],
    reason: str = "nouveau",
) -> None:
    title = (product.get("titre") or f"Produit {asin}")[:256]
    url = product.get("lien") or f"{AMAZON_BASE}/dp/{asin}"
    image = product.get("image")
    availability = product.get("disponibilite") or "Non renseignee"
    commandable = product.get("commandable", False)
    delivery_date = product.get("dateLivraison")

    orderable_line = (
        f"\n🟢 Commandable — livraison : **{delivery_date}**"
        if commandable and delivery_date
        else "\n🟢 Commandable"
        if commandable
        else "\n🔴 Non commandable"
    )

    if reason == "tete_de_liste":
        description = f"⬆️ Passe en tete de liste !{orderable_line}\nDisponibilite : {availability}"
    elif reason == "disponibilite":
        description = f"📦 Disponibilite mise a jour : **{availability}**{orderable_line}"
    elif reason == "commandable":
        description = "🛒 Produit maintenant commandable !"
        if delivery_date:
            description += f"\n📅 Livraison : **{delivery_date}**"
    else:
        description = f"Nouveau produit detecte sur la page promotionnelle Amazon !{orderable_line}"

    embed: Dict[str, Any] = {
        "title": title,
        "url": url,
        "color": EMBED_COLOR,
        "description": description,
        "fields": [
            {
                "name": "Panier",
                "value": f"[🛒 Ajouter au panier (x2)]({_cart_url(asin)})",
                "inline": True,
            }
        ],
        "footer": {
            "text": "Amazon Monitor • amazon.fr/promotion/psp/A26013IELTKPDW"
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
        log("⚠️ Aucun produit (probable anti-bot) — etat conserve")
        return state

    # Nouveaux produits
    for asin in [a for a in products if a not in state]:
        title = products[asin].get("titre") or asin
        log(f"🆕 Nouveau : {title} ({asin})")
        try:
            notify_discord(asin, products[asin], reason="nouveau")
        except Exception as exc:
            log(f"⚠️ Erreur notification : {exc}")

    # Produits deja connus
    for asin, product in products.items():
        if asin not in state:
            continue
        prev = state[asin]

        # Passage en tete de liste
        prev_pos = prev.get("position")
        curr_pos = product.get("position")
        if curr_pos == 0 and prev_pos not in (None, 0):
            title = product.get("titre") or asin
            log(f"⬆️ Tete de liste : {title} ({asin})")
            try:
                notify_discord(asin, product, reason="tete_de_liste")
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

        # Changement de disponibilite
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
                notify_discord(asin, product, reason="disponibilite")
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

        # Produit devient commandable
        if product.get("commandable") and not prev.get("commandable"):
            title = product.get("titre") or asin
            date_info = product.get("dateLivraison") or ""
            log(f"🛒 Commandable : {title} ({asin}) — {date_info}")
            try:
                notify_discord(asin, product, reason="commandable")
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

    save_state(products)
    return products


async def main() -> None:
    log("Demarrage du monitoring...")
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
                    log(f"Premier run — {len(products)} produits")
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
