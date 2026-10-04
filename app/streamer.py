"""Start/Stop-Verwaltung des ffmpeg-Prozesses, der vom USB-Audio-Interface
per SRT zu LiveVoice streamt.

Der eigentliche ffmpeg-Kommandozeilen-Bau (`build_ffmpeg_cmd`) ist bewusst
von der Prozessverwaltung (`StreamManager`) getrennt, damit ersteres ohne
echten ffmpeg-Prozess getestet werden kann und letzteres in Tests einen
Dummy-Befehl statt eines echten ffmpeg-Aufrufs bekommen kann.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from .config import SHARED_CAPTURE_DEVICE, StreamerConfig

CmdBuilder = Callable[[StreamerConfig], list[str]]

_LOG_TAIL_LINES = 200

logger = logging.getLogger(__name__)


def build_ffmpeg_cmd(cfg: StreamerConfig, ffmpeg_bin: str = "ffmpeg") -> list[str]:
    """Baut den ffmpeg-Befehl für Audio-only ALSA -> SRT/MPEG-TS Streaming.

    Bewusst OHNE den Pegel-Filter aus levelmeter.py: der schreibt sehr
    häufig (mehrmals pro Sekunde) auf stdout, und wenn der lesende
    Python-Thread das auch nur kurz nicht schnell genug abholt, kann ffmpeg
    beim Schreiben blockieren - das darf die eigentliche Live-Audio-Pipeline
    nie riskieren. Die Pegelanzeige läuft stattdessen als komplett
    eigenständiger ffmpeg-Prozess über denselben SHARED_CAPTURE_DEVICE-Alias
    (siehe levelmeter.py) und damit unabhängig vom Stream weiter.

    Liest bewusst über `SHARED_CAPTURE_DEVICE` (den `dsnoop`-Alias aus
    /etc/asound.conf, siehe DEPLOY.md) statt direkt über `cfg.audio_device`:
    so können die parallele Recording-Aufnahme (app/recorder.py) und die
    eigenständige Pegel-Messung (app/levelmeter.py) jederzeit unabhängig
    laufen, ohne dass der Stream dafür unterbrochen/neu gestartet werden
    müsste - alle drei ffmpeg-Prozesse teilen sich denselben ALSA-Capture-
    Stream. `cfg.audio_device` bleibt trotzdem Pflicht (siehe
    is_ready_to_stream) und wird weiterhin zur Anzeige im WebUI genutzt.
    """
    if not cfg.audio_device:
        raise ValueError("audio_device ist nicht gesetzt")
    if not cfg.srt_url:
        raise ValueError("srt_url ist nicht gesetzt")

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-f", "alsa",
        "-channels", str(cfg.channels),
        "-sample_rate", str(cfg.sample_rate),
        "-i", SHARED_CAPTURE_DEVICE,
        "-c:a", "aac",
        "-b:a", f"{cfg.bitrate_kbps}k",
        "-ar", str(cfg.sample_rate),
        "-f", "mpegts",
        cfg.srt_url,
    ]


_SRT_CONNECT_FAILED_HINT = (
    "Verbindung zu LiveVoice fehlgeschlagen - zuerst den Import bei "
    "LiveVoice (im Operator oder Dashboard) starten, dann den Stream hier "
    "erneut starten."
)
_SRT_REMOTE_STOPPED_HINT = (
    "Stream wurde von LiveVoice-Seite beendet (Import dort gestoppt oder "
    "Verbindung unterbrochen) - bei Bedarf hier erneut starten."
)


def _friendly_error(returncode: int, tail: str) -> str:
    """Ersetzt das rohe ffmpeg-Log durch eine verständliche Meldung für
    bekannte, oft auftretende Fehlerbilder - unbekannte Fehler zeigen
    weiterhin das rohe Log, damit nichts verschluckt wird:

    - SRT-Verbindung schlägt schon beim Verbindungsaufbau fehl, weil der
      Import bei LiveVoice noch nicht gestartet wurde (ffmpeg meldet dann
      "Connection to srt://...failed: Input/output error" im
      "caller"-Modus).
    - SRT-Verbindung bricht waehrend eines laufenden Streams ab, weil der
      Import bei LiveVoice auf der Gegenseite gestoppt wurde (ffmpeg meldet
      dann "Error muxing a packet"/"Error writing trailer" beim Schreiben in
      den mpegts-Muxer, nicht beim Verbindungsaufbau)."""
    if "Connection to srt://" in tail and "failed" in tail:
        return _SRT_CONNECT_FAILED_HINT
    if "Error muxing a packet" in tail or "Error writing trailer" in tail:
        return _SRT_REMOTE_STOPPED_HINT
    return f"ffmpeg beendet mit Code {returncode}" + (f": {tail}" if tail else "")


@dataclass
class StreamStatus:
    running: bool
    pid: int | None
    started_at: float | None
    uptime_seconds: float
    last_exit_code: int | None
    last_error: str | None
    log_tail: list[str] = field(default_factory=list)


class StreamManager:
    """Kapselt genau einen ffmpeg-Kindprozess. Nicht für mehrere parallele
    Streams gedacht - dieses Gerät fährt einen Stream zu einer Zeit."""

    def __init__(self, cmd_builder: CmdBuilder = build_ffmpeg_cmd) -> None:
        self._cmd_builder = cmd_builder
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._log_tail: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._reader_thread: threading.Thread | None = None
        self._stopping = False

    def start(self, cfg: StreamerConfig) -> StreamStatus:
        with self._lock:
            self._reap_locked()
            if self._process is not None:
                raise RuntimeError("Stream läuft bereits")

            self._last_error = None
            self._last_exit_code = None
            self._log_tail.clear()

            cmd = self._cmd_builder(cfg)
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                self._last_error = f"ffmpeg konnte nicht gestartet werden: {exc}"
                raise RuntimeError(self._last_error) from exc

            self._process = process
            self._started_at = time.monotonic()
            self._reader_thread = threading.Thread(
                target=self._read_stderr, args=(process,), daemon=True
            )
            self._reader_thread.start()
            logger.info("Stream gestartet (PID %s, Gerät %s, %s kbps)", process.pid, cfg.audio_device, cfg.bitrate_kbps)
            return self._status_locked()

    def stop(self, timeout: float = 5.0) -> StreamStatus:
        with self._lock:
            process = self._process
            if process is None:
                return self._status_locked()

            self._stopping = True
            try:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=timeout)

                logger.info("Stream gestoppt (PID %s)", process.pid)
                self._reap_locked()
            finally:
                self._stopping = False
            return self._status_locked()

    def status(self) -> StreamStatus:
        with self._lock:
            self._reap_locked()
            return self._status_locked()

    def clear_error(self) -> None:
        """Blendet eine vergangene Fehlermeldung im WebUI aus, ohne den
        Stream neu zu starten - der naechste Start setzt last_error ohnehin
        zurueck, das hier ist nur fuer "Fehler schliessen" bei einem
        Fehler, den der Nutzer nicht direkt neu starten will/kann."""
        with self._lock:
            self._last_error = None

    def _read_stderr(self, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._log_tail.append(line.rstrip("\n"))

    def _reap_locked(self) -> None:
        """Nur mit gehaltenem self._lock aufrufen. Prüft, ob der Prozess von
        selbst beendet ist (z.B. SRT-Verbindungsfehler) und räumt auf.

        ffmpeg beendet sich auch bei einem gewollten Stop nie mit Code 0
        (SIGTERM/SIGKILL) - `self._stopping` unterscheidet den erwarteten
        Stop (kein Fehler) vom echten Absturz (Fehler wird gesetzt/geloggt).
        """
        process = self._process
        if process is None:
            return
        returncode = process.poll()
        if returncode is None:
            return
        self._last_exit_code = returncode
        if returncode != 0 and not self._stopping:
            tail = "\n".join(list(self._log_tail)[-5:])
            self._last_error = _friendly_error(returncode, tail)
            logger.warning("ffmpeg unerwartet beendet (Code %s): %s", returncode, tail)
        self._process = None
        self._started_at = None

    def _status_locked(self) -> StreamStatus:
        running = self._process is not None
        uptime = (
            time.monotonic() - self._started_at
            if running and self._started_at is not None
            else 0.0
        )
        return StreamStatus(
            running=running,
            pid=self._process.pid if running and self._process else None,
            started_at=self._started_at,
            uptime_seconds=uptime,
            last_exit_code=self._last_exit_code,
            last_error=self._last_error,
            log_tail=list(self._log_tail),
        )
