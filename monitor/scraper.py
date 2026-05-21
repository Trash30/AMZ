"""
Scraping de la page promo Amazon via Playwright (Chromium headless).

Extraction DOM des produits, de leur disponibilite, du delai de livraison
et de l'offerListingId necessaire a l'URL d'ajout au panier.

Compatible Ubuntu : Chromium headless standard, sans channel="chrome".
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Tuple

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from monitor import config
from monitor.logging_utils import log


# Selecteurs — bases sur la structure HTML reelle de la page promo Amazon
PRODUCT_SELECTOR = "li.productGrid[data-asin]"
DELIVERY_SELECTOR = ".udm-primary-delivery-message"
SEE_MORE_SELECTOR = "a[href*='ref=_see_more']"


# ---------------------------------------------------------------------------
# Contexte navigateur
# ---------------------------------------------------------------------------


async def create_browser_context(
    playwright: Playwright,
) -> Tuple[Browser, BrowserContext]:
    """Lance Chromium headless et cree un contexte pret a scraper.

    Charge les cookies de session depuis amazon_session.json si present.
    Aucun channel="chrome" : Chromium embarque par Playwright, compatible
    avec un serveur Ubuntu headless.
    """
    browser: Browser = await playwright.chromium.launch(headless=True)
    context: BrowserContext = await browser.new_context(
        user_agent=config.USER_AGENT,
        viewport=config.VIEWPORT,
        locale=config.LOCALE,
        timezone_id=config.TIMEZONE,
        extra_http_headers=config.EXTRA_HEADERS,
    )

    if config.SESSION_FILE.exists():
        try:
            with config.SESSION_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            cookies = data.get("cookies") if isinstance(data, dict) else data
            if cookies:
                await context.add_cookies(cookies)
                log(f"  → {len(cookies)} cookie(s) de session charge(s)")
        except (json.JSONDecodeError, OSError, KeyError) as exc:
            log(f"⚠️ Erreur lecture amazon_session.json : {exc}")

    return browser, context


# ---------------------------------------------------------------------------
# Helpers de chargement de page
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
    """Poll le nombre de blocs livraison jusqu'a stabilisation.

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


# ---------------------------------------------------------------------------
# Extracteur DOM (execute dans le contexte de la page)
# ---------------------------------------------------------------------------

_EXTRACT_JS = """
() => {
    const BASE = "https://www.amazon.fr";
    const products = {};

    const extractOfferListingId = (el) => {
        // 1. lien contenant OfferListingId dans la query string
        const anchor = el.querySelector("a[href*='OfferListingId']");
        if (anchor) {
            const href = anchor.getAttribute("href") || "";
            const match = href.match(/OfferListingId(?:\\.\\d+)?=([^&]+)/i);
            if (match && match[1]) {
                return decodeURIComponent(match[1]);
            }
        }
        // 2. attribut data-offer-listing-id sur l'element lui-meme
        const own = el.getAttribute("data-offer-listing-id");
        if (own) return own;
        // 3. attribut data-offer-listing-id sur un descendant
        const child = el.querySelector("[data-offer-listing-id]");
        if (child) {
            const cid = child.getAttribute("data-offer-listing-id");
            if (cid) return cid;
        }
        return null;
    };

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

        const offerListingId = extractOfferListingId(el);

        products[asin] = {
            titre: title,
            lien: link,
            image,
            position: i,
            disponibilite: availability,
            offerListingId: offerListingId,
            commandable: !!deliveryDate,
            dateLivraison: deliveryDate,
        };
    });

    return products;
}
"""


# ---------------------------------------------------------------------------
# Point d'entree scraping
# ---------------------------------------------------------------------------


async def scrape_products(page: Page) -> Dict[str, Dict[str, Any]]:
    """Charge la page, attend le DOM complet et extrait tous les produits."""
    await page.goto(config.TARGET_URL, wait_until="networkidle", timeout=60000)

    try:
        await page.wait_for_selector(PRODUCT_SELECTOR, timeout=30000)
    except Exception:
        log("⚠️ Aucun produit detecte dans le DOM (probable anti-bot)")
        return {}

    nb = await page.locator(PRODUCT_SELECTOR).count()
    log(f"  → {nb} produit(s) charge(s)")

    await _expand_all(page)
    await _wait_for_delivery_blocks(page)

    result = await page.evaluate(_EXTRACT_JS)
    if not isinstance(result, dict):
        return {}

    with_date = sum(1 for p in result.values() if p.get("commandable"))
    log(f"  → {len(result)} produits extraits — {with_date} commandables")
    return result
