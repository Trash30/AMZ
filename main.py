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
            delivery_info = product.get("deliveryText") or ""
            log(f"🛒 Commandable : {title} ({asin}) — {delivery_info}")
            try:
                await notify_discord(
                    asin, product, reason="commandable", prev=prev
                )
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

    # Grace period : ne supprimer les ASINs absents qu'apres N scans consecutifs
    new_state = dict(products)
    for asin in list(state.keys()):
        if asin in products:
            # present : reset missing_count
            new_state[asin]["missing_count"] = 0
        else:
            prev = state[asin]
            missing = prev.get("missing_count", 0) + 1
            if missing >= config.GRACE_PERIOD_SCANS:
                title = prev.get("titre") or asin
                log(f"🗑️ Supprime {asin} ({title}) — absent {config.GRACE_PERIOD_SCANS} scans")
            else:
                title = prev.get("titre") or asin
                log(f"⚠️ Absent ASIN {asin} ({title}) — essai {missing}/{config.GRACE_PERIOD_SCANS}")
                entry = dict(prev)
                entry["missing_count"] = missing
                new_state[asin] = entry

    save_state(new_state)
    return new_state


async def main() -> None:
    log("Demarrage du monitoring (modulaire, Ubuntu-ready)...")
    state = load_state()
    initial_run = len(state) == 0 and not config.STATE_FILE.exists()

    async with async_playwright() as playwright:
        browser, context = await create_browser_context(playwright)
        page: Page = await context.new_page()

        scan_count = 0
        startup_notified = False
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
                scan_count += 1

                # Recyclage navigateur periodique
                if scan_count > 1 and scan_count % config.BROWSER_RECYCLE_INTERVAL == 0:
                    log(f"[System] Recyclage navigateur (scan #{scan_count})...")
                    try:
                        await context.close()
                        await browser.close()
                        browser, context = await create_browser_context(playwright)
                        page = await context.new_page()
                    except Exception as exc:
                        log(f"⚠️ Erreur recyclage navigateur : {exc}")

                state = await run_cycle(page, state, cycle_index)

                # Notification demarrage apres le premier scrape reussi
                if not startup_notified and state:
                    startup_notified = True
                    try:
                        await notify_discord(
                            "",
                            {},
                            reason="demarrage",
                            all_products=list(state.values()),
                        )
                    except Exception as exc:
                        log(f"⚠️ Erreur notification demarrage : {exc}")
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Monitoring arrete.")
        sys.exit(0)
