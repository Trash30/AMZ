"""
Amazon Promotion Page Monitor v2.1

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

PRODUCT_SELECTOR = "li.productGrid[data-asin]"
DELIVERY_SELECTOR = ".udm-primary-delivery-message"
SEE_MORE_SELECTOR = "a[href*='ref=_see_more']"

# Passer a True pour loguer toutes les requetes XHR/fetch du premier cycle
# et identifier les endpoints Amazon a intercepter
DEBUG_NETWORK = True

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


async def _scroll_to_bottom(page: Page) -> None:
    """Scroll progressif pour declencher le lazy-loading Amazon."""
    prev_height = -1
    while True:
        height = await page.evaluate("document.body.scrollHeight")
        if height == prev_height:
            break
        prev_height = height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(0.8)
    # Remonte en haut pour que les boutons soient visibles
    await page.evaluate("window.scrollTo(0, 0)")


async def _expand_all(page: Page) -> None:
    """Clique tous les boutons 'Afficher plus' en scrollant au besoin."""
    clicks = 0
    while True:
        try:
            btn = page.locator(SEE_MORE_SELECTOR).first
            await btn.wait_for(state="visible", timeout=2000)
            await btn.scroll_into_view_if_needed()
            await btn.click()
            await page.wait_for_load_state("networkidle", timeout=15000)
            clicks += 1
        except Exception:
            break
    if clicks:
        log(f"  → {clicks} clic(s) 'Afficher plus'")


async def _wait_for_delivery_blocks(page: Page) -> None:
    """
    Poll le count de blocs livraison jusqu'a stabilisation (2 s sans variation).
    Amazon les injecte en asynchrone apres le rendu initial.
    """
    prev_count = -1
    stable_ticks = 0
    for _ in range(60):  # timeout 30 s
        count = await page.locator(DELIVERY_SELECTOR).count()
        if count == prev_count:
            stable_ticks += 1
            if stable_ticks >= 4:
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

        // Prix via aria-label (ex: "12,21 €")
        const priceEl = el.querySelector("[name='productPriceToPay']");
        const prix = priceEl?.getAttribute("aria-label")?.trim() || null;

        // Message de livraison complet + date seule
        const deliveryBlock = el.querySelector(".udm-primary-delivery-message");
        const deliveryBold = deliveryBlock?.querySelector(".a-text-bold");
        const deliveryDate = deliveryBold?.textContent?.trim() || null;
        const messageLivraison = deliveryBlock?.textContent
            ?.replace(/\\s+/g, " ").trim() || null;

        products[asin] = {
            titre: title,
            lien: link,
            image,
            position: i,
            disponibilite: availability,
            prix,
            commandable: !!deliveryDate,
            dateLivraison: deliveryDate,
            messageLivraison,
        };
    });

    return products;
}
"""


