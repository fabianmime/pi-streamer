"""Kontinuierliche Eingangspegel-Messung des Audio-Geräts.

Soll laut Anforderung *immer* den Eingang anzeigen - auch während Stream
oder Aufnahme laufen. `LevelMeter` unten öffnet dafür einen eigenständigen
ffmpeg-Prozess (Ausgabe verworfen, `-f null -`), der wie Stream und
Recording über den geteilten `dsnoop`-Alias `SHARED_CAPTURE_DEVICE` liest
(siehe app/config.py) statt über das rohe `plughw:`-Gerät - `dsnoop` erlaubt
mehreren Prozessen gleichzeitigen Zugriff auf dasselbe USB-Audio-Interface,
im Unterschied zu einem direkten `hw:`/`plughw:`-Zugriff (der lässt nur
einen Leser gleichzeitig zu, empirisch geprüft: ein zweiter
`ffmpeg -f alsa -i <device>` bricht dann mit "Device or resource busy" ab).

Das Starten/Neustarten (z.B. nach Geräte-Wechsel oder wenn der Prozess von
selbst beendet ist) übernimmt `AppState.ensure_level_meter()` in
app/__init__.py, aufgerufen bei jedem `/api/audio-level`-Poll.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_CHANNEL_PEAK_RE = re.compile(r"^lavfi\.astats\.(\d+)\.Peak_level=(.+)$")
_STALE_AFTER_SECONDS = 3.0


def build_level_filter() -> str:
    """Der -af-Filterausdruck: misst Peak-Pegel pro Audio-Puffer und Kanal
    (L/R bei Stereo) und druckt ihn kontinuierlich auf stdout - ohne die
    Audiodaten selbst zu verändern, kann also parallel zur eigentlichen
    Kodierung mitlaufen.

    `measure_perchannel=Peak_level:measure_overall=none` beschränkt astats
    bewusst auf genau die eine Kennzahl pro Kanal (statt aller ~15
    Kennzahlen von astats, mal Kanal 1/2/Overall) - sonst würde jede der
    mehrmals pro Sekunde gedruckten Metadaten-Zeilen unnötig aufgebläht, ohne
    dass die WebUI-Anzeige (nur Peak pro Kanal) das je braucht.

    `direct=1` ist Pflicht: `ametadata=print` puffert seine Ausgabe sonst
    intern (avio-Buffer, nicht die übliche libc-stdio-Pufferung - ein
    `stdbuf -oL`-Wrapper hilft darum nicht), wenn das Ziel eine Pipe statt
    ein Terminal ist - ohne `direct=1` kommt beim Lesen über
    `subprocess.Popen(..., stdout=PIPE)` schlicht nie etwas an, bis der
    Prozess beendet wird. Live auf dem Pi verifiziert (ohne `direct=1`: 0
    Zeilen in 5s: mit `direct=1`: Zeilen alle ~120ms, wie erwartet bei
    `length=0.1`)."""
    return (
        "astats=metadata=1:reset=1:length=0.1:measure_perchannel=Peak_level:measure_overall=none,"
        "ametadata=print:file=-:direct=1"
    )


def parse_channel_peak_line(line: str) -> tuple[int, float | None] | None:
    """Erkennt eine Pro-Kanal-Peak-Zeile (`lavfi.astats.<N>.Peak_level=...`,
    1-basiert - Kanal 1 = links/mono, Kanal 2 = rechts bei Stereo) und gibt
    `(Kanalnummer, Wert)` zurück. Wert ist `None` bei -inf/inf/NaN (=Stille),
    die Zeile selbst gilt aber trotzdem als erkannt (Rückgabe ist nicht
    `None`) - das unterscheidet "Kanal liefert gerade Stille" von "Zeile
    gehört gar nicht zu einer Pegel-Messung" (z.B. der `frame:N pts:...`-
    Kopf, den `ametadata=print` vor jeder Kanal-Zeile ausgibt). Python's
    `float()` parsed "-inf"/"nan" anstandslos als gültige Sonderwerte -
    deshalb hier explizit auf math.isfinite prüfen, statt uns nur auf
    ValueError zu verlassen."""
    match = _CHANNEL_PEAK_RE.match(line.strip())
    if not match:
        return None
    channel = int(match.group(1))
    try:
        value = float(match.group(2))
    except ValueError:
        return None
    if not math.isfinite(value):
        value = None
    return channel, value


@dataclass
class LevelReading:
    channel_peaks_db: list[float | None]  # Index 0 = Kanal 1 (L/mono), Index 1 = Kanal 2 (R) bei Stereo
    available: bool


class LevelTap:
    """Liest Pro-Kanal-Peak-Pegel-Zeilen von stdout eines ffmpeg-Prozesses in
    einem eigenen Thread und hält nur die zuletzt gemessenen Werte pro Kanal
    threadsicher vor - ältere Werte werden nicht aufgehoben, ein Pegelmeter
    braucht nur den aktuellen Stand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._channel_peaks: dict[int, float | None] = {}
        self._last_update: float = 0.0

    def attach(self, process: subprocess.Popen) -> None:
        threading.Thread(target=self._read, args=(process,), daemon=True).start()

    def _read(self, process: subprocess.Popen) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            parsed = parse_channel_peak_line(line)
            if parsed is None:
                continue
            channel, value = parsed
            with self._lock:
                self._channel_peaks[channel] = value
                self._last_update = time.monotonic()

    def reading(self) -> LevelReading:
        with self._lock:
            peaks = dict(self._channel_peaks)
            last_update = self._last_update
        age = time.monotonic() - last_update if last_update else None
        available = age is not None and age <= _STALE_AFTER_SECONDS
        if not available or not peaks:
            return LevelReading(channel_peaks_db=[], available=available)
        ordered = [peaks.get(i) for i in range(1, max(peaks) + 1)]
        return LevelReading(channel_peaks_db=ordered, available=available)

    def reset(self) -> None:
        with self._lock:
            self._channel_peaks = {}
            self._last_update = 0.0


class LevelMeter:
    """Eigenständiger, dauerhaft laufender Mess-Prozess - unabhängig von
    Stream/Recording (siehe Modul-Docstring). Läuft nur genau dann, wenn
    `start()` explizit aufgerufen wurde und nicht per `stop()` beendet -
    siehe AppState.ensure_level_meter()."""

    def __init__(self, ffmpeg_bin: str = "ffmpeg") -> None:
        self._ffmpeg_bin = ffmpeg_bin
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._tap = LevelTap()
        self._device: str | None = None

    def start(self, device: str, channels: int, sample_rate: int) -> None:
        with self._lock:
            if self._process is not None:
                return
            if not device:
                return
            self._tap.reset()
            cmd = [
                self._ffmpeg_bin,
                "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "alsa",
                "-channels", str(channels),
                "-sample_rate", str(sample_rate),
                "-i", device,
                "-af", build_level_filter(),
                "-f", "null", "-",
            ]
            try:
                process = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
                )
            except OSError as exc:
                logger.warning("Pegel-Messung konnte nicht gestartet werden: %s", exc)
                return
            self._process = process
            self._device = device
            self._tap.attach(process)
            logger.info("Pegel-Messung gestartet (Gerät %s)", device)

    def stop(self) -> None:
        with self._lock:
            process = self._process
            if process is None:
                return
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            self._process = None
            self._device = None
            self._tap.reset()

    def is_running_for(self, device: str) -> bool:
        with self._lock:
            if self._process is None or self._device != device:
                return False
            return self._process.poll() is None

    def reading(self) -> LevelReading:
        return self._tap.reading()
