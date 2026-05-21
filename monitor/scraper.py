"""
Scraping de la page promo Amazon via Playwright (Chromium headless).

Extraction DOM des produits, de leur disponibilite et du delai de livraison.
Compatible Ubuntu : Chromium headless standard, sans channel="chrome".
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, Tuple

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
)

from monitor import config
from monitor.logging_utils import log


PRODUCT_SELECTOR = "[data-asin]"


# ---------------------------------------------------------------------------
# Contexte navigateur
# ---------------------------------------------------------------------------


async def create_browser_context(
    playwright: Playwright,
) -> Tuple[Browser, BrowserContext]:
    launch_kwargs: dict = {
        "headless": True,
        "args": [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if config.CHROMIUM_EXECUTABLE:
        launch_kwargs["executable_path"] = config.CHROMIUM_EXECUTABLE
    browser: Browser = await playwright.chromium.launch(**launch_kwargs)
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

    document.querySelectorAll("[data-asin]").forEach((el, i) => {
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

        const availEl = el.querySelector('[data-name="availabilityMessage"]');
        let disponibilite = '';
        if (availEl) {
            const clone = availEl.cloneNode(true);
            clone.querySelectorAll('script, style').forEach(s => s.remove());
            disponibilite = clone.textContent.trim();
        }

        const delivEl = el.querySelector('[name="productDeliveryBox"]');
        let deliveryText = '';
        if (delivEl) {
            const clone = delivEl.cloneNode(true);
            clone.querySelectorAll('script, style').forEach(s => s.remove());
            deliveryText = clone.textContent.trim().replace(/\s+/g, ' ');
        }

        const pricePayEl = el.querySelector('div[name="productPriceBox"] [name="productPriceToPay"]');
        let prix = '';
        if (pricePayEl) {
            prix = pricePayEl.getAttribute('aria-label') || '';
        }
        if (!prix) {
            const priceBoxEl = el.querySelector('div[name="productPriceBox"]');
            prix = priceBoxEl ? priceBoxEl.textContent.trim() : '';
        }

        products[asin] = {
            titre,
            lien,
            image,
            prix,
            position: i,
            disponibilite,
            deliveryText,
        };
    });

    return Object.fromEntries(
        Object.entries(products).filter(([, item]) => item.titre && item.titre.trim().length > 0)
    );
}
"""


# ---------------------------------------------------------------------------
# Disponibilite Python (portage de determineAvailability de furtys/scraper.js)
# ---------------------------------------------------------------------------


def determine_availability(delivery_text: str, availability_text: str) -> bool:
    text_lower = (delivery_text or "").lower().strip()
    avail_lower = (availability_text or "").lower().strip()

    if "indisponible" in avail_lower or "rupture de stock" in avail_lower:
        return False

    cleaned = text_lower
    cleaned = re.sub(r"livraison gratuite pour les membres prime", "", cleaned)
    cleaned = re.sub(r"livraison gratuite", "", cleaned)
    cleaned = re.sub(r"pour les membres prime", "", cleaned)
    cleaned = re.sub(r"livraison standard", "", cleaned)
    cleaned = re.sub(r"livraison", "", cleaned)
    cleaned = re.sub(r"gratuite", "", cleaned)
    cleaned = re.sub(r"\d+([,.]\d+)?\s*€", "", cleaned)
    cleaned = cleaned.strip()

    months = r"(janv|févr|mar|avr|mai|juin|juil|août|sept|oct|nov|déc)"
    days = r"(lundi|mardi|mercredi|jeudi|vendredi|samedi|dimanche|demain)"
    if re.search(months, cleaned, re.IGNORECASE) or re.search(days, cleaned, re.IGNORECASE):
        return True

    if "en stock" in avail_lower or "disponible" in avail_lower:
        if "habituellement" not in avail_lower and "expédié sous" not in avail_lower:
            return True

    return False


# ---------------------------------------------------------------------------
# Point d'entree scraping
# ---------------------------------------------------------------------------


async def scrape_products(page: Page) -> Dict[str, Dict[str, Any]]:
    cb = int(time.time() * 1000)
    sep = "&" if "?" in config.TARGET_URL else "?"
    url = f"{config.TARGET_URL}{sep}cb={cb}"

    await page.goto(url, wait_until="networkidle", timeout=60000)

    try:
        await page.wait_for_function(
            "() => { const el = document.querySelector('[data-name=\"productTitle\"]'); return el && el.textContent.trim().length > 5; }",
            timeout=10000,
        )
        await page.wait_for_timeout(300)
    except Exception:
        title = await page.title()
        if "Robot Check" in title or "CAPTCHA" in title:
            log("⚠️ CAPTCHA détecté — reset navigateur au prochain cycle")
            return {}
        log("⚠️ Hydration timeout — on continue quand même")

    try:
        btn = page.locator("#sp-cc-accept")
        if await btn.count() > 0:
            await btn.click()
            await page.wait_for_timeout(500)
    except Exception:
        pass

    try:
        await page.wait_for_selector(PRODUCT_SELECTOR, timeout=30000)
    except Exception:
        log("⚠️ Aucun produit detecte dans le DOM (probable anti-bot)")
        return {}

    result = await page.evaluate(_EXTRACT_JS)
    if not isinstance(result, dict):
        return {}

    for asin, product in result.items():
        product["commandable"] = determine_availability(
            product.get("deliveryText", ""),
            product.get("disponibilite", ""),
        )

    with_cmd = sum(1 for p in result.values() if p.get("commandable"))
    log(f"  → {len(result)} produits extraits — {with_cmd} commandables")
    return result
