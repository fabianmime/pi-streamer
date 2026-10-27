"""Tests für die Produktiv-Erweiterungen vom 27.10.2026: Aufnahmeformate,
robuste Config, Gerätename, Kartenerkennung per ALSA-Kennung, Stream nach
Neustart fortsetzen."""

import sys
from datetime import datetime

import pytest

from app import AppState
from app.audio import AudioDevice
from app.config import StreamerConfig, load_config, save_config
from app.recorder import RecordingManager, build_ffmpeg_record_cmd, recording_filename
from app.streamer import StreamManager
from app.system import estimate_recording_seconds, set_hostname, validate_hostname


def _sleep_record_cmd(_cfg, _output_path):
    return [sys.executable, "-c", "import time; time.sleep(5)"]


# --- Aufnahmeformate ------------------------------------------------------------


def test_recording_filename_uses_format_extension():
    name = recording_filename("Konzert", datetime(2026, 9, 9, 20, 15, 30), extension="flac")
    assert name == "Konzert-20260909-201530.flac"


def test_wav_recording_allows_files_over_4gb():
    cmd = build_ffmpeg_record_cmd(StreamerConfig(recording_format="wav"), "/media/stick/a.wav")
    assert cmd[cmd.index("-rf64") + 1] == "auto"


@pytest.mark.parametrize(
    "fmt, codec, container",
    [("wav", "pcm_s16le", "wav"), ("flac", "flac", "flac"), ("mka", "pcm_s16le", "matroska")],
)
def test_record_cmd_uses_selected_format(fmt, codec, container):
    cmd = build_ffmpeg_record_cmd(StreamerConfig(recording_format=fmt), "/media/stick/x")
    codec_index = cmd.index("-c:a")
    assert cmd[codec_index + 1] == codec
    assert cmd[cmd.index("-f", codec_index) + 1] == container


def test_recording_manager_names_file_by_format(tmp_path):
    manager = RecordingManager(cmd_builder=_sleep_record_cmd)
    cfg = StreamerConfig(recording_format="flac", recording_filename_prefix="Test")

    status = manager.start(cfg, str(tmp_path))
    try:
        assert status.output_path.endswith(".flac")
        assert "Test-" in status.output_path
    finally:
        manager.stop()


def test_estimate_recording_seconds_respects_format_size_factor():
    wav = estimate_recording_seconds(192000 * 60, channels=2, sample_rate=48000)
    flac = estimate_recording_seconds(192000 * 60, channels=2, sample_rate=48000, size_factor=0.5)
    assert wav == 60.0
    assert flac == 120.0


# --- Config -------------------------------------------------------------------


def test_to_public_dict_hides_admin_password():
    public = StreamerConfig(admin_password="chef").to_public_dict()
    assert "admin_password" not in public
    assert public["admin_password_set"] is True


@pytest.mark.parametrize("fmt", ["wav", "flac", "mka"])
def test_validated_accepts_recording_formats(fmt):
    assert StreamerConfig(recording_format=fmt).validated().recording_format == fmt


def test_validated_rejects_unknown_recording_format():
    with pytest.raises(ValueError):
        StreamerConfig(recording_format="mp3").validated()


@pytest.mark.parametrize(
    "url",
    ["https://github.com/fabianmime/pi-streamer.git", "git@github.com:fabianmime/pi-streamer.git", ""],
)
def test_validated_accepts_update_sources(url):
    assert StreamerConfig(update_repo_url=url).validated().update_repo_url == url


@pytest.mark.parametrize(
    "url", ["file:///etc", "ext::sh -c touch% /tmp/x", "-uhttps://x", "http://github.com/x", "https://x y"]
)
def test_validated_rejects_unsafe_update_sources(url):
    with pytest.raises(ValueError):
        StreamerConfig(update_repo_url=url).validated()


@pytest.mark.parametrize("branch", ["", "-x", "main..x", "a b", "--upload-pack=x"])
def test_validated_rejects_unsafe_update_branch(branch):
    with pytest.raises(ValueError):
        StreamerConfig(update_branch=branch).validated()


def test_defaults_for_new_features():
    cfg = StreamerConfig()
    assert cfg.update_repo_url == "https://github.com/fabianmime/pi-streamer.git"
    assert cfg.update_branch == "main"
    assert cfg.update_auto is False
    assert cfg.stream_auto_reconnect is True
    assert cfg.hide_recording is False
    assert cfg.recording_format == "wav"


def test_save_keeps_backup_of_previous_config(tmp_path):
    path = tmp_path / "config.json"
    save_config(StreamerConfig(srt_url="srt://alt"), path)
    save_config(StreamerConfig(srt_url="srt://neu"), path)

    assert load_config(tmp_path / "config.json.bak").srt_url == "srt://alt"


def test_load_falls_back_to_backup_when_config_is_corrupt(tmp_path):
    # Typisches Bild nach Stromausfall direkt nach dem Speichern: leere Datei.
    path = tmp_path / "config.json"
    save_config(StreamerConfig(srt_url="srt://alt"), path)
    save_config(StreamerConfig(srt_url="srt://neu"), path)
    path.write_text("", encoding="utf-8")

    assert load_config(path).srt_url == "srt://alt"


