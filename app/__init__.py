from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask

from . import audio, storage
from .config import DEFAULT_CONFIG_PATH, SHARED_CAPTURE_DEVICE, StreamerConfig, load_config, save_config
from .levelmeter import LevelMeter, LevelReading
from .recorder import RecordingManager
from .streamer import StreamManager


def configure_logging(level_name: str) -> None:
    """Setzt das globale Log-Level. Schreibt nach stderr, was systemd in den
    Journal-Log übernimmt - genau das, was der "Log herunterladen"-Knopf
    liest. `force=True` erlaubt Neukonfiguration zur Laufzeit (z.B. nach
    einer Änderung in den Einstellungen), ohne dass sich Handler duplizieren."""
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _load_or_create_secret_key(instance_dir: Path) -> str:
    """Signierschlüssel für die Session-Cookies der Login-Seite.

    Wird einmalig erzeugt und in instance/secret_key abgelegt - bei jedem
    Neustart neu zu generieren würde alle eingeloggten Sessions sofort
    ungültig machen.
    """
    path = instance_dir / "secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    instance_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    return key


class AppState:
    """Hält den einen langlebigen Zustand des Geräts: die aktuell geladene
    Konfiguration und den einen StreamManager. Absichtlich kein
    Datenbank-/ORM-Unterbau - ein Gerät, ein Benutzer, ein Stream."""

    def __init__(self, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.config_path = config_path
        self.config: StreamerConfig = load_config(config_path)
        self.stream_manager = StreamManager()
        self.recording_manager = RecordingManager()
        self.level_meter = LevelMeter()
        self.sync_dsnoop_config()

    def save(self) -> None:
        save_config(self.config, self.config_path)

    def refresh_audio_devices(self):
        # Ueber das Modul aufgerufen (nicht als frueh gebundener Funktions-
        # Import), damit Tests mit monkeypatch.setattr(audio, "...") greifen -
        # genau wie storage.mount_drive() in routes.py.
        return audio.list_capture_devices()

    def refresh_usb_drives(self):
        return storage.list_usb_drives()

    def sync_dsnoop_config(self) -> None:
        """Regeneriert `~/.asoundrc` (siehe app/audio.py::write_dsnoop_config)
        für das aktuell konfigurierte Audio-Gerät - beim App-Start (z.B. nach
        einem Reboot mit gewechseltem Interface, sofern es noch an
        derselben `plughw:`-Position hängt) sowie nach jedem Speichern der
        Einstellungen. Kein Effekt, wenn kein Gerät konfiguriert ist oder es
        aktuell nicht per `arecord -l` erkannt wird (z.B. `arecord` fehlt in
        Tests) - dann bleibt eine vorhandene Konfiguration einfach stehen."""
        device_hw_id = self.config.audio_device
        if not device_hw_id:
            return
        for dev in self.refresh_audio_devices():
            if dev.hw_id == device_hw_id:
                audio.write_dsnoop_config(dev.card_id, self.config.channels, self.config.sample_rate)
                return

    def ensure_level_meter(self) -> None:
        """Startet die eigenständige Pegel-Messung, falls ein Audio-Gerät
        konfiguriert ist und sie gerade nicht läuft - z.B. weil noch nie
        gestartet oder der Mess-Prozess von selbst beendet ist.

        Liest über SHARED_CAPTURE_DEVICE (den `dsnoop`-Alias aus
        /etc/asound.conf, siehe app/config.py), nicht über das rohe
        `plughw:`-Gerät aus den Einstellungen - genau wie Stream und
        Recording. `dsnoop` erlaubt mehreren Lesern gleichzeitigen Zugriff,
        die Pegelanzeige läuft daher unabhängig davon, ob gerade gestreamt
        oder aufgenommen wird, statt zu pausieren."""
        device = self.config.audio_device
        if not device:
            self.level_meter.stop()
            return
        if not self.level_meter.is_running_for(SHARED_CAPTURE_DEVICE):
            self.level_meter.stop()
            self.level_meter.start(SHARED_CAPTURE_DEVICE, self.config.channels, self.config.sample_rate)

    def current_audio_level(self) -> LevelReading:
        return self.level_meter.reading()


def create_app(config_path: Path = DEFAULT_CONFIG_PATH) -> Flask:
    app = Flask(__name__)
    app.state = AppState(config_path)  # type: ignore[attr-defined]
    configure_logging(app.state.config.log_level)
    app.secret_key = _load_or_create_secret_key(config_path.parent)
    app.permanent_session_lifetime = timedelta(days=30)

    from . import routes

    routes.register(app)
    return app
