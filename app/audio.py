"""Erkennung von ALSA-Aufnahmegeräten (USB-Audio-Interfaces) via `arecord -l`
sowie Erzeugung des geteilten `dsnoop`-Capture-Alias (SHARED_CAPTURE_DEVICE,
siehe app/config.py) für das jeweils gewählte Gerät.

Bewusst kein python-sounddevice o.ä. als Abhängigkeit - `arecord` ist Teil
von alsa-utils und auf jedem Raspberry Pi OS ohnehin für die
Fehlersuche nötig.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_CARD_LINE_RE = re.compile(
    r"^card\s+(?P<card>\d+):\s+(?P<card_id>\S+)\s+\[(?P<card_name>[^\]]+)\],\s+"
    r"device\s+(?P<device>\d+):\s+(?P<device_name>.+?)\s+\[[^\]]+\]\s*$"
)


@dataclass
class AudioDevice:
    hw_id: str  # z.B. "plughw:1,0" - direkt als ffmpeg -i Wert nutzbar
    card_id: str  # ALSA-Kurzkennung, z.B. "WH62" - fürs "hw:CARD=<id>,DEV=..." des dsnoop-Alias
    card_name: str
    device_name: str

    @property
    def label(self) -> str:
        return f"{self.card_name} - {self.device_name} ({self.hw_id})"


def parse_arecord_output(output: str) -> list[AudioDevice]:
    """Nutzt bewusst `plughw:` statt `hw:` als Geräte-Kennung: ein rohes
    `hw:`-Gerät verweigert nicht unterstützte Sample-Raten nicht etwa mit
    einem Fehler, sondern klemmt sie still auf die nächstliegende
    Hardware-Rate herunter, ohne dass ffmpeg das bemerkt oder die im
    AAC-Stream deklarierte Rate korrigiert - das Ergebnis ist ein technisch
    fehlerfrei laufender Stream mit unhörbarem/verzerrtem Audio (beobachtet
    mit einem Headset, das hardwareseitig nur 8-32kHz kann, während die
    Konfiguration 48kHz verlangte). `plughw:` legt eine ALSA-Software-
    Konvertierungsschicht drüber, die Rate/Format/Kanalzahl transparent an
    das anpasst, was tatsächlich angefragt wird - unabhängig davon, was die
    jeweils angeschlossene Hardware nativ kann."""
    devices: list[AudioDevice] = []
    for line in output.splitlines():
        match = _CARD_LINE_RE.match(line.strip())
        if not match:
            continue
        devices.append(
            AudioDevice(
                hw_id=f"plughw:{match['card']},{match['device']}",
                card_id=match["card_id"],
                card_name=match["card_name"],
                device_name=match["device_name"],
            )
        )
    return devices


def list_capture_devices(timeout: float = 5.0) -> list[AudioDevice]:
    """Liest die verfügbaren ALSA-Aufnahmegeräte aus.

    Gibt eine leere Liste zurück, wenn `arecord` fehlt oder fehlschlägt,
    statt eine Exception zu werfen - die Settings-Seite soll auch ohne
    angeschlossenes Interface benutzbar bleiben.
    """
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    return parse_arecord_output(result.stdout)


def build_dsnoop_config_text(card_id: str, channels: int, sample_rate: int) -> str:
    """Baut die ALSA-Konfiguration für den geteilten Capture-Alias
    (SHARED_CAPTURE_DEVICE, siehe app/config.py) für genau eine Karte -
    `card_id` ist die ALSA-Kurzkennung aus `arecord -l` (z.B. "WH62"), nicht
    der beschreibende Name in Klammern (siehe AudioDevice)."""
    return (
        "# Automatisch generiert von app/audio.py::write_dsnoop_config -\n"
        "# manuelle Änderungen gehen beim nächsten Speichern der Einstellungen\n"
        "# bzw. beim nächsten Dienst-Start verloren.\n"
        "pcm.livevoice_dsnoop {\n"
        "    type dsnoop\n"
        "    ipc_key 219347\n"
        "    slave {\n"
        f'        pcm "hw:CARD={card_id},DEV=0"\n'
        f"        channels {channels}\n"
        f"        rate {sample_rate}\n"
        "        format S16_LE\n"
        "    }\n"
        "}\n"
        "pcm.livevoice_capture {\n"
        "    type plug\n"
        '    slave.pcm "livevoice_dsnoop"\n'
        "}\n"
    )


def write_dsnoop_config(card_id: str, channels: int, sample_rate: int, path: Path | None = None) -> None:
    """Schreibt die private ALSA-Konfiguration des Dienstbenutzers
    (`~/.asoundrc`) neu, damit SHARED_CAPTURE_DEVICE immer auf die Karte des
    aktuell in den Einstellungen gewählten Audio-Geräts zeigt.

    `~/.asoundrc` wird von ALSA NACH dem system-weiten `/etc/asound.conf`
    gelesen und überschreibt darin definierte PCMs mit demselben Namen
    (siehe DEPLOY.md) - kein root/sudo/Polkit nötig, der Dienstbenutzer
    besitzt sein eigenes Home-Verzeichnis (`/opt/livevoice-streamer`)
    bereits. Macht den Wechsel auf ein anderes USB-Audio-Interface
    plug-and-play: Gerät anschliessen, in den Einstellungen unter Audio
    "Neu suchen" tippen, das neue Gerät wählen, speichern - fertig, kein
    manuelles Bearbeiten von Konfigurationsdateien per SSH mehr nötig."""
    target = path or (Path.home() / ".asoundrc")
    content = build_dsnoop_config_text(card_id, channels, sample_rate)
    tmp_path = target.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(target)
