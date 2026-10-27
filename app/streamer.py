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


# Wartezeit vor dem n-ten Neuverbindungsversuch (danach bleibt es beim
# letzten Wert) - bewusst ohne Obergrenze an Versuchen: an einem Event soll
# der Stream zurückkommen, sobald Netz/LiveVoice-Import wieder da sind, auch
# wenn das Minuten dauert.
RECONNECT_DELAYS_SECONDS = (2.0, 5.0, 10.0, 15.0, 30.0)
# Lief der Stream mindestens so lange stabil, beginnt die Wartezeit-Reihe
# beim nächsten Abbruch wieder von vorn.
_STABLE_RUN_SECONDS = 30.0


def reconnect_delay(attempt: int, delays: tuple[float, ...] = RECONNECT_DELAYS_SECONDS) -> float:
    """Wartezeit vor Versuch `attempt` (1-basiert)."""
    index = min(max(attempt, 1), len(delays)) - 1
    return delays[index]


@dataclass
class StreamStatus:
    running: bool
    pid: int | None
    started_at: float | None
    uptime_seconds: float
    last_exit_code: int | None
    last_error: str | None
    log_tail: list[str] = field(default_factory=list)
    # Stream ist abgebrochen und wird automatisch neu verbunden (zählt aus
    # Sicht des Cockpits als "soll laufen", Stop-Knopf bricht das ab).
    reconnecting: bool = False
    reconnect_attempt: int = 0
    reconnect_in_seconds: float | None = None
    reconnects_total: int = 0


