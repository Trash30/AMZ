"""Notifications Discord via webhook (httpx async)."""

from __future__ import annotations

from typing import Any, Dict, Optional

import httpx

from monitor import config
from monitor.logging_utils import log


async def notify_discord(
    asin: str,
    product: Dict[str, Any],
    reason: str = "nouveau",
    prev: Optional[Dict[str, Any]] = None,
) -> None:
    """Envoie un embed Discord decrivant l'evenement detecte.

    Raisons supportees : "nouveau", "disponibilite", "commandable".
    """
    if not config.DISCORD_WEBHOOK_URL:
        log("⚠️ DISCORD_WEBHOOK_URL non defini — notification ignoree")
        return

    title = (product.get("titre") or f"Produit {asin}")[:256]
    url = product.get("lien") or f"{config.AMAZON_BASE}/dp/{asin}"
    image = product.get("image")
    prix = product.get("prix")
    commandable = product.get("commandable", False)
    delivery_date = product.get("dateLivraison")
    msg_livraison = product.get("messageLivraison")
    availability = product.get("disponibilite") or "Non renseignee"

    product_link = f"[🛒 Voir le produit]({config.AMAZON_BASE}/dp/{asin})"

    event_titles = {
        "nouveau": "🆕 Nouveau produit détecté !",
        "commandable": "🟢 Produit de retour en stock !",
        "disponibilite": "📦 Changement de disponibilité",
    }
    event_title = event_titles.get(reason, "ℹ️ Mise à jour produit")

    lines = [f"**{event_title}**", "", product_link, ""]

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
            if delivery_date
            else "—"
        )
        lines += [
            "**🚚 Infos de Livraison**",
            f"🔴 Avant : *{prev_msg}*",
            f"🟢 Maintenant : **{curr_msg}**",
            "",
        ]
    elif commandable and msg_livraison:
        lines += [f"🟢 {msg_livraison}", ""]
    elif commandable and delivery_date:
        lines += [f"🟢 Livraison : **{delivery_date}**", ""]
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
        "color": config.EMBED_COLOR,
        "description": description,
        "fields": fields,
        "footer": {
            "text": (
                "Bot Amazon Promotion Bluray • "
                "amazon.fr/promotion/psp/A26013IELTKPDW"
            )
        },
    }
    if image:
        embed["image"] = {"url": image}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                config.DISCORD_WEBHOOK_URL,
                json={"embeds": [embed]},
            )
        if response.status_code >= 400:
            log(
                f"⚠️ Erreur Discord HTTP {response.status_code} : "
                f"{response.text[:200]}"
            )
    except httpx.HTTPError as exc:
        log(f"⚠️ Erreur Discord : {exc}")
