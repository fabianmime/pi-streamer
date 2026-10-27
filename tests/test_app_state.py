from app import AppState
from app.config import SHARED_CAPTURE_DEVICE


def test_ensure_level_meter_does_nothing_without_device(tmp_path):
    state = AppState(config_path=tmp_path / "config.json")
    calls = []
    state.level_meter.start = lambda *a, **kw: calls.append((a, kw))

    state.ensure_level_meter()

    assert calls == []


def test_ensure_level_meter_starts_meter_via_shared_capture_device(tmp_path, monkeypatch):
    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "hw:1,0"

    monkeypatch.setattr(state.level_meter, "is_running_for", lambda device: False)
    start_calls = []
    monkeypatch.setattr(state.level_meter, "start", lambda device, channels, sample_rate: start_calls.append(
        (device, channels, sample_rate)
    ))
    stop_calls = []
    monkeypatch.setattr(state.level_meter, "stop", lambda: stop_calls.append(True))

    state.ensure_level_meter()

    # Nicht "hw:1,0" (das rohe cfg.audio_device), sondern der geteilte
    # dsnoop-Alias - erlaubt gleichzeitigen Zugriff mit Stream/Recording.
    assert start_calls == [(SHARED_CAPTURE_DEVICE, state.config.channels, state.config.sample_rate)]
    assert stop_calls == [True]


def test_ensure_level_meter_skips_start_when_already_running_for_shared_device(tmp_path, monkeypatch):
    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "hw:1,0"

    monkeypatch.setattr(state.level_meter, "is_running_for", lambda device: device == SHARED_CAPTURE_DEVICE)
    start_calls = []
    monkeypatch.setattr(state.level_meter, "start", lambda *a, **kw: start_calls.append((a, kw)))

    state.ensure_level_meter()

    assert start_calls == []


def test_ensure_level_meter_starts_even_while_stream_running(tmp_path, monkeypatch):
    # Anders als frueher pausiert die Pegelmessung NICHT mehr waehrend eines
    # Streams - beide lesen ueber den geteilten dsnoop-Alias parallel.
    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "hw:1,0"
    monkeypatch.setattr(state.stream_manager, "status", lambda: type("S", (), {"running": True})())
    monkeypatch.setattr(state.level_meter, "is_running_for", lambda device: False)

    start_calls = []
    monkeypatch.setattr(state.level_meter, "start", lambda *a, **kw: start_calls.append((a, kw)))

    state.ensure_level_meter()

    assert len(start_calls) == 1


def test_ensure_level_meter_starts_even_while_recording_running(tmp_path, monkeypatch):
    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "hw:1,0"
    monkeypatch.setattr(state.recording_manager, "status", lambda: type("S", (), {"running": True})())
    monkeypatch.setattr(state.level_meter, "is_running_for", lambda device: False)

    start_calls = []
    monkeypatch.setattr(state.level_meter, "start", lambda *a, **kw: start_calls.append((a, kw)))

    state.ensure_level_meter()

    assert len(start_calls) == 1


def test_current_audio_level_uses_level_meter_reading(tmp_path, monkeypatch):
    state = AppState(config_path=tmp_path / "config.json")
    sentinel = object()
    monkeypatch.setattr(state.level_meter, "reading", lambda: sentinel)

    assert state.current_audio_level() is sentinel


def test_sync_dsnoop_config_does_nothing_without_device(tmp_path, monkeypatch):
    from app import audio as audio_module

    state = AppState(config_path=tmp_path / "config.json")
    write_calls = []
    monkeypatch.setattr(audio_module, "write_dsnoop_config", lambda *a, **kw: write_calls.append((a, kw)))

    state.sync_dsnoop_config()

    assert write_calls == []


def test_sync_dsnoop_config_writes_config_for_matching_device(tmp_path, monkeypatch):
    from app import audio as audio_module
    from app.audio import AudioDevice

    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "plughw:1,0"
    state.config.channels = 2
    state.config.sample_rate = 48000
    monkeypatch.setattr(
        state,
        "refresh_audio_devices",
        lambda: [AudioDevice(hw_id="plughw:1,0", card_id="Scarlett", card_name="Focusrite Scarlett 2i2", device_name="USB Audio")],
    )
    write_calls = []
    monkeypatch.setattr(audio_module, "write_dsnoop_config", lambda *a, **kw: write_calls.append((a, kw)))

    state.sync_dsnoop_config()

    assert write_calls == [(("Scarlett", 2, 48000), {})]


def test_sync_dsnoop_config_skips_write_when_device_not_detected(tmp_path, monkeypatch):
    from app import audio as audio_module

    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "plughw:1,0"
    monkeypatch.setattr(state, "refresh_audio_devices", lambda: [])
    write_calls = []
    monkeypatch.setattr(audio_module, "write_dsnoop_config", lambda *a, **kw: write_calls.append((a, kw)))

    state.sync_dsnoop_config()

    assert write_calls == []