class StreamManager:
    """Kapselt genau einen ffmpeg-Kindprozess. Nicht für mehrere parallele
    Streams gedacht - dieses Gerät fährt einen Stream zu einer Zeit.

    Auto-Reconnect: Ein Überwachungs-Thread wartet auf das Ende des
    ffmpeg-Prozesses. Endet er, ohne dass jemand "Stoppen" gedrückt hat, und
    ist `auto_reconnect` aktiv, startet er ffmpeg nach einer wachsenden
    Wartezeit (RECONNECT_DELAYS_SECONDS) neu - unabhängig davon, ob gerade
    jemand das Cockpit offen hat. `_generation` wird bei jedem Start/Stop
    hochgezählt, damit ein veralteter Überwachungs-Thread nach einem
    manuellen Stop/Neustart nichts mehr anfasst."""

    def __init__(
        self,
        cmd_builder: CmdBuilder = build_ffmpeg_cmd,
        reconnect_delays: tuple[float, ...] = RECONNECT_DELAYS_SECONDS,
        on_desired_change: Callable[[bool], None] | None = None,
    ) -> None:
        self._cmd_builder = cmd_builder
        self._reconnect_delays = reconnect_delays
        # Wird mit True/False aufgerufen, wenn der Stream per Start/Stop
        # gewollt ein-/ausgeschaltet wird - AppState merkt sich das auf der
        # SD-Karte, um nach einem Stromausfall weiterzustreamen.
        self._on_desired_change = on_desired_change
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_exit_code: int | None = None
        self._last_error: str | None = None
        self._log_tail: deque[str] = deque(maxlen=_LOG_TAIL_LINES)
        self._reader_thread: threading.Thread | None = None
        self._stopping = False
        self._cfg: StreamerConfig | None = None
        self._auto_reconnect = False
        self._generation = 0
        self._cancel = threading.Event()
        self._reconnect_attempt = 0
        self._reconnect_at: float | None = None
        self._reconnects_total = 0

    def start(self, cfg: StreamerConfig) -> StreamStatus:
        with self._lock:
            self._reap_locked()
            if self._process is not None or self._reconnect_at is not None:
                raise RuntimeError("Stream läuft bereits")

            self._last_error = None
            self._last_exit_code = None
            self._log_tail.clear()
            self._cfg = cfg
            self._auto_reconnect = cfg.stream_auto_reconnect
            self._reconnect_attempt = 0
            self._reconnects_total = 0
            self._generation += 1
            self._cancel = threading.Event()

            self._spawn_locked()  # wirft RuntimeError, wenn ffmpeg nicht startet
            self._start_supervisor_locked()
            logger.info("Stream gestartet (PID %s, Gerät %s, %s kbps)", self._process.pid, cfg.audio_device, cfg.bitrate_kbps)
            status = self._status_locked()
        self._notify_desired(True)
        return status

    def stop(self, timeout: float = 5.0) -> StreamStatus:
        with self._lock:
            # Zuerst die Überwachung abmelden - sonst würde sie den gleich
            # beendeten Prozess als Absturz sehen und neu starten.
            was_active = self._process is not None or self._reconnect_at is not None
            self._generation += 1
            self._cancel.set()
            self._reconnect_at = None
            self._reconnect_attempt = 0

            process = self._process
            if process is not None:
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
            elif was_active:
                logger.info("Automatisches Neuverbinden abgebrochen")
            status = self._status_locked()
        if was_active:
            self._notify_desired(False)
        return status

    def status(self) -> StreamStatus:
        with self._lock:
            self._reap_locked()
            return self._status_locked()

    def _notify_desired(self, running: bool) -> None:
        if self._on_desired_change is None:
            return
        try:
            self._on_desired_change(running)
        except Exception:  # noqa: BLE001 - Merken ist Komfort, darf Start/Stop nie verhindern
            logger.exception("Stream-Zustand konnte nicht gespeichert werden")

    def _spawn_locked(self) -> None:
        """Nur mit gehaltenem self._lock aufrufen. Startet ffmpeg mit der beim
        letzten start() übergebenen Konfiguration."""
        assert self._cfg is not None
        cmd = self._cmd_builder(self._cfg)
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

    def _start_supervisor_locked(self) -> None:
        threading.Thread(
            target=self._supervise, args=(self._generation, self._cancel), daemon=True
        ).start()

    def _supervise(self, generation: int, cancel: threading.Event) -> None:
        while True:
            with self._lock:
                if generation != self._generation:
                    return
                process = self._process
                # Selbst merken statt self._started_at zu lesen - ein
                # status()-Poll kann den Prozess vor uns abräumen und setzt
                # _started_at dabei zurück.
                process_started_at = self._started_at
            if process is not None:
                process.wait()
            with self._lock:
                if generation != self._generation:
                    return
                if self._process is not None and self._process is not process:
                    continue  # bereits durch jemand anderen ersetzt
                ran_for = (
                    time.monotonic() - process_started_at
                    if process is not None and process_started_at is not None
                    else 0.0
                )
                self._reap_locked()
                if not self._auto_reconnect:
                    return
                if ran_for >= _STABLE_RUN_SECONDS:
                    self._reconnect_attempt = 0
                self._reconnect_attempt += 1
                delay = reconnect_delay(self._reconnect_attempt, self._reconnect_delays)
                self._reconnect_at = time.monotonic() + delay
                logger.warning(
                    "Stream abgebrochen - neuer Verbindungsversuch %s in %.0f s",
                    self._reconnect_attempt,
                    delay,
                )
            if cancel.wait(delay):
                return  # gestoppt, während wir gewartet haben
            with self._lock:
                if generation != self._generation:
                    return
                self._reconnect_at = None
                try:
                    self._spawn_locked()
                    # Der Abbruchgrund ist erledigt - im Cockpit bleibt nur
                    # der Zähler "x-mal neu verbunden" statt einer roten
                    # Fehlermeldung neben einem laufenden Stream.
                    self._last_error = None
                    self._reconnects_total += 1
                    logger.info(
                        "Stream neu verbunden (Versuch %s, PID %s)", self._reconnect_attempt, self._process.pid
                    )
                except RuntimeError:
                    logger.warning("Neustart von ffmpeg fehlgeschlagen: %s", self._last_error)
                    self._process = None  # nächste Runde plant den nächsten Versuch

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
            reconnecting=self._reconnect_at is not None,
            reconnect_attempt=self._reconnect_attempt,
            reconnect_in_seconds=(
                max(self._reconnect_at - time.monotonic(), 0.0) if self._reconnect_at is not None else None
            ),
            reconnects_total=self._reconnects_total,
        )
