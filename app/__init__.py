from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask

from . import audio, storage, updater
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
        self.runtime_path = config_path.parent / "runtime.json"
        self.config: StreamerConfig = load_config(config_path)
        self.stream_manager = StreamManager(on_desired_change=self._remember_stream_wanted)
        self.recording_manager = RecordingManager()
        self.level_meter = LevelMeter()
        self.updater = updater.Updater(restart=updater.schedule_process_restart)
        self.sync_dsnoop_config()

    def save(self) -> None:
        save_config(self.config, self.config_path)

    def is_busy(self) -> bool:
        """Stream (auch während automatischem Neuverbinden) oder Aufnahme
        aktiv - dann keine Updates, die den Dienst neu starten."""
        stream = self.stream_manager.status()
        return stream.running or stream.reconnecting or self.recording_manager.status().running

    # --- Stream nach Stromausfall/Neustart fortsetzen -------------------------

    def _remember_stream_wanted(self, wanted: bool) -> None:
        """Merkt sich auf der SD-Karte, ob der Stream gewollt läuft - per
        Start/Stop im Cockpit, nicht bei einem Abbruch. So kann
        `resume_stream_if_wanted` nach einem Stromausfall weiterstreamen,
        aber nicht nach einem bewussten Stop + Herunterfahren."""
        payload = json.dumps({"stream_wanted": wanted}) + "\n"
        tmp = self.runtime_path.with_name(self.runtime_path.name + ".tmp")
        self.runtime_path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open("w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.runtime_path)

    def stream_was_wanted(self) -> bool:
        try:
            return bool(json.loads(self.runtime_path.read_text(encoding="utf-8")).get("stream_wanted"))
        except (OSError, ValueError, AttributeError):
            return False

    def resume_stream_if_wanted(self) -> bool:
        """Beim Dienst-Start: lief der Stream vor dem Stromausfall/Absturz
        gewollt, und ist Auto-Reconnect aktiv, gleich wieder starten. Ist das
        Netz noch nicht da, übernimmt das automatische Neuverbinden."""
        if not (self.config.stream_auto_reconnect and self.config.is_ready_to_stream() and self.stream_was_wanted()):
            return False
        try:
            self.stream_manager.start(self.config)
        except RuntimeError as exc:
            logging.getLogger(__name__).warning("Stream nach Neustart nicht fortgesetzt: %s", exc)
            return False
        logging.getLogger(__name__).info("Stream nach Neustart automatisch fortgesetzt")
        return True

    def refresh_audio_devices(self):
        # Ueber das Modul aufgerufen (nicht als frueh gebundener Funktions-
        # Import), damit Tests mit monkeypatch.setattr(audio, "...") greifen -
        # genau wie storage.mount_drive() in routes.py.
        return audio.list_capture_devices()

    def refresh_usb_drives(self):
        return storage.list_usb_drives()

    def sync_dsnoop_config(self) -> None:
        """Regeneriert `~/.asoundrc` (siehe app/audio.py::write_dsnoop_config)
        für das aktuell konfigurierte Audio-Gerät - beim App-Start sowie nach
        jedem Speichern der Einstellungen.

        Sucht das Gerät zuerst über seine stabile ALSA-Kurzkennung
        (`audio_card_id`, z.B. "Scarlett") und erst dann über die Kartennummer
        aus `audio_device` ("plughw:1,0"): die Nummer kann sich ändern, wenn
        beim Booten weitere USB-Geräte stecken - dann würde sonst still das
        falsche Gerät gestreamt. Hat sich die Nummer verschoben, wird
        `audio_device` nachgeführt und gespeichert.

        Kein Effekt, wenn kein Gerät konfiguriert ist oder es aktuell nicht
        per `arecord -l` erkannt wird (z.B. `arecord` fehlt in Tests) - dann
        bleibt eine vorhandene Konfiguration einfach stehen."""
        device_hw_id = self.config.audio_device
        if not device_hw_id:
            return
        devices = self.refresh_audio_devices()
        match = None
        if self.config.audio_card_id:
            match = next((d for d in devices if d.card_id == self.config.audio_card_id), None)
        if match is None:
            match = next((d for d in devices if d.hw_id == device_hw_id), None)
        if match is None:
            return
        changed = False
        if match.hw_id != self.config.audio_device:
            logging.getLogger(__name__).info(
                "Audio-Gerät %s hängt jetzt an %s statt %s - Einstellung nachgeführt",
                match.card_id,
                match.hw_id,
                self.config.audio_device,
            )
            self.config.audio_device = match.hw_id
            changed = True
        if match.card_id != self.config.audio_card_id:
            self.config.audio_card_id = match.card_id
            changed = True
        if changed:
            self.save()
        audio.write_dsnoop_config(match.card_id, self.config.channels, self.config.sample_rate)

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
    # Login-Cookie nicht bei Anfragen mitschicken, die von fremden Seiten
    # ausgelöst werden (einfacher CSRF-Schutz für die POST-Endpunkte).
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    from . import routes

    routes.register(app)
    return app
