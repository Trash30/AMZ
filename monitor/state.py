"""Persistance de l'etat des produits dans un fichier JSON."""

from __future__ import annotations

import json
from typing import Any, Dict

from monitor import config
from monitor.logging_utils import log


def load_state() -> Dict[str, Dict[str, Any]]:
    """Charge l'etat depuis config.STATE_FILE, ou {} si absent/illisible."""
    if not config.STATE_FILE.exists():
        return {}
    try:
        with config.STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        log(f"⚠️ Erreur lecture state.json : {exc}")
        return {}


def save_state(state: Dict[str, Dict[str, Any]]) -> None:
    """Ecrit l'etat dans config.STATE_FILE (UTF-8, indente)."""
    try:
        with config.STATE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log(f"⚠️ Erreur ecriture state.json : {exc}")
