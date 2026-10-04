"""Start/Stop-Verwaltung des ffmpeg-Prozesses, der unabhängig vom Stream
das Eingangssignal verlustfrei als WAV auf ein Ziel (USB-Stick) aufzeichnet.

Liest über den geteilten ALSA-Capture-Alias `SHARED_CAPTURE_DEVICE` (siehe
app/config.py und DEPLOY.md), nicht über das rohe `plughw:`-Gerät aus den
Einstellungen: ein USB-Audio-Interface erlaubt sonst nur einem Prozess
gleichzeitigen Zugriff (siehe app/levelmeter.py-Docstring, "Device or
resource busy"). Der `dsnoop`-Alias aus /etc/asound.conf löst das, indem
Stream- und Recording-ffmpeg denselben, von ALSA geteilten Capture-Stream
lesen statt das Hardware-Gerät direkt zu öffnen - Recording lässt sich damit
jederzeit unabhängig vom Stream starten/stoppen, ohne diesen zu
unterbrechen.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import SHARED_CAPTURE_DEVICE, StreamerConfig

CmdBuilder = Callable[[StreamerConfig, str], list[str]]

_LOG_TAIL_LINES = 200

logger = logging.getLogger(__name__)


def build_ffmpeg_record_cmd(cfg: StreamerConfig, output_path: str, ffmpeg_bin: str = "ffmpeg") -> list[str]:
    if not output_path:
        raise ValueError("output_path ist nicht gesetzt")

    return [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel", "warning",
        "-nostdin",
        "-f", "alsa",
        "-channels", str(cfg.channels),
        "-sample_rate", str(cfg.sample_rate),
        "-i", SHARED_CAPTURE_DEVICE,
        "-c:a", "pcm_s16le",
        "-f", "wav",
        output_path,
    ]


def recording_filename(prefix: str, now: datetime | None = None) -> str:
    now = now or datetime.now()
    return f"{prefix}-{now:%Y%m%d-%H%M%S}.wav"


@dataclass
class RecordingStatus:
    running: bool
    pid: int | None
    started_at: float | None
    uptime_seconds: float
    last_exit_code: int | None
    last_error: str | None
    output_path: str | None
    log_tail: list[str] = field(default_factory=list)


class RecordingManager:
    """Kapselt genau einen ffmpeg-Kindprozess für die Aufnahme - analog zu
    StreamManager (streamer.py), aber mit einem Ziel-Dateipfad statt einer
    SRT-URL und unabhängig vom Stream startbar/stoppbar."""

    def __init__(self, cmd_builder: CmdBuilder = build_ffmpeg_record_cmd) -> None:
        self._cmd_builder = cmd_builder
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._output_path: str | None = None
        self._log_tail: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._reader_thread: threading.Thread | None = None
        self._stopping = False

    def start(self, cfg: StreamerConfig, target_dir: str) -> RecordingStatus:
        with self._lock:
            self._reap_locked()
            if self._process is not None:
                raise RuntimeError("Aufnahme läuft bereits")
            if not target_dir:
                raise RuntimeError("Kein Aufnahme-Ziel gewählt.")

            target_path = Path(target_dir)
            if not target_path.is_dir():
                raise RuntimeError(f"Aufnahme-Ziel nicht erreichbar: {target_dir}")

            output_path = str(target_path / recording_filename(cfg.recording_filename_prefix))

            self._last_error = None
            self._last_exit_code = None
            self._log_tail.clear()

            cmd = self._cmd_builder(cfg, output_path)
            try:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                )
            except OSError as exc:
                self._last_error = f"ffmpeg (Aufnahme) konnte nicht gestartet werden: {exc}"
                raise RuntimeError(self._last_error) from exc

            self._process = process
            self._output_path = output_path
            self._started_at = time.monotonic()
            self._reader_thread = threading.Thread(
                target=self._read_stderr, args=(process,), daemon=True
            )
            self._reader_thread.start()
            logger.info("Aufnahme gestartet (PID %s, Ziel %s)", process.pid, output_path)
            return self._status_locked()

    def stop(self, timeout: float = 5.0) -> RecordingStatus:
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

                logger.info("Aufnahme gestoppt (PID %s)", process.pid)
                self._reap_locked()
            finally:
                self._stopping = False
            return self._status_locked()

    def status(self) -> RecordingStatus:
        with self._lock:
            self._reap_locked()
            return self._status_locked()

    def clear_error(self) -> None:
        """Blendet eine vergangene Fehlermeldung im WebUI aus, ohne die
        Aufnahme neu zu starten - siehe StreamManager.clear_error()."""
        with self._lock:
            self._last_error = None

    def _read_stderr(self, process: subprocess.Popen) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            self._log_tail.append(line.rstrip("\n"))

    def _reap_locked(self) -> None:
        """Nur mit gehaltenem self._lock aufrufen - siehe StreamManager._reap_locked
        (streamer.py) für dieselbe Begründung (SIGTERM/SIGKILL beenden ffmpeg nie
        mit Code 0, self._stopping unterscheidet gewollten Stop von echtem Absturz)."""
        process = self._process
        if process is None:
            return
        returncode = process.poll()
        if returncode is None:
            return
        self._last_exit_code = returncode
        if returncode != 0 and not self._stopping:
            tail = "\n".join(list(self._log_tail)[-5:])
            self._last_error = f"ffmpeg (Aufnahme) beendet mit Code {returncode}" + (
                f": {tail}" if tail else ""
            )
            logger.warning("Aufnahme-ffmpeg unerwartet beendet (Code %s): %s", returncode, tail)
        self._process = None
        self._started_at = None

    def _status_locked(self) -> RecordingStatus:
        running = self._process is not None
        uptime = (
            time.monotonic() - self._started_at
            if running and self._started_at is not None
            else 0.0
        )
        return RecordingStatus(
            running=running,
            pid=self._process.pid if running and self._process else None,
            started_at=self._started_at,
            uptime_seconds=uptime,
            last_exit_code=self._last_exit_code,
            last_error=self._last_error,
            output_path=self._output_path if running else None,
            log_tail=list(self._log_tail),
        )
