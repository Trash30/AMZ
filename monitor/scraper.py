"""
Scraping de la page promo Amazon via Playwright (Chromium headless).

Extraction DOM des produits, de leur disponibilite et du delai de livraison.
Compatible Ubuntu : Chromium headless standard, sans channel="chrome".
"""

from __future__ import annotations

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


PRODUCT_SELECTOR = "li.productGrid[data-asin]"


# ---------------------------------------------------------------------------
# Contexte navigateur
# ---------------------------------------------------------------------------


async def create_browser_context(
    playwright: Playwright,
) -> Tuple[Browser, BrowserContext]:
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
# Extracteur DOM (execute dans le contexte de la page)
# ---------------------------------------------------------------------------

_EXTRACT_JS = """
() => {
    const BASE = "https://www.amazon.fr";
    const products = {};

    document.querySelectorAll("li.productGrid[data-asin]").forEach((el, i) => {
        const asin = (el.getAttribute("data-asin") || "").trim();
        if (!asin) return;

        const titleAnchor = el.querySelector("a[data-name='productTitle']");
        const titre = titleAnchor?.textContent?.trim() || null;
        const rawHref = titleAnchor?.getAttribute("href") || null;
        const lien = rawHref
            ? (rawHref.startsWith("http") ? rawHref : BASE + rawHref)
            : null;

        const image =
            el.querySelector("img[name='productImage']")?.getAttribute("src") ||
            null;

        const disponibilite =
            el.querySelector("[data-name='availabilityMessage']")
              ?.textContent?.trim() || null;

        const deliveryBold = el.querySelector(
            ".udm-primary-delivery-message .a-text-bold"
        );
        const dateLivraison = deliveryBold?.textContent?.trim() || null;

        products[asin] = {
            titre,
            lien,
            image,
            position: i,
            disponibilite,
            dateLivraison,
            commandable: !!dateLivraison,
        };
    });

    return products;
}
"""


# ---------------------------------------------------------------------------
# Point d'entree scraping
# ---------------------------------------------------------------------------


async def scrape_products(page: Page) -> Dict[str, Dict[str, Any]]:
    await page.goto(config.TARGET_URL, wait_until="networkidle", timeout=60000)

    try:
        await page.wait_for_selector(PRODUCT_SELECTOR, timeout=30000)
    except Exception:
        log("⚠️ Aucun produit detecte dans le DOM (probable anti-bot)")
        return {}

    result = await page.evaluate(_EXTRACT_JS)
    if not isinstance(result, dict):
        return {}

    with_date = sum(1 for p in result.values() if p.get("commandable"))
    log(f"  → {len(result)} produits extraits — {with_date} commandables")
    return result
