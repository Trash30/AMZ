"""
Amazon Promotion Page Monitor.

Surveille la page promotionnelle Amazon
https://www.amazon.fr/promotion/psp/A26013IELTKPDW toutes les 10 secondes
et notifie via webhook Discord lorsqu'un nouveau produit apparait.
"""

from __future__ import annotations

import asyncio
import json
import os
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

PRODUCT_SELECTORS = [
    "li.productGrid[data-asin]",  # structure reelle de la page promo Amazon
    "li[data-asin]",
    "div.s-result-item[data-asin]",
    "div[data-asin]",
]

TITLE_SELECTORS = [
    "a[data-name='productTitle']",  # lien titre direct sur la page promo
    "h2 a span", "h2 span",
    ".a-size-medium", ".a-size-base-plus",
    "[data-cy='title-recipe'] span", ".a-text-normal",
    "span.a-truncate-full", "span.a-truncate-cut",
]
LINK_SELECTORS = ["h2 a", "a.a-link-normal"]

AMAZON_BASE = "https://www.amazon.fr"
EMBED_COLOR = 0xFF9900

NORMAL_AVAILABILITY = {
    "Habituellement expédié sous 1 à 2 mois",
    "Habituellement expédié sous 3 à 7 mois",
    "Habituellement expédié sous 6 à 7 mois",
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    """Affiche un message horodate sur stdout."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{timestamp}] {message}", flush=True)
    except UnicodeEncodeError:
        # Fallback si le terminal Windows ne supporte pas certains emojis.
        safe = message.encode("ascii", errors="replace").decode("ascii")
        print(f"[{timestamp}] {safe}", flush=True)


# ---------------------------------------------------------------------------
# Persistance de l'etat
# ---------------------------------------------------------------------------


def load_state() -> Dict[str, Dict[str, Optional[str]]]:
    """Charge l'etat depuis state.json (dict vide si absent)."""
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            if isinstance(data, dict):
                return data
            return {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️ Erreur lecture state.json : {exc}")
        return {}


def save_state(state: Dict[str, Dict[str, Optional[str]]]) -> None:
    """Persiste l'etat dans state.json."""
    try:
        with STATE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"⚠️ Erreur ecriture state.json : {exc}")


# ---------------------------------------------------------------------------
# Scraping
# ---------------------------------------------------------------------------


async def _extract_with_selector(
    page: Page, selector: str
) -> Dict[str, Dict[str, Optional[str]]]:
    """Tente d'extraire les produits avec un selecteur donne."""
    js = """
    (params) => {
        const { selector, titleSelectors, linkSelectors, base } = params;
        const elements = Array.from(document.querySelectorAll(selector));
        const products = {};

        for (let i = 0; i < elements.length; i++) {
            const el = elements[i];
            const asin = el.getAttribute('data-asin');
            if (!asin || asin.trim() === '') continue;

            let title = null;
            for (const sel of titleSelectors) {
                const node = el.querySelector(sel);
                if (node && node.textContent) {
                    title = node.textContent.trim();
                    if (title) break;
                }
            }

            let link = null;
            for (const sel of linkSelectors) {
                const node = el.querySelector(sel);
                if (node) {
                    const href = node.getAttribute('href');
                    if (href) {
                        link = href.startsWith('http') ? href : base + href;
                        break;
                    }
                }
            }

            let image = null;
            const imgNode = el.querySelector('img');
            if (imgNode) {
                image = imgNode.getAttribute('src');
            }

            let availability = null;
            const availNode = el.querySelector('[data-name="availabilityMessage"]');
            if (availNode) {
                availability = availNode.textContent.trim();
            }

            let commandable = false;
            let deliveryDate = null;
            // Cherche d'abord dans l'element, puis dans son parent proche
            // car udm-primary-delivery-message peut etre un sibling du div[data-asin]
            const deliverySearchRoots = [el];
            let parent = el.parentElement;
            for (let d = 0; d < 3 && parent; d++, parent = parent.parentElement) {
                deliverySearchRoots.push(parent);
            }
            for (const root of deliverySearchRoots) {
                const deliveryNode = root.querySelector('.udm-primary-delivery-message');
                if (deliveryNode) {
                    const boldSpan = deliveryNode.querySelector('.a-text-bold');
                    if (boldSpan && boldSpan.textContent.trim()) {
                        commandable = true;
                        deliveryDate = boldSpan.textContent.trim();
                    }
                    break;
                }
            }

            products[asin] = { titre: title, lien: link, image: image, position: i, disponibilite: availability, commandable: commandable, dateLivraison: deliveryDate };
        }
        return products;
    }
    """
    result = await page.evaluate(
        js,
        {
            "selector": selector,
            "titleSelectors": TITLE_SELECTORS,
            "linkSelectors": LINK_SELECTORS,
            "base": AMAZON_BASE,
        },
    )
    if isinstance(result, dict):
        return result
    return {}


SEE_MORE_SELECTORS = [
    "a[href*='ref=_see_more']",
    "button[data-action*='see-more']",
    "a:has-text('Afficher plus')",
    "span:has-text('Afficher plus')",
]


async def _click_see_more(page: Page) -> int:
    """Clique sur tous les boutons 'Afficher plus' jusqu'a epuisement.
    Retourne le nombre de clics effectues."""
    clicks = 0
    while True:
        found = False
        for sel in SEE_MORE_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await page.wait_for_load_state("networkidle", timeout=15000)
                    clicks += 1
                    found = True
                    break
            except Exception:
                continue
        if not found:
            break
    return clicks


async def scrape_products(page: Page) -> Dict[str, Dict[str, Optional[str]]]:
    """Scrape la page cible et retourne les produits indexes par ASIN."""
    await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

    # Les blocs de livraison sont charges en asynchrone apres networkidle
    try:
        await page.wait_for_selector(".udm-primary-delivery-message", timeout=10000)
    except Exception:
        pass  # Certains produits n'ont pas de bloc livraison

    clicks = await _click_see_more(page)
    if clicks:
        log(f"  → {clicks} clic(s) 'Afficher plus'")
        try:
            await page.wait_for_selector(".udm-primary-delivery-message", timeout=5000)
        except Exception:
            pass

    for selector in PRODUCT_SELECTORS:
        products = await _extract_with_selector(page, selector)
        if products:
            return products
    return {}


# ---------------------------------------------------------------------------
# Notification Discord
# ---------------------------------------------------------------------------


def notify_discord(
    asin: str,
    product: Dict[str, Optional[str]],
    reason: str = "nouveau",
) -> None:
    """Envoie une notification Discord pour un produit."""
    title = product.get("titre") or f"Produit {asin}"
    title = title[:256]
    url = product.get("lien") or f"{AMAZON_BASE}/dp/{asin}"
    image = product.get("image")
    availability = product.get("disponibilite") or "Non renseignee"

    commandable = product.get("commandable", False)
    delivery_date = product.get("dateLivraison")
    orderable_line = (
        f"\n🟢 Commandable — livraison : **{delivery_date}**"
        if commandable and delivery_date
        else ("\n🟢 Commandable" if commandable else "\n🔴 Non commandable")
    )

    if reason == "tete_de_liste":
        description = f"⬆️ Passe en tete de liste !{orderable_line}\nDisponibilite : {availability}"
    elif reason == "disponibilite":
        description = f"📦 Disponibilite mise a jour : **{availability}**{orderable_line}"
    elif reason == "commandable":
        description = (
            f"🛒 Produit maintenant commandable !"
            + (f"\n📅 Livraison : **{delivery_date}**" if delivery_date else "")
        )
    else:
        description = f"Nouveau produit detecte sur la page promotionnelle Amazon !{orderable_line}"

    embed: Dict[str, Any] = {
        "title": title,
        "url": url,
        "color": EMBED_COLOR,
        "description": description,
        "footer": {
            "text": (
                "Amazon Monitor • "
                "amazon.fr/promotion/psp/A26013IELTKPDW"
            )
        },
    }
    if image:
        embed["image"] = {"url": image}

    cart_url = f"{AMAZON_BASE}/gp/aws/cart/add.html?ASIN.1={asin}&Quantity.1=2&AssociateTag=botbluray-21"
    embed["fields"] = [
        {"name": "Panier", "value": f"[🛒 Ajouter au panier (x2)]({cart_url})", "inline": True}
    ]

    payload = {"embeds": [embed]}

    try:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json=payload,
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
    state: Dict[str, Dict[str, Optional[str]]],
    cycle_index: int,
) -> Dict[str, Dict[str, Optional[str]]]:
    """Execute un cycle de scrape + comparaison + notifications."""
    try:
        products = await scrape_products(page)
    except Exception as exc:  # noqa: BLE001 - on veut tout attraper ici
        log(f"⚠️ Erreur : {exc}")
        return state

    log(f"Cycle #{cycle_index} — {len(products)} produits trouves")

    if not products:
        log(
            "⚠️ Aucun produit trouve (probable anti-bot) - "
            "etat conserve"
        )
        return state

    new_asins = [asin for asin in products if asin not in state]

    for asin in new_asins:
        product = products[asin]
        title = product.get("titre") or asin
        log(f"\U0001f195 Nouveau produit : {title} ({asin})")
        try:
            notify_discord(asin, product, reason="nouveau")
        except Exception as exc:  # noqa: BLE001
            log(f"⚠️ Erreur notification : {exc}")

    for asin, product in products.items():
        if asin not in state:
            continue
        prev = state[asin]

        prev_pos = prev.get("position")
        curr_pos = product.get("position")
        if curr_pos == 0 and prev_pos is not None and prev_pos != 0:
            title = product.get("titre") or asin
            log(f"⬆️ Produit passe en tete de liste : {title} ({asin})")
            try:
                notify_discord(asin, product, reason="tete_de_liste")
            except Exception as exc:  # noqa: BLE001
                log(f"⚠️ Erreur notification : {exc}")

        prev_avail = prev.get("disponibilite")
        curr_avail = product.get("disponibilite")
        if (
            curr_avail
            and curr_avail not in NORMAL_AVAILABILITY
            and curr_avail != prev_avail
        ):
            title = product.get("titre") or asin
            log(f"📦 Disponibilite changee : {curr_avail} — {title} ({asin})")
            try:
                notify_discord(asin, product, reason="disponibilite")
            except Exception as exc:  # noqa: BLE001
                log(f"⚠️ Erreur notification : {exc}")

        prev_commandable = prev.get("commandable", False)
        curr_commandable = product.get("commandable", False)
        if curr_commandable and not prev_commandable:
            title = product.get("titre") or asin
            date_info = product.get("dateLivraison") or ""
            log(f"🛒 Produit commandable : {title} ({asin}) — {date_info}")
            try:
                notify_discord(asin, product, reason="commandable")
            except Exception as exc:  # noqa: BLE001
                log(f"⚠️ Erreur notification : {exc}")

    save_state(products)
    return products


async def main() -> None:
    """Point d'entree : initialise Playwright et lance la boucle."""
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
                    log(
                        f"Premier run — {len(products)} produits trouves"
                    )
                    if products:
                        state = products
                        save_state(state)
                    else:
                        log(
                            "⚠️ Aucun produit detecte au premier "
                            "run (probable anti-bot)"
                        )
                except Exception as exc:  # noqa: BLE001
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
