from app.system import (
    cpu_temperature_celsius,
    disk_usage,
    estimate_recording_seconds,
    load_average,
    memory_usage,
    poweroff,
    reboot,
)


def test_load_average_parses_proc_loadavg(tmp_path):
    path = tmp_path / "loadavg"
    path.write_text("0.42 0.30 0.18 2/456 12345\n", encoding="utf-8")

    assert load_average(path) == (0.42, 0.30, 0.18)


def test_load_average_missing_file_returns_none(tmp_path):
    assert load_average(tmp_path / "missing") is None


def test_load_average_malformed_content_returns_none(tmp_path):
    path = tmp_path / "loadavg"
    path.write_text("garbage\n", encoding="utf-8")

    assert load_average(path) is None


def test_memory_usage_parses_meminfo_and_uses_memavailable(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text(
        "MemTotal:        8000000 kB\n"
        "MemFree:          500000 kB\n"
        "MemAvailable:    6000000 kB\n"
        "Buffers:          100000 kB\n",
        encoding="utf-8",
    )

    info = memory_usage(path)

    assert info is not None
    assert info.total_bytes == 8000000 * 1024
    assert info.available_bytes == 6000000 * 1024
    assert info.used_bytes == (8000000 - 6000000) * 1024
    assert 24.9 < info.used_percent < 25.1


def test_memory_usage_missing_fields_returns_none(tmp_path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 8000000 kB\n", encoding="utf-8")

    assert memory_usage(path) is None


def test_memory_usage_missing_file_returns_none(tmp_path):
    assert memory_usage(tmp_path / "missing") is None


def test_disk_usage_reports_root_filesystem(tmp_path):
    info = disk_usage(str(tmp_path))

    assert info is not None
    assert info.total_bytes >= info.used_bytes + info.free_bytes - 1
    assert 0 <= info.used_percent <= 100


def test_disk_usage_missing_path_returns_none():
    assert disk_usage("/pfad/der/sicher/nicht/existiert") is None


def test_cpu_temperature_parses_millidegrees(tmp_path):
    path = tmp_path / "temp"
    path.write_text("42800\n", encoding="utf-8")

    assert cpu_temperature_celsius(path) == 42.8


def test_cpu_temperature_missing_file_returns_none(tmp_path):
    assert cpu_temperature_celsius(tmp_path / "missing") is None


def test_estimate_recording_seconds_matches_wav_bitrate():
    # Stereo, 48 kHz, 16 Bit (S16_LE) -> 192000 Byte/s.
    seconds = estimate_recording_seconds(free_bytes=192000 * 60, channels=2, sample_rate=48000)

    assert seconds == 60.0


def test_estimate_recording_seconds_zero_free_space():
    assert estimate_recording_seconds(free_bytes=0, channels=2, sample_rate=48000) == 0.0


def test_poweroff_uses_systemctl(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append(a[0]) or FakeResult())

    ok, error = poweroff()

    assert ok is True
    assert error is None
    assert calls == [["systemctl", "poweroff"]]


def test_reboot_uses_systemctl(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append(a[0]) or FakeResult())

    ok, error = reboot()

    assert ok is True
    assert calls == [["systemctl", "reboot"]]


def test_poweroff_reports_backend_error(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "Interactive authentication required."

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())

    ok, error = poweroff()

    assert ok is False
    assert "Interactive authentication" in error


def test_poweroff_handles_missing_systemctl(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("systemctl not found")

    monkeypatch.setattr("subprocess.run", fake_run)

    ok, error = poweroff()

    assert ok is False
    assert error is not None
