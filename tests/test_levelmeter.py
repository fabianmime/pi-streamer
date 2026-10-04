import subprocess
import sys
import time

from app.levelmeter import LevelMeter, LevelTap, build_level_filter, parse_channel_peak_line


def test_parse_channel_peak_line_extracts_channel_and_value():
    assert parse_channel_peak_line("lavfi.astats.1.Peak_level=-12.345") == (1, -12.345)
    assert parse_channel_peak_line("lavfi.astats.2.Peak_level=-6.0") == (2, -6.0)


def test_parse_channel_peak_line_treats_inf_as_silence_but_still_recognized():
    # -inf/inf/NaN heisst "Stille", die Zeile gehoert aber trotzdem zu einer
    # erkannten Pegel-Messung (Rueckgabe ist nicht None) - unterscheidet sich
    # von einer komplett unbekannten Zeile.
    channel, value = parse_channel_peak_line("lavfi.astats.1.Peak_level=-inf")
    assert channel == 1
    assert value is None


def test_parse_channel_peak_line_ignores_other_lines():
    assert parse_channel_peak_line("frame:0    pts:0       pts_time:0") is None
    assert parse_channel_peak_line("lavfi.astats.Overall.Peak_level=-12.345") is None


def test_build_level_filter_contains_expected_pieces():
    filt = build_level_filter()
    assert "astats" in filt
    assert "ametadata=print" in filt
    # Pro-Kanal-Peak, keine Overall-Kennzahl und keine der anderen ~15
    # astats-Kennzahlen - haelt die Metadaten-Ausgabe schlank (siehe
    # build_level_filter-Docstring).
    assert "measure_perchannel=Peak_level" in filt
    assert "measure_overall=none" in filt
    # Ohne direct=1 puffert ametadata=print seine Ausgabe intern, bis der
    # Prozess beendet wird - beim Lesen ueber eine Pipe kommt dann nie
    # etwas in Echtzeit an (live auf dem Pi verifiziert, siehe
    # levelmeter.py-Docstring von build_level_filter).
    assert "direct=1" in filt


def _fake_process(lines: list[str]) -> subprocess.Popen:
    script = "import sys, time\n" + "\n".join(
        f'print({line!r}); sys.stdout.flush()' for line in lines
    ) + "\ntime.sleep(5)\n"
    return subprocess.Popen(
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
    )


def test_level_tap_reads_latest_peak_value_mono():
    process = _fake_process(
        [
            "lavfi.astats.1.Peak_level=-40.0",
            "lavfi.astats.1.Peak_level=-5.5",
        ]
    )
    tap = LevelTap()
    try:
        tap.attach(process)
        deadline = time.monotonic() + 3
        reading = tap.reading()
        while reading.channel_peaks_db != [-5.5] and time.monotonic() < deadline:
            time.sleep(0.05)
            reading = tap.reading()

        assert reading.available is True
        assert reading.channel_peaks_db == [-5.5]
    finally:
        process.kill()
        process.wait(timeout=3)


def test_level_tap_reads_latest_peak_value_stereo():
    process = _fake_process(
        [
            "lavfi.astats.1.Peak_level=-10.0",
            "lavfi.astats.2.Peak_level=-20.0",
        ]
    )
    tap = LevelTap()
    try:
        tap.attach(process)
        deadline = time.monotonic() + 3
        reading = tap.reading()
        while reading.channel_peaks_db != [-10.0, -20.0] and time.monotonic() < deadline:
            time.sleep(0.05)
            reading = tap.reading()

        assert reading.available is True
        assert reading.channel_peaks_db == [-10.0, -20.0]
    finally:
        process.kill()
        process.wait(timeout=3)


def test_level_tap_reports_unavailable_when_never_updated():
    tap = LevelTap()
    reading = tap.reading()
    assert reading.available is False
    assert reading.channel_peaks_db == []


def test_level_meter_start_stop_lifecycle(monkeypatch):
    meter = LevelMeter(ffmpeg_bin=sys.executable)
    real_popen = subprocess.Popen

    def fake_popen(cmd, **kwargs):
        script = (
            "import sys, time\n"
            "print('lavfi.astats.1.Peak_level=-20.0'); sys.stdout.flush()\n"
            "time.sleep(5)\n"
        )
        return real_popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    meter.start("hw:1,0", channels=2, sample_rate=48000)
    try:
        assert meter.is_running_for("hw:1,0") is True
        assert meter.is_running_for("hw:0,0") is False

        deadline = time.monotonic() + 3
        reading = meter.reading()
        while not reading.available and time.monotonic() < deadline:
            time.sleep(0.05)
            reading = meter.reading()
        assert reading.available is True
        assert reading.channel_peaks_db == [-20.0]
    finally:
        meter.stop()

    assert meter.is_running_for("hw:1,0") is False


def test_level_meter_start_without_device_is_noop():
    meter = LevelMeter()
    meter.start("", channels=2, sample_rate=48000)
    assert meter.is_running_for("") is False


def test_level_meter_stop_when_not_running_is_noop():
    meter = LevelMeter()
    meter.stop()  # darf nicht crashen
