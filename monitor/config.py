"""
Configuration centralisee du monitor Amazon.

Toutes les valeurs sont chargees depuis l'environnement (via un fichier .env
optionnel a la racine du projet) avec des valeurs par defaut raisonnables.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


# Racine du projet (dossier parent du package "monitor")
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Charge le fichier .env s'il existe (sans ecraser les vars deja presentes)
load_dotenv(PROJECT_ROOT / ".env")


def _get_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Variables principales
# ---------------------------------------------------------------------------

TARGET_URL: str = _get_str(
    "TARGET_URL", "https://www.amazon.fr/promotion/psp/A26013IELTKPDW"
)

# Requis : pas de valeur par defaut. Reste une chaine vide si non defini,
# le notifier loggue alors une erreur plutot que de crasher.
DISCORD_WEBHOOK_URL: str = os.getenv("DISCORD_WEBHOOK_URL", "")

ASSOCIATE_TAG: str = _get_str("ASSOCIATE_TAG", "botbluray-21")

INTERVAL_SECONDS: int = _get_int("INTERVAL_SECONDS", 5)

STATE_FILE: Path = Path(_get_str("STATE_FILE", str(PROJECT_ROOT / "state.json")))

SELLER_ID: str = _get_str("SELLER_ID", "A1X6FK5RDHNB96")


# ---------------------------------------------------------------------------
# Constantes navigateur / page
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1920, "height": 1080}
LOCALE = "fr-FR"
TIMEZONE = "Europe/Paris"
EXTRA_HEADERS = {"Accept-Language": "fr-FR,fr;q=0.9"}

AMAZON_BASE = "https://www.amazon.fr"
EMBED_COLOR = 0xFF9900

# Fichier de cookies de session optionnel (charge si present)
SESSION_FILE: Path = PROJECT_ROOT / "amazon_session.json"


# ---------------------------------------------------------------------------
# Disponibilites considerees comme "normales" (ne declenchent pas d'alerte)
# ---------------------------------------------------------------------------

NORMAL_AVAILABILITY = {
    "Habituellement expédié sous 1 à 2 mois",
    "Habituellement expédié sous 3 à 7 mois",
    "Habituellement expédié sous 6 à 7 mois",
    "Habituellement expédié sous 1 à 3 mois",
}
