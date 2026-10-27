"""Demo-Server mit Beispieldaten - für Screenshots und zum Ausprobieren des
WebUI ohne Audio-Interface, USB-Stick oder LiveVoice-Event.

Startet die echte App, ersetzt aber alle Hardware-/System-Zugriffe
(Audio-Geräte, USB-Sticks, NetworkManager, Hostname, ffmpeg) durch feste
Beispielwerte. Es wird nichts gestreamt, aufgenommen oder am System
geändert; die Konfiguration landet in einem temporären Verzeichnis.

    python tools/demo_server.py [--port 8099] [--stream running|reconnecting|stopped]
                                [--admin-password demo]

Danach http://<host>:8099/ öffnen.
"""

from __future__ import annotations

import argparse
import math
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import waitress  # noqa: E402

from app import audio, create_app, storage, system, wifi  # noqa: E402
from app.audio import AudioDevice  # noqa: E402
from app.config import StreamerConfig, save_config  # noqa: E402
from app.levelmeter import LevelReading  # noqa: E402
from app.recorder import RecordingStatus  # noqa: E402
from app.storage import UsbDrive  # noqa: E402
from app.streamer import StreamStatus  # noqa: E402

DEMO_MOUNT = "/media/rec/AUFNAHMEN"
_T0 = time.monotonic()


def _patch_system() -> None:
    audio.list_capture_devices = lambda timeout=5.0: [
        AudioDevice(hw_id="plughw:1,0", card_id="USB", card_name="Focusrite Scarlett 2i2", device_name="USB Audio"),
    ]
    audio.write_dsnoop_config = lambda *a, **kw: None
    storage.list_usb_drives = lambda timeout=5.0: [
        UsbDrive(device="/dev/sda1", label="AUFNAHMEN", size_bytes=64_000_000_000, mountpoint=DEMO_MOUNT)
    ]
    storage.free_space_bytes = lambda path: 41_300_000_000
    wifi.get_status = lambda: wifi.NetworkStatus(
        available=True,
        active_connections=[
            wifi.ActiveConnection(
                device="eth0",
                type="ethernet",
                name="Kabelverbindung",
                ipv4=wifi.IPv4Details(
                    method="auto",
                    address="192.168.1.50",
                    prefix=24,
                    gateway="192.168.1.1",
                    dns=["192.168.1.1"],
                    dhcp_server="192.168.1.1",
                ),
            )
        ],
        ip_addresses=["192.168.1.50"],
    )
    wifi.list_saved_connections = lambda: [
        wifi.SavedConnection(name="Event-WLAN", display_name="Event-WLAN", active=False),
        wifi.SavedConnection(name="Buehne-5G", display_name="Bühne-5G", active=False),
    ]
    wifi.scan_networks = lambda: [
        wifi.WifiNetwork(ssid="Event-WLAN", signal=82, secured=True),
        wifi.WifiNetwork(ssid="Gast", signal=54, secured=False),
    ]
    system.current_hostname = lambda: "streamer"


def _fake_levels() -> LevelReading:
    t = time.monotonic() - _T0
    left = -16 + 6 * math.sin(t * 2.3) + 2 * math.sin(t * 7.1)
    right = -18 + 6 * math.sin(t * 2.0 + 1) + 2 * math.sin(t * 6.3)
    return LevelReading(channel_peaks_db=[round(left, 1), round(right, 1)], available=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--stream", choices=("running", "reconnecting", "stopped"), default="running")
    parser.add_argument("--admin-password", default="")
    args = parser.parse_args()

    _patch_system()

    config_path = Path(tempfile.mkdtemp(prefix="lv-demo-")) / "config.json"
    save_config(
        StreamerConfig(
            srt_url="srt://ingest.example.com:9000?streamid=demo&passphrase=demo",
            operator_url="https://livevoice.io/de/events/operator/demo",
            audio_device="plughw:1,0",
            audio_card_id="USB",
            recording_target=DEMO_MOUNT,
            recording_format="flac",
            recording_filename_prefix="Gottesdienst",
            admin_password=args.admin_password,
            web_port=args.port,
        ),
        config_path,
    )

    app = create_app(config_path=config_path)
    state = app.state

    def stream_status() -> StreamStatus:
        uptime = time.monotonic() - _T0 + 3725
        if args.stream == "running":
            return StreamStatus(True, 4242, _T0, uptime, None, None, reconnects_total=1)
        if args.stream == "reconnecting":
            return StreamStatus(
                False,
                None,
                None,
                0.0,
                251,
                "Stream wurde von LiveVoice-Seite beendet (Import dort gestoppt oder Verbindung unterbrochen).",
                reconnecting=True,
                reconnect_attempt=3,
                reconnect_in_seconds=max(10.0 - (time.monotonic() - _T0) % 10.0, 0.5),
                reconnects_total=2,
            )
        return StreamStatus(False, None, None, 0.0, None, None)

    recording_running = args.stream == "running"

    def recording_status() -> RecordingStatus:
        if not recording_running:
            return RecordingStatus(False, None, None, 0.0, None, None, None)
        return RecordingStatus(
            True,
            4343,
            _T0,
            time.monotonic() - _T0 + 3702,
            None,
            None,
            f"{DEMO_MOUNT}/Gottesdienst-20261027-093012.flac",
        )

    state.stream_manager.status = stream_status
    state.stream_manager.start = lambda cfg: stream_status()
    state.stream_manager.stop = lambda timeout=5.0: stream_status()
    state.recording_manager.status = recording_status
    state.ensure_level_meter = lambda: None
    state.current_audio_level = _fake_levels

    upd = state.updater._status
    upd.current_summary = "a1b2c3d vom 27.10.2026: Auto-Reconnect und Update-Funktion"
    upd.last_check = "27.10.2026 09:00"
    upd.update_available = True
    upd.pending_commits = ["e4f5a6b Pegelanzeige: Peak-Hold", "c7d8e9f Kleinere Korrekturen"]
    upd.message = "Update verfügbar (2 Änderung(en))."
    state.updater.start_async = lambda action: None

    print(f"Demo läuft auf http://0.0.0.0:{args.port}/ (Stream: {args.stream})", flush=True)
    waitress.serve(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
