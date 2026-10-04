"""Zugriff auf die systemd-Journal-Logs des Diensts selbst - für den
Log-Download in den Einstellungen.

Braucht Leserechte auf das System-Journal (Mitgliedschaft in der Gruppe
`systemd-journal` für den Service-User `livevoice` - siehe DEPLOY.md).
Ohne diese Rechte liefert `journalctl` einen Permission-Fehler, den wir hier
abfangen und in eine verständliche Meldung übersetzen, statt die Seite
abstürzen zu lassen.
"""

from __future__ import annotations

import subprocess

UNIT_NAME = "livevoice-streamer"
DEFAULT_LINES = 2000


def get_recent_log_text(lines: int = DEFAULT_LINES, unit: str = UNIT_NAME) -> str:
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "--no-pager", "-n", str(lines)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Log konnte nicht gelesen werden: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return (
            "Log konnte nicht gelesen werden (journalctl-Fehler). "
            "Läuft der Service-User 'livevoice' in der Gruppe 'systemd-journal'? "
            f"Details: {detail}"
        )

    return result.stdout or "Keine Log-Einträge gefunden."
