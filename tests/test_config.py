import pytest

from app.config import StreamerConfig, load_config, save_config


def test_defaults_are_not_ready_to_stream():
    cfg = StreamerConfig()
    assert cfg.is_ready_to_stream() is False


def test_ready_to_stream_needs_srt_url_and_device():
    cfg = StreamerConfig(srt_url="srt://host:1234", audio_device="hw:1,0")
    assert cfg.is_ready_to_stream() is True

    assert StreamerConfig(srt_url="srt://host:1234").is_ready_to_stream() is False
    assert StreamerConfig(audio_device="hw:1,0").is_ready_to_stream() is False


def test_defaults_include_port_80():
    assert StreamerConfig().web_port == 80


def test_defaults_are_not_ready_to_record():
    assert StreamerConfig().is_ready_to_record() is False


def test_ready_to_record_needs_target():
    assert StreamerConfig(recording_target="/media/livevoice/STICK").is_ready_to_record() is True


def test_default_recording_filename_prefix_is_aufnahme():
    assert StreamerConfig().recording_filename_prefix == "Aufnahme"


@pytest.mark.parametrize("prefix", ["", "a/b", "a\\b", "x" * 41])
def test_validated_rejects_invalid_recording_filename_prefix(prefix):
    cfg = StreamerConfig(recording_filename_prefix=prefix)
    with pytest.raises(ValueError):
        cfg.validated()


@pytest.mark.parametrize("prefix", ["Aufnahme", "Konzert 2026", "x" * 40])
def test_validated_accepts_valid_recording_filename_prefix(prefix):
    cfg = StreamerConfig(recording_filename_prefix=prefix)
    assert cfg.validated().recording_filename_prefix == prefix


@pytest.mark.parametrize(
    "field, value",
    [
        ("channels", 3),
        ("sample_rate", 12345),
        ("bitrate_kbps", 999),
        ("web_port", 0),
        ("web_port", 65536),
        ("web_port", 443),
        ("web_port", 1023),
        ("log_level", "TRACE"),
    ],
)
def test_validated_rejects_invalid_values(field, value):
    cfg = StreamerConfig(**{field: value})
    with pytest.raises(ValueError):
        cfg.validated()


@pytest.mark.parametrize("port", [80, 1024, 8080, 65535])
def test_validated_accepts_80_or_unprivileged_ports(port):
    cfg = StreamerConfig(web_port=port)
    assert cfg.validated().web_port == port


def test_to_public_dict_hides_password():
    cfg = StreamerConfig(web_password="geheim")
    public = cfg.to_public_dict()
    assert "web_password" not in public
    assert public["web_password_set"] is True


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = StreamerConfig(
        srt_url="srt://example:1234?passphrase=x",
        operator_url="https://livevoice.io/de/events/operator/abc",
        audio_device="hw:1,0",
        channels=1,
        sample_rate=44100,
        bitrate_kbps=128,
        web_password="secret",
        web_port=8080,
        hide_operator=True,
        log_level="DEBUG",
        recording_target="/media/livevoice/STICK",
    )

    save_config(cfg, path)
    loaded = load_config(path)

    assert loaded == cfg


def test_load_creates_default_when_missing(tmp_path):
    path = tmp_path / "does-not-exist" / "config.json"
    loaded = load_config(path)

    assert loaded == StreamerConfig()
    assert path.exists()


def test_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"srt_url": "srt://x", "some_future_field": 42}', encoding="utf-8")

    loaded = load_config(path)

    assert loaded.srt_url == "srt://x"
