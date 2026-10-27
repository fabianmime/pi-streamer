"""Persistente Konfiguration für den LiveVoice-Pi-Streamer.

Die Konfiguration lebt als einfaches JSON-File in instance/config.json
(default) und wird bei jedem Speichern komplett neu geschrieben. Es gibt
bewusst keine Datenbank - für ein Einzelgerät mit einem Benutzer reicht
das.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "instance" / "config.json"

VALID_BITRATES_KBPS = (96, 128, 160, 192, 256)
VALID_SAMPLE_RATES = (44100, 48000)
VALID_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
# Aufnahmeformate (siehe app/recorder.py::RECORDING_FORMATS): WAV als
# bisheriger Standard, FLAC (verlustfrei, ~halb so gross, bleibt bei
# Stromausfall bis zur Abbruchstelle lesbar), MKA (unkomprimiertes PCM in
# Matroska - das Audio-Gegenstück zu OBS' MKV).
VALID_RECORDING_FORMATS = ("wav", "flac", "mka")
DEFAULT_WEB_PORT = 80
DEFAULT_RECORDING_FILENAME_PREFIX = "Aufnahme"
DEFAULT_UPDATE_REPO_URL = "https://github.com/fabianmime/pi-streamer.git"
DEFAULT_UPDATE_BRANCH = "main"

logger = logging.getLogger(__name__)

# Git-Branchname: bewusst eng gefasst (kein Leerzeichen, kein "..", kein
# führendes "-"), landet als Argument in einem git-Aufruf (app/updater.py).
_SAFE_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
# Nur https:// oder GitHub-SSH-Form - keine file://, ext:: o.ä.
# Git-Transporte, die lokale Befehle ausführen könnten.
_SAFE_REPO_URL_RE = re.compile(r"^(https://[^\s]+|git@[A-Za-z0-9.-]+:[^\s]+)$")

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
    # ALSA-Kurzkennung (z.B. "Scarlett") des gewählten Geräts - stabil, anders
    # als die Kartennummer in `audio_device` ("plughw:1,0"), die sich ändern
    # kann, wenn weitere USB-Geräte stecken (siehe AppState.sync_dsnoop_config).
    audio_card_id: str = ""
    channels: int = 2
    sample_rate: int = 48000
    bitrate_kbps: int = 160
    # Cockpit-Passwort (Stream/Aufnahme bedienen). Ohne admin_password
    # schützt es wie bisher auch die Einstellungen.
    web_password: str = ""
    # Admin-Passwort: nur damit kommt man in die Einstellungen (und darf
    # Power/Update/WLAN/USB-Verwaltung), siehe routes.require_login.
    admin_password: str = ""
    web_port: int = DEFAULT_WEB_PORT
    hide_operator: bool = False
    hide_recording: bool = False
    log_level: str = "INFO"
    recording_target: str = ""
    recording_filename_prefix: str = DEFAULT_RECORDING_FILENAME_PREFIX
    recording_format: str = "wav"
    # Stream bei Abbruch (Netz weg, Import bei LiveVoice neu gestartet)
    # selbständig neu verbinden - und nach einem Stromausfall/Neustart des
    # Geräts wieder aufnehmen, wenn er vorher lief (siehe app/streamer.py).
    stream_auto_reconnect: bool = True
    update_repo_url: str = DEFAULT_UPDATE_REPO_URL
    update_branch: str = DEFAULT_UPDATE_BRANCH
    update_auto: bool = False

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
        if self.recording_format not in VALID_RECORDING_FORMATS:
            raise ValueError(f"recording_format muss einer von {VALID_RECORDING_FORMATS} sein")
        if self.update_repo_url and not _SAFE_REPO_URL_RE.match(self.update_repo_url):
            raise ValueError("Update-Quelle muss mit https:// beginnen (oder git@github.com:... für SSH)")
        if not _SAFE_BRANCH_RE.match(self.update_branch) or ".." in self.update_branch:
            raise ValueError("Update-Branch ist ungültig")
        return self

    def is_ready_to_stream(self) -> bool:
        """SRT-URL und Audio-Device sind Pflicht, um ffmpeg überhaupt starten zu können."""
        return bool(self.srt_url.strip()) and bool(self.audio_device.strip())

    def is_ready_to_record(self) -> bool:
        """Aufnahme-Ziel (Mountpoint des USB-Sticks) muss gesetzt sein."""
        return bool(self.recording_target.strip())

    def to_public_dict(self) -> dict:
        """Wie asdict(), aber ohne die Passwörter - für JSON-Antworten an den Client."""
        data = asdict(self)
        data.pop("web_password", None)
        data.pop("admin_password", None)
        data["web_password_set"] = bool(self.web_password)
        data["admin_password_set"] = bool(self.admin_password)
        return data


def _read_config_file(path: Path) -> StreamerConfig:
    with path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    known_fields = {f.name for f in StreamerConfig.__dataclass_fields__.values()}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return StreamerConfig(**filtered)


def _backup_path(path: Path) -> Path:
    return path.with_suffix(".json.bak")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> StreamerConfig:
    """Lädt die Konfiguration. Ist die Datei nach einem Stromausfall leer
    oder kaputt, wird die Sicherung vom vorletzten Speichern genommen statt
    in einer Absturzschleife zu enden (der Dienst startet sonst nie mehr)."""
    if not path.exists():
        cfg = StreamerConfig()
        save_config(cfg, path)
        return cfg

    try:
        return _read_config_file(path)
    except (OSError, ValueError, TypeError) as exc:
        backup = _backup_path(path)
        logger.error("Konfiguration %s nicht lesbar (%s) - versuche Sicherung %s", path, exc, backup)
        if backup.exists():
            try:
                return _read_config_file(backup)
            except (OSError, ValueError, TypeError) as backup_exc:
                logger.error("Auch Sicherung nicht lesbar (%s) - starte mit Standardwerten", backup_exc)
        return StreamerConfig()


def _write_durable(path: Path, content: str) -> None:
    """Schreibt atomar UND dauerhaft: tmp-Datei + fsync + rename + fsync des
    Verzeichnisses - sonst kann ein Stromausfall kurz nach dem Speichern
    eine leere Datei hinterlassen (ext4 schreibt verzögert)."""
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    tmp_path.replace(path)
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return  # z.B. Windows ohne Verzeichnis-fsync - Datei selbst ist schon gesynct
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def save_config(cfg: StreamerConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            _read_config_file(path)  # nur eine lesbare Datei als Sicherung übernehmen
            _write_durable(_backup_path(path), path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
    _write_durable(path, json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n")
