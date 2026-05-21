"""Helper de logging horodate, partage par tous les modules."""

from __future__ import annotations

from datetime import datetime


def log(message: str) -> None:
    """Affiche un message prefixe par l'heure courante [HH:MM:SS].

    Gere les erreurs d'encodage console (emojis sous certains terminaux
    Windows) en repliant sur de l'ASCII.
    """
    timestamp = datetime.now().strftime("%H:%M:%S")
    try:
        print(f"[{timestamp}] {message}", flush=True)
    except UnicodeEncodeError:
        safe = message.encode("ascii", errors="replace").decode("ascii")
        print(f"[{timestamp}] {safe}", flush=True)