async def scrape_products(
    page: Page,
    debug_network: bool = False,
) -> Dict[str, Dict[str, Any]]:

    captured: list[dict] = []

    if debug_network:
        async def _on_response(response) -> None:
            url = response.url
            # On ignore les assets statiques
            if any(ext in url for ext in (".png", ".jpg", ".gif", ".css", ".woff", ".ico")):
                return
            ct = (response.headers.get("content-type") or "").lower()
            if "json" in ct or "javascript" in ct or "html" in ct:
                try:
                    body = await response.body()
                    size = len(body)
                    preview = body[:120].decode("utf-8", errors="replace").replace("\n", " ")
                    captured.append({
                        "status": response.status,
                        "url": url,
                        "ct": ct.split(";")[0],
                        "size": size,
                        "preview": preview,
                    })
                except Exception:
                    pass

        page.on("response", _on_response)

    await page.goto(TARGET_URL, wait_until="networkidle", timeout=60000)

    try:
        await page.wait_for_selector(PRODUCT_SELECTOR, timeout=30000)
    except Exception:
        log("⚠️ Aucun produit detecte dans le DOM (probable anti-bot)")
        return {}

    nb = await page.locator(PRODUCT_SELECTOR).count()
    log(f"  → {nb} produit(s) au chargement initial")

    # Scroll pour declencher le lazy-loading avant d'expand
    await _scroll_to_bottom(page)

    nb_after_scroll = await page.locator(PRODUCT_SELECTOR).count()
    if nb_after_scroll != nb:
        log(f"  → {nb_after_scroll} produit(s) apres scroll ({nb_after_scroll - nb:+d})")

    await _expand_all(page)

    nb_final = await page.locator(PRODUCT_SELECTOR).count()
    if nb_final != nb_after_scroll:
        log(f"  → {nb_final} produit(s) apres expand ({nb_final - nb_after_scroll:+d})")

    await _wait_for_delivery_blocks(page)

    result = await page.evaluate(_EXTRACT_JS)
    if not isinstance(result, dict):
        return {}

    with_date = sum(1 for p in result.values() if p.get("commandable"))
    log(f"  → {len(result)} produits extraits — {with_date} commandables")

    if debug_network and captured:
        log("=" * 60)
        log(f"DEBUG RESEAU — {len(captured)} requetes capturees :")
        for i, r in enumerate(captured, 1):
            log(f"  [{i:02d}] {r['status']} {r['ct']} {r['size']}o")
            log(f"        URL     : {r['url']}")
            log(f"        Preview : {r['preview']}")
        log("=" * 60)
        if page.listeners("response"):
            page.remove_listener("response", page.listeners("response")[0])

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

    cart_link = f"[🛒 Ajouter 2 ex. au panier (ATC)]({_cart_url(asin)})"

    # Titre de la notification selon l'evenement
    event_titles = {
        "nouveau":       "🆕 Nouveau produit détecté !",
        "commandable":   "🟢 Produit de retour en stock !",
        "tete_de_liste": "⬆️ Passé en tête de liste !",
        "disponibilite": "📦 Changement de disponibilité",
    }
    event_title = event_titles.get(reason, "ℹ️ Mise à jour produit")

    lines = [f"**{event_title}**", "", cart_link, ""]

    # Avant / Maintenant pour disponibilite
    if reason == "disponibilite" and prev:
        prev_avail = prev.get("disponibilite") or "—"
        lines += [
            "**🔄 Changement de Statut**",
            f"🔴 Avant : *{prev_avail}*",
            f"🟢 Maintenant : **{availability}**",
            "",
        ]
    elif reason not in ("commandable", "nouveau", "tete_de_liste"):
        lines += [f"Disponibilite : {availability}", ""]

    # Avant / Maintenant pour livraison
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

    # Champs inline : Prix + ASIN
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
        log("⚠️ Aucun produit (probable anti-bot) — etat conserve")
        return state

    for asin in [a for a in products if a not in state]:
        title = products[asin].get("titre") or asin
        log(f"🆕 Nouveau : {title} ({asin})")
        try:
            notify_discord(asin, products[asin], reason="nouveau")
        except Exception as exc:
            log(f"⚠️ Erreur notification : {exc}")

    for asin, product in products.items():
        if asin not in state:
            continue
        prev = state[asin]

        # Tete de liste
        if product.get("position") == 0 and prev.get("position") not in (None, 0):
            title = product.get("titre") or asin
            log(f"⬆️ Tete de liste : {title} ({asin})")
            try:
                notify_discord(asin, product, reason="tete_de_liste", prev=prev)
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
                notify_discord(asin, product, reason="disponibilite", prev=prev)
            except Exception as exc:
                log(f"⚠️ Erreur notification : {exc}")

        # Devient commandable
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
                    products = await scrape_products(page, debug_network=DEBUG_NETWORK)
                    log(f"Premier run — {len(products)} produits")
                    if products:
                        state = products
                        save_state(state)
                    else:
                        log("⚠️ Aucun produit au premier run (probable anti-bot)")
                except Exception as exc:
                    log(f"⚠️ Erreur : {exc}")
            else:
                # Pas de initial_run mais debug demande : on scrape une fois pour capturer
                if DEBUG_NETWORK:
                    try:
                        log("Mode debug reseau actif — capture du premier cycle...")
                        await scrape_products(page, debug_network=True)
                    except Exception as exc:
                        log(f"⚠️ Erreur debug : {exc}")

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
