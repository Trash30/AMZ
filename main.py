"""
Amazon Promotion Page Monitor — entry point.

Surveille la page promo Amazon configuree et notifie via Discord :
  - nouveau produit (ASIN absent de l'etat)
  - changement de disponibilite (hors disponibilites normales)
  - produit devenu commandable (delai de livraison apparu)

Le trigger "tete de liste" (position == 0) a ete supprime.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any, Dict

from playwright.async_api import Page, async_playwright

from monitor import config
from monitor.logging_utils import log
from monitor.notifier import notify_discord
from monitor.scraper import create_browser_context, scrape_products
from monitor.state import load_state, save_state


async def run_cycle(
    page: Page,
    state: Dict[str, Dict[str, Any]],
    cycle_index: int,
) -> Dict[str, Dict[str, Any]]:
    """Execute un cycle de scraping + detection + notifications.

    Retourne le nouvel etat (les produits scrapes) qui devient l'etat de
    reference pour le cycle suivant.
    """
    try:
        products = await scrape_products(page)
    except Exception as exc:
        log(f"⚠️ Erreur scrape : {exc}")
        return state

    log(f"Cycle #{cycle_index} — {len(products)} produits")

    if not products:
        log("⚠️ Aucun produit — etat conserve")
        return state

    # Nouveaux produits (ASIN absent de l'etat)
    for asin in [a for a in products if a not in state]:
        product = products[asin]
        title = product.get("titre") or asin
        log(f"🆕 Nouveau : {title} ({asin})")
        try:
            await notify_discord(asin, product, reason="nouveau")
        except Exception as exc:
            log(f"⚠️ Erreur notification : {exc}")

    # Produits deja connus
    for asin, product in products.items():
        if asin not in state:
            continue
        prev = state[asin]

        # PAS de check position / tete de liste (supprime)

        # Changement de disponibilite (hors disponibilites normales)
        prev_avail = prev.get("disponibilite")
        curr_avail = product.get("disponibilite")
        if (
            curr_avail
            and curr_avail not in config.NORMAL_AVAILABILITY
            and curr_avail != prev_avail
        ):
            title = product.get("titre") or asin
            log(f"📦 Disponibilite : {curr_avail} — {title} ({asin})")
            try:
                await notify_discord(
                    asin, product, reason="disponibilite", prev=prev
                )
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

        # Produit devenu commandable (False -> True)
        if product.get("commandable") and not prev.get("commandable"):
            title = product.get("titre") or asin
            date_info = product.get("dateLivraison") or ""
            log(f"🛒 Commandable : {title} ({asin}) — {date_info}")
            try:
                await notify_discord(
                    asin, product, reason="commandable", prev=prev
                )
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

    save_state(products)
    return products


async def main() -> None:
    log("Demarrage du monitoring (modulaire, Ubuntu-ready)...")
    state = load_state()
    initial_run = len(state) == 0 and not config.STATE_FILE.exists()

    async with async_playwright() as playwright:
        browser, context = await create_browser_context(playwright)
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
                await asyncio.sleep(config.INTERVAL_SECONDS)
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
