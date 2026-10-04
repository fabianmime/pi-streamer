import sys
import time
from datetime import datetime

import pytest

from app.config import SHARED_CAPTURE_DEVICE, StreamerConfig
from app.recorder import RecordingManager, build_ffmpeg_record_cmd, recording_filename


def test_build_ffmpeg_record_cmd_contains_expected_pieces():
    cfg = StreamerConfig(channels=2, sample_rate=48000)

    cmd = build_ffmpeg_record_cmd(cfg, "/media/stick/aufnahme.wav", ffmpeg_bin="ffmpeg")

    assert cmd[0] == "ffmpeg"
    assert SHARED_CAPTURE_DEVICE in cmd
    assert "pcm_s16le" in cmd
    assert "-f" in cmd and "wav" in cmd
    assert cmd[-1] == "/media/stick/aufnahme.wav"


def test_build_ffmpeg_record_cmd_requires_output_path():
    cfg = StreamerConfig()
    with pytest.raises(ValueError):
        build_ffmpeg_record_cmd(cfg, "")


def test_recording_filename_uses_timestamp():
    name = recording_filename("Aufnahme", datetime(2026, 9, 9, 20, 15, 30))
    assert name == "Aufnahme-20260909-201530.wav"


def test_recording_filename_uses_custom_prefix():
    name = recording_filename("Konzert", datetime(2026, 9, 9, 20, 15, 30))
    assert name == "Konzert-20260909-201530.wav"


def _sleep_cmd_builder(_cfg, _output_path, seconds: float = 2.0):
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def _exit_immediately_cmd_builder(_cfg, _output_path, code: int = 1):
    return [sys.executable, "-c", f"import sys; sys.exit({code})"]


def test_start_then_status_reports_running(tmp_path):
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig(channels=2, sample_rate=48000)

    status = manager.start(cfg, str(tmp_path))
    try:
        assert status.running is True
        assert status.pid is not None
        assert status.output_path.startswith(str(tmp_path))
        assert manager.status().running is True
    finally:
        manager.stop()


def test_start_without_target_dir_raises():
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig()

    with pytest.raises(RuntimeError):
        manager.start(cfg, "")


def test_start_with_nonexistent_target_dir_raises(tmp_path):
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig()

    with pytest.raises(RuntimeError):
        manager.start(cfg, str(tmp_path / "nicht-gemountet"))


def test_start_twice_raises(tmp_path):
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig()

    manager.start(cfg, str(tmp_path))
    try:
        with pytest.raises(RuntimeError):
            manager.start(cfg, str(tmp_path))
    finally:
        manager.stop()


def test_stop_terminates_process(tmp_path):
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig()

    manager.start(cfg, str(tmp_path))
    status = manager.stop()

    assert status.running is False
    assert manager.status().running is False


def test_deliberate_stop_does_not_report_as_error(tmp_path):
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    cfg = StreamerConfig()

    manager.start(cfg, str(tmp_path))
    status = manager.stop()

    assert status.last_error is None
    assert manager.status().last_error is None


def test_stop_when_not_running_is_noop():
    manager = RecordingManager(cmd_builder=_sleep_cmd_builder)
    status = manager.stop()
    assert status.running is False


def test_clear_error_hides_last_error_without_touching_status(tmp_path):
    manager = RecordingManager(cmd_builder=lambda cfg, out: _exit_immediately_cmd_builder(cfg, out, code=1))
    cfg = StreamerConfig()

    manager.start(cfg, str(tmp_path))
    deadline = time.monotonic() + 5
    status = manager.status()
    while status.running and time.monotonic() < deadline:
        time.sleep(0.05)
        status = manager.status()
    assert status.last_error is not None

    manager.clear_error()

    assert manager.status().last_error is None


def test_process_exiting_with_error_is_reflected_in_status(tmp_path):
    manager = RecordingManager(cmd_builder=lambda cfg, out: _exit_immediately_cmd_builder(cfg, out, code=1))
    cfg = StreamerConfig()

    manager.start(cfg, str(tmp_path))
    deadline = time.monotonic() + 5
    status = manager.status()
    while status.running and time.monotonic() < deadline:
        time.sleep(0.05)
        status = manager.status()

    assert status.running is False
    assert status.last_exit_code == 1
    assert status.last_error is not None
