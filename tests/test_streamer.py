import sys
import time

import pytest

from app.config import SHARED_CAPTURE_DEVICE, StreamerConfig
from app.streamer import StreamManager, _friendly_error, build_ffmpeg_cmd


def test_build_ffmpeg_cmd_contains_expected_pieces():
    cfg = StreamerConfig(
        srt_url="srt://example.com:9000?streamid=abc&passphrase=xyz",
        audio_device="hw:1,0",
        channels=2,
        sample_rate=48000,
        bitrate_kbps=160,
    )

    cmd = build_ffmpeg_cmd(cfg, ffmpeg_bin="ffmpeg")

    assert cmd[0] == "ffmpeg"
    # Liest ueber den geteilten dsnoop-Alias, nicht direkt ueber
    # cfg.audio_device - siehe Docstring von build_ffmpeg_cmd (Recording
    # muss unabhaengig vom Stream mitlesen koennen).
    assert SHARED_CAPTURE_DEVICE in cmd
    assert "hw:1,0" not in cmd
    assert "160k" in cmd
    assert cmd[-1] == cfg.srt_url
    assert "-f" in cmd and "mpegts" in cmd
    # Bewusst OHNE Pegel-Filter im Live-Stream-Befehl - siehe Docstring von
    # build_ffmpeg_cmd (Backpressure-Risiko fuer die Audio-Pipeline).
    assert "-af" not in cmd


def test_build_ffmpeg_cmd_requires_audio_device():
    cfg = StreamerConfig(srt_url="srt://example.com:9000")
    with pytest.raises(ValueError):
        build_ffmpeg_cmd(cfg)


def test_build_ffmpeg_cmd_requires_srt_url():
    cfg = StreamerConfig(audio_device="hw:1,0")
    with pytest.raises(ValueError):
        build_ffmpeg_cmd(cfg)


def _sleep_cmd_builder(_cfg, seconds: float = 2.0):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _exit_immediately_cmd_builder(_cfg, code: int = 1):
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def test_start_then_status_reports_running():
    manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    status = manager.start(cfg)
    try:
        assert status.running is True
        assert status.pid is not None
        assert manager.status().running is True
    finally:
        manager.stop()


def test_start_twice_raises():
    manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    manager.start(cfg)
    try:
        with pytest.raises(RuntimeError):
            manager.start(cfg)
    finally:
        manager.stop()


def test_stop_terminates_process():
    manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    manager.start(cfg)
    status = manager.stop()

    assert status.running is False
    assert manager.status().running is False


def test_deliberate_stop_does_not_report_as_error():
    # ffmpeg beendet sich auch bei einem gewollten Stop nie mit Code 0
    # (SIGTERM/SIGKILL) - das darf nicht fälschlicherweise als Absturz/Fehler
    # im Status auftauchen (siehe echter Log vom 2026-09-06).
    manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    manager.start(cfg)
    status = manager.stop()

    assert status.running is False
    assert status.last_error is None
    assert manager.status().last_error is None


def test_stop_when_not_running_is_noop():
    manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    status = manager.stop()
    assert status.running is False


def test_process_exiting_with_error_is_reflected_in_status():
    manager = StreamManager(cmd_builder=lambda cfg: _exit_immediately_cmd_builder(cfg, code=1))
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    manager.start(cfg)
    deadline = time.monotonic() + 5
    status = manager.status()
    while status.running and time.monotonic() < deadline:
        time.sleep(0.05)
        status = manager.status()

    assert status.running is False
    assert status.last_exit_code == 1
    assert status.last_error is not None


def test_clear_error_hides_last_error_without_touching_process():
    manager = StreamManager(cmd_builder=lambda cfg: _exit_immediately_cmd_builder(cfg, code=1))
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    manager.start(cfg)
    deadline = time.monotonic() + 5
    status = manager.status()
    while status.running and time.monotonic() < deadline:
        time.sleep(0.05)
        status = manager.status()
    assert status.last_error is not None

    manager.clear_error()

    assert manager.status().last_error is None
    # last_exit_code bleibt erhalten - nur die Fehleranzeige wird geschlossen.
    assert manager.status().last_exit_code == 1


def test_clear_error_on_idle_manager_is_noop():
    manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    manager.clear_error()
    assert manager.status().last_error is None


def test_friendly_error_recognizes_srt_connect_failure():
    # Genau das Fehlerbild, wenn der Import bei LiveVoice noch nicht
    # gestartet wurde (ffmpeg im "caller"-Modus, kein SRT-Listener aktiv).
    tail = (
        "[srt @ 0x558a188010] Connection to "
        "srt://import-ec-01.streaming.livevoice.io:12517?mode=caller failed: "
        "Input/output error"
    )
    assert _friendly_error(251, tail) == (
        "Verbindung zu LiveVoice fehlgeschlagen - zuerst den Import bei "
        "LiveVoice (im Operator oder Dashboard) starten, dann den Stream hier "
        "erneut starten."
    )


def test_friendly_error_recognizes_remote_stopped_mid_stream():
    # Genau das Fehlerbild, wenn LiveVoice den Import waehrend eines
    # laufenden Streams selbst stoppt (Verbindungsabbruch beim Schreiben,
    # nicht beim Verbindungsaufbau).
    tail = (
        "[out#0/mpegts @ 0x1] Error muxing a packet\n"
        "[out#0/mpegts @ 0x1] Task finished with error code: -5 "
        "(Input/output error)\n"
        "[out#0/mpegts @ 0x1] Error writing trailer: Input/output error"
    )
    assert _friendly_error(251, tail) == (
        "Stream wurde von LiveVoice-Seite beendet (Import dort gestoppt oder "
        "Verbindung unterbrochen) - bei Bedarf hier erneut starten."
    )


def test_friendly_error_leaves_unknown_errors_raw():
    assert _friendly_error(1, "irgendein anderer ffmpeg-Fehler") == (
        "ffmpeg beendet mit Code 1: irgendein anderer ffmpeg-Fehler"
    )


def _srt_connect_failure_cmd_builder(_cfg):
    message = (
        "[srt @ 0x1] Connection to srt://example.com:1?mode=caller failed: "
        "Input/output error"
    )
    return [sys.executable, "-c", f"import sys; print({message!r}, file=sys.stderr); sys.exit(251)"]


def test_stream_shows_friendly_hint_on_srt_connect_failure():
    manager = StreamManager(cmd_builder=_srt_connect_failure_cmd_builder)
    cfg = StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=False)

    manager.start(cfg)
    deadline = time.monotonic() + 5
    status = manager.status()
    while status.running and time.monotonic() < deadline:
        time.sleep(0.05)
        status = manager.status()

    assert status.last_error == (
        "Verbindung zu LiveVoice fehlgeschlagen - zuerst den Import bei "
        "LiveVoice (im Operator oder Dashboard) starten, dann den Stream hier "
        "erneut starten."
    )
