"""Persistente Konfiguration für den LiveVoice-Pi-Streamer.

Die Konfiguration lebt als einfaches JSON-File in instance/config.json
(default) und wird bei jedem Speichern komplett neu geschrieben. Es gibt
bewusst keine Datenbank - für ein Einzelgerät mit einem Benutzer reicht
das.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "instance" / "config.json"

VALID_BITRATES_KBPS = (96, 128, 160, 192, 256)
VALID_SAMPLE_RATES = (44100, 48000)
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
DEFAULT_WEB_PORT = 80
DEFAULT_RECORDING_FILENAME_PREFIX = "Aufnahme"

# Kein "/", "\" oder Steuerzeichen - das landet direkt als Dateiname-Präfix
# auf dem USB-Stick (siehe recorder.recording_filename), muss also auf
# FAT32/exFAT gültig bleiben. Länge grosszügig, aber begrenzt.
_SAFE_FILENAME_PREFIX_RE = re.compile(r"^[^/\\\x00-\x1f]{1,40}$")

# ALSA-Alias aus /etc/asound.conf (siehe DEPLOY.md, Schritt "Geteilter
# Audio-Zugriff für Recording"): ein `dsnoop`-PCM hinter einem `plug`-Wrapper,
# das denselben USB-Audio-Eingang für mehrere gleichzeitige ffmpeg-Prozesse
# freigibt (Stream + Recording). Ein rohes `plughw:`-Gerät (wie es
# `audio_device` sonst für die Pegel-Messung nutzt) erlaubt das nicht - siehe
# app/levelmeter.py-Docstring ("Device or resource busy" bei zwei Prozessen).
SHARED_CAPTURE_DEVICE = "livevoice_capture"


@dataclass
class StreamerConfig:
    """Alle Einstellungen, die über die Settings-Seite editierbar sind."""

    srt_url: str = ""
    operator_url: str = ""
    audio_device: str = ""
    channels: int = 2
    sample_rate: int = 48000
    bitrate_kbps: int = 160
    web_password: str = ""
    web_port: int = DEFAULT_WEB_PORT
    hide_operator: bool = False
    log_level: str = "INFO"
    recording_target: str = ""
    recording_filename_prefix: str = DEFAULT_RECORDING_FILENAME_PREFIX

    def validated(self) -> "StreamerConfig":
        """Wirft ValueError bei offensichtlich unsinnigen Werten."""
        if self.channels not in (1, 2):
            raise ValueError("channels muss 1 oder 2 sein")
        if self.sample_rate not in VALID_SAMPLE_RATES:
            raise ValueError(f"sample_rate muss einer von {VALID_SAMPLE_RATES} sein")
        if self.bitrate_kbps not in VALID_BITRATES_KBPS:
            raise ValueError(f"bitrate_kbps muss einer von {VALID_BITRATES_KBPS} sein")
        if self.web_port != 80 and not (1024 <= self.web_port <= 65535):
            raise ValueError("web_port muss 80 oder zwischen 1024 und 65535 liegen")
        if self.log_level not in VALID_LOG_LEVELS:
            raise ValueError(f"log_level muss einer von {VALID_LOG_LEVELS} sein")
        if not _SAFE_FILENAME_PREFIX_RE.match(self.recording_filename_prefix):
            raise ValueError(
                "recording_filename_prefix darf nicht leer sein, keine '/' oder '\\' enthalten "
                "und muss unter 40 Zeichen lang sein"
            )
        return self

    def is_ready_to_stream(self) -> bool:
        """SRT-URL und Audio-Device sind Pflicht, um ffmpeg überhaupt starten zu können."""
        return bool(self.srt_url.strip()) and bool(self.audio_device.strip())

    def is_ready_to_record(self) -> bool:
        """Aufnahme-Ziel (Mountpoint des USB-Sticks) muss gesetzt sein."""
        return bool(self.recording_target.strip())

    def to_public_dict(self) -> dict:
        """Wie asdict(), aber ohne das Passwort - für JSON-Antworten an den Client."""
        data = asdict(self)
        data.pop("web_password", None)
        data["web_password_set"] = bool(self.web_password)
        return data


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> StreamerConfig:
    if not path.exists():
        cfg = StreamerConfig()
        save_config(cfg, path)
        return cfg

    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    known_fields = {f.name for f in StreamerConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return StreamerConfig(**filtered)


def save_config(cfg: StreamerConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp_path.replace(path)
