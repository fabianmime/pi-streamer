from app.audio import (
    AudioDevice,
    build_dsnoop_config_text,
    list_capture_devices,
    parse_arecord_output,
    write_dsnoop_config,
)

SAMPLE_ARECORD_OUTPUT = """\
**** Liste der Hardware-Geräte (CAPTURE) ****
card 0: Headphones [bcm2835 Headphones], device 0: bcm2835 Headphones [bcm2835 Headphones]
  Untergeordnete Geräte: 1/1
  Untergeordnetes Gerät #0: subdevice #0
card 1: U0x46d0x81b [USB Device 0x46d:0x81b], device 0: USB Audio [USB Audio]
  Untergeordnete Geräte: 1/1
  Untergeordnetes Gerät #0: subdevice #0
"""


def test_parse_arecord_output_extracts_devices():
    devices = parse_arecord_output(SAMPLE_ARECORD_OUTPUT)

    assert devices == [
        AudioDevice(
            hw_id="plughw:0,0",
            card_id="Headphones",
            card_name="bcm2835 Headphones",
            device_name="bcm2835 Headphones",
        ),
        AudioDevice(
            hw_id="plughw:1,0",
            card_id="U0x46d0x81b",
            card_name="USB Device 0x46d:0x81b",
            device_name="USB Audio",
        ),
    ]


def test_parse_arecord_output_empty_string():
    assert parse_arecord_output("") == []


def test_parse_arecord_output_no_devices_message():
    assert parse_arecord_output("keine Soundkarten gefunden ...\n") == []


def test_audio_device_label():
    dev = AudioDevice(hw_id="plughw:1,0", card_id="Audio", card_name="Scarlett 2i2", device_name="USB Audio")
    assert dev.label == "Scarlett 2i2 - USB Audio (plughw:1,0)"


def test_list_capture_devices_returns_empty_when_arecord_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("arecord not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert list_capture_devices() == []


def test_build_dsnoop_config_text_uses_card_id_not_card_name():
    # Das "hw:CARD=..." braucht die ALSA-Kurzkennung (card_id), nicht den
    # beschreibenden Namen in Klammern (card_name) - siehe DEPLOY.md-Beispiel
    # "card 1: Audio [Scarlett 2i2 USB]" -> Kennung "Audio", nicht
    # "Scarlett 2i2 USB".
    text = build_dsnoop_config_text("WH62", channels=2, sample_rate=48000)

    assert 'pcm "hw:CARD=WH62,DEV=0"' in text
    assert "channels 2" in text
    assert "rate 48000" in text
    assert "pcm.livevoice_dsnoop" in text
    assert "pcm.livevoice_capture" in text


def test_write_dsnoop_config_writes_asoundrc(tmp_path):
    target = tmp_path / ".asoundrc"

    write_dsnoop_config("WH62", channels=2, sample_rate=48000, path=target)

    assert target.exists()
    assert 'pcm "hw:CARD=WH62,DEV=0"' in target.read_text(encoding="utf-8")


def test_write_dsnoop_config_overwrites_existing_file(tmp_path):
    target = tmp_path / ".asoundrc"
    target.write_text("# alte, veraltete Config\n", encoding="utf-8")

    write_dsnoop_config("NeuesInterface", channels=1, sample_rate=44100, path=target)

    content = target.read_text(encoding="utf-8")
    assert "NeuesInterface" in content
    assert "alte, veraltete Config" not in content