def test_load_uses_defaults_when_config_and_backup_are_corrupt(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{kaputt", encoding="utf-8")

    assert load_config(path) == StreamerConfig()


def test_corrupt_config_is_not_copied_over_good_backup(tmp_path):
    path = tmp_path / "config.json"
    save_config(StreamerConfig(srt_url="srt://gut"), path)
    save_config(StreamerConfig(srt_url="srt://auch-gut"), path)
    path.write_text("", encoding="utf-8")

    save_config(StreamerConfig(srt_url="srt://neu"), path)

    assert load_config(tmp_path / "config.json.bak").srt_url == "srt://gut"


# --- Gerätename -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["livevoice", "LiveVoice-Pi-Streamer", "pi2", "a"])
def test_validate_hostname_accepts_valid_names(name):
    assert validate_hostname(name) is None


@pytest.mark.parametrize("name", ["", "-pi", "pi-", "pi streamer", "pi.local", "ä-pi", "x" * 64, "pi;reboot"])
def test_validate_hostname_rejects_invalid_names(name):
    assert validate_hostname(name) is not None


def test_set_hostname_rejects_invalid_name_without_calling_hostnamectl(monkeypatch):
    import subprocess

    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: calls.append(a))

    ok, error = set_hostname("pi streamer")

    assert ok is False
    assert error
    assert calls == []


def test_set_hostname_calls_hostnamectl(monkeypatch):
    import subprocess

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert set_hostname("buehne-links") == (True, None)
    assert calls == [["hostnamectl", "set-hostname", "buehne-links"]]


# --- Audio-Gerät über stabile ALSA-Kennung ----------------------------------------


def test_sync_dsnoop_follows_card_when_its_number_changed(tmp_path, monkeypatch):
    from app import audio as audio_module

    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "plughw:1,0"
    state.config.audio_card_id = "Scarlett"
    # Nach einem Neustart mit zusätzlichem USB-Gerät hängt das Headset an Karte 2,
    # an Karte 1 steckt jetzt etwas anderes.
    monkeypatch.setattr(
        state,
        "refresh_audio_devices",
        lambda: [
            AudioDevice(hw_id="plughw:1,0", card_id="Other", card_name="Anderes Gerät", device_name="USB Audio"),
            AudioDevice(hw_id="plughw:2,0", card_id="Scarlett", card_name="Focusrite Scarlett 2i2", device_name="USB Audio"),
        ],
    )
    writes = []
    monkeypatch.setattr(audio_module, "write_dsnoop_config", lambda *a, **kw: writes.append(a))

    state.sync_dsnoop_config()

    assert writes == [("Scarlett", state.config.channels, state.config.sample_rate)]
    assert state.config.audio_device == "plughw:2,0"
    assert load_config(tmp_path / "config.json").audio_device == "plughw:2,0"


def test_sync_dsnoop_remembers_card_id_of_selected_device(tmp_path, monkeypatch):
    from app import audio as audio_module

    state = AppState(config_path=tmp_path / "config.json")
    state.config.audio_device = "plughw:1,0"
    monkeypatch.setattr(
        state,
        "refresh_audio_devices",
        lambda: [AudioDevice(hw_id="plughw:1,0", card_id="Scarlett", card_name="Focusrite Scarlett 2i2", device_name="USB Audio")],
    )
    monkeypatch.setattr(audio_module, "write_dsnoop_config", lambda *a, **kw: None)

    state.sync_dsnoop_config()

    assert state.config.audio_card_id == "Scarlett"


# --- Stream nach Stromausfall fortsetzen ----------------------------------------


def _sleep_stream_cmd(_cfg):
    return [sys.executable, "-c", "import time; time.sleep(5)"]


def _ready_state(tmp_path) -> AppState:
    state = AppState(config_path=tmp_path / "config.json")
    state.config.srt_url = "srt://x"
    state.config.audio_device = "hw:1,0"
    state.stream_manager = StreamManager(
        cmd_builder=_sleep_stream_cmd, on_desired_change=state._remember_stream_wanted
    )
    return state


def test_stream_wanted_is_remembered_across_restarts(tmp_path):
    state = _ready_state(tmp_path)
    state.stream_manager.start(state.config)
    state.stream_manager.stop()
    assert state.stream_was_wanted() is False

    state.stream_manager.start(state.config)
    try:
        assert AppState(config_path=tmp_path / "config.json").stream_was_wanted() is True
    finally:
        state.stream_manager.stop()


def test_resume_stream_if_wanted_starts_stream(tmp_path):
    state = _ready_state(tmp_path)
    state._remember_stream_wanted(True)

    try:
        assert state.resume_stream_if_wanted() is True
        assert state.stream_manager.status().running is True
    finally:
        state.stream_manager.stop()


def test_resume_stream_skipped_when_stopped_deliberately(tmp_path):
    state = _ready_state(tmp_path)
    state._remember_stream_wanted(False)

    assert state.resume_stream_if_wanted() is False
    assert state.stream_manager.status().running is False


def test_resume_stream_skipped_without_auto_reconnect(tmp_path):
    state = _ready_state(tmp_path)
    state.config.stream_auto_reconnect = False
    state._remember_stream_wanted(True)

    assert state.resume_stream_if_wanted() is False


def test_is_busy_reflects_stream_and_recording(tmp_path):
    state = _ready_state(tmp_path)
    assert state.is_busy() is False
    state.stream_manager.start(state.config)
    try:
        assert state.is_busy() is True
    finally:
        state.stream_manager.stop()
