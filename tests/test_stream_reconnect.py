import sys
import time

import pytest

from app.config import StreamerConfig
from app.streamer import StreamManager, reconnect_delay

_FAST_DELAYS = (0.05, 0.05, 0.05)


def _exit_cmd(_cfg, code: int = 1):
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def _sleep_cmd(_cfg, seconds: float = 5.0):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _cfg(auto_reconnect: bool = True) -> StreamerConfig:
    return StreamerConfig(srt_url="srt://x", audio_device="hw:1,0", stream_auto_reconnect=auto_reconnect)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_reconnect_delay_grows_and_stays_at_last_value():
    delays = (2.0, 5.0, 30.0)
    assert reconnect_delay(1, delays) == 2.0
    assert reconnect_delay(2, delays) == 5.0
    assert reconnect_delay(3, delays) == 30.0
    assert reconnect_delay(50, delays) == 30.0


def test_crashed_stream_is_restarted_without_anyone_polling():
    # Kein status()-Aufruf (kein Cockpit offen) - der Überwachungs-Thread
    # muss den Neustart selbst auslösen.
    spawned = []

    def builder(cfg):
        spawned.append(True)
        return _exit_cmd(cfg)

    manager = StreamManager(cmd_builder=builder, reconnect_delays=_FAST_DELAYS)
    manager.start(_cfg())
    try:
        assert _wait_for(lambda: len(spawned) >= 3)
    finally:
        manager.stop()


def test_reconnect_clears_error_and_counts_reconnects():
    runs = []

    def builder(cfg):
        runs.append(True)
        return _exit_cmd(cfg) if len(runs) == 1 else _sleep_cmd(cfg)

    manager = StreamManager(cmd_builder=builder, reconnect_delays=_FAST_DELAYS)
    manager.start(_cfg())
    try:
        assert _wait_for(lambda: manager.status().reconnects_total == 1 and manager.status().running)
        status = manager.status()
        assert status.last_error is None
        assert status.reconnecting is False
    finally:
        manager.stop()


def test_status_shows_reconnecting_while_waiting():
    manager = StreamManager(cmd_builder=_exit_cmd, reconnect_delays=(10.0,))
    manager.start(_cfg())
    try:
        assert _wait_for(lambda: manager.status().reconnecting)
        status = manager.status()
        assert status.running is False
        assert status.reconnect_attempt == 1
        assert 0 < status.reconnect_in_seconds <= 10.0
        assert status.last_error is not None
    finally:
        manager.stop()


def test_stop_during_reconnect_wait_cancels_reconnect():
    spawned = []

    def builder(cfg):
        spawned.append(True)
        return _exit_cmd(cfg)

    manager = StreamManager(cmd_builder=builder, reconnect_delays=(10.0,))
    manager.start(_cfg())
    assert _wait_for(lambda: manager.status().reconnecting)

    status = manager.stop()

    assert status.reconnecting is False
    assert status.running is False
    time.sleep(0.2)
    assert len(spawned) == 1


def test_start_after_cancelled_reconnect_works():
    manager = StreamManager(cmd_builder=_exit_cmd, reconnect_delays=(10.0,))
    manager.start(_cfg())
    assert _wait_for(lambda: manager.status().reconnecting)
    manager.stop()

    manager._cmd_builder = _sleep_cmd
    status = manager.start(_cfg())
    try:
        assert status.running is True
    finally:
        manager.stop()


def test_reconnecting_stream_blocks_second_start():
    manager = StreamManager(cmd_builder=_exit_cmd, reconnect_delays=(10.0,))
    manager.start(_cfg())
    try:
        assert _wait_for(lambda: manager.status().reconnecting)
        with pytest.raises(RuntimeError):
            manager.start(_cfg())
    finally:
        manager.stop()


def test_without_auto_reconnect_crash_stays_stopped():
    spawned = []

    def builder(cfg):
        spawned.append(True)
        return _exit_cmd(cfg)

    manager = StreamManager(cmd_builder=builder, reconnect_delays=_FAST_DELAYS)
    manager.start(_cfg(auto_reconnect=False))
    assert _wait_for(lambda: not manager.status().running)
    time.sleep(0.3)

    status = manager.status()
    assert len(spawned) == 1
    assert status.reconnecting is False
    assert status.last_error is not None


def test_desired_state_callback_only_on_deliberate_start_and_stop():
    calls = []
    manager = StreamManager(cmd_builder=_exit_cmd, reconnect_delays=_FAST_DELAYS, on_desired_change=calls.append)

    manager.start(_cfg())
    time.sleep(0.3)  # mehrere Abstürze + Neustarts ändern den Wunschzustand nicht
    manager.stop()

    assert calls == [True, False]


def test_stop_without_running_stream_does_not_report_desired_change():
    calls = []
    manager = StreamManager(cmd_builder=_sleep_cmd, on_desired_change=calls.append)
    manager.stop()
    assert calls == []
