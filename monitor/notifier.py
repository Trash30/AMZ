"""Notifications Discord via webhook (httpx async)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from monitor import config
from monitor.logging_utils import log


async def notify_discord(
    asin: str,
    product: Dict[str, Any],
    reason: str = "nouveau",
    prev: Optional[Dict[str, Any]] = None,
    all_products: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Envoie un embed Discord decrivant l'evenement detecte.

    Raisons supportees : "nouveau", "disponibilite", "commandable", "demarrage".
    """
    if not config.DISCORD_WEBHOOK_URL:
        log("⚠️ DISCORD_WEBHOOK_URL non defini — notification ignoree")
        return

    if reason == "demarrage":
        await _notify_demarrage(all_products or [])
        return

    title = (product.get("titre") or f"Produit {asin}")[:256]
    url = product.get("lien") or f"{config.AMAZON_BASE}/dp/{asin}"
    image = product.get("image")
    prix = product.get("prix")
    commandable = product.get("commandable", False)
    delivery_text = product.get("deliveryText")
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
        prev_delivery = prev.get("deliveryText") or "Livraison non disponible"
        curr_delivery = delivery_text or "—"
        lines += [
            "**🚚 Infos de Livraison**",
            f"🔴 Avant : *{prev_delivery}*",
            f"🟢 Maintenant : **{curr_delivery}**",
            "",
        ]
    elif commandable and delivery_text:
        lines += [f"🟢 Livraison : **{delivery_text}**", ""]
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

    await _post_embed(embed)


async def _notify_demarrage(products: List[Dict[str, Any]]) -> None:
    in_stock = [p for p in products if p.get("commandable")]
    out_of_stock = [p for p in products if not p.get("commandable")]

    lines = [
        f"Le bot est maintenant actif et surveille la promotion.",
        f"",
        f"🔗 **[Lien de la Promotion]({config.TARGET_URL})**",
        f"",
        f"### 🟢 En Stock ({len(in_stock)})",
    ]
    if not in_stock:
        lines.append("*Aucun produit en stock actuellement.*")
    else:
        for p in in_stock:
            titre = p.get("titre") or p.get("asin", "?")
            lien = p.get("lien") or "#"
            prix = p.get("prix") or ""
            prix_str = f" — `{prix}`" if prix else ""
            lines.append(f"- **[{titre}]({lien})**{prix_str}")

    lines += ["", f"### 🔴 Hors Stock ({len(out_of_stock)})"]
    if not out_of_stock:
        lines.append("*Aucun produit hors stock.*")
    else:
        for p in out_of_stock:
            titre = p.get("titre") or p.get("asin", "?")
            lien = p.get("lien") or "#"
            prix = p.get("prix") or ""
            prix_str = f" — `{prix}`" if prix else ""
            lines.append(f"- **[{titre}]({lien})**{prix_str}")

    description = "\n".join(lines)
    if len(description) > 4000:
        description = description[:3970] + "\n\n*(Tronqué pour respecter la limite Discord)*"

    embed: Dict[str, Any] = {
        "title": "🚀 Bot démarré — Récapitulatif",
        "description": description,
        "color": 10181046,
        "fields": [
            {"name": "📊 Total", "value": str(len(products)), "inline": True},
            {"name": "🟢 En stock", "value": str(len(in_stock)), "inline": True},
            {"name": "🔴 Hors stock", "value": str(len(out_of_stock)), "inline": True},
        ],
        "footer": {
            "text": (
                "Bot Amazon Promotion Bluray • "
                "amazon.fr/promotion/psp/A26013IELTKPDW"
            )
        },
    }
    await _post_embed(embed)


async def _post_embed(embed: Dict[str, Any]) -> None:
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
