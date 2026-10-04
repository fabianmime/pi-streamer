import sys

import pytest

from app import create_app
from app.recorder import RecordingManager
from app.streamer import StreamManager


def _sleep_cmd_builder(_cfg):
    return [sys.executable, "-c", "import time; time.sleep(2)"]


def _sleep_record_cmd_builder(_cfg, _output_path):
    return [sys.executable, "-c", "import time; time.sleep(2)"]


@pytest.fixture
def app(tmp_path):
    config_path = tmp_path / "config.json"
    flask_app = create_app(config_path=config_path)
    # Kein echtes ffmpeg im Testlauf nötig - Dummy-Prozess statt ffmpeg-Aufruf.
    flask_app.state.stream_manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    flask_app.state.recording_manager = RecordingManager(cmd_builder=_sleep_record_cmd_builder)
    flask_app.config.update(TESTING=True)
    yield flask_app
    flask_app.state.stream_manager.stop()
    flask_app.state.recording_manager.stop()


@pytest.fixture
def client(app):
    return app.test_client()


def test_index_without_operator_url_shows_hint(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Noch keine Operator-URL".encode("utf-8") in res.data


def test_index_shows_single_level_meter_row_for_mono(client, app):
    app.state.config.channels = 1
    res = client.get("/")
    assert res.data.count(b'class="level-meter-row"') == 1
    assert b">Eingangspegel<" in res.data


def test_index_shows_two_labeled_level_meter_rows_for_stereo(client, app):
    app.state.config.channels = 2
    res = client.get("/")
    assert res.data.count(b'class="level-meter-row"') == 2
    assert b">L<" in res.data
    assert b">R<" in res.data


def test_settings_get_renders_form(client):
    res = client.get("/settings")
    assert res.status_code == 200
    assert b"srt_url" in res.data


def test_settings_post_saves_and_redisplays(client, app):
    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://example.com:9000?streamid=abc",
            "operator_url": "https://livevoice.io/de/events/operator/token",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
        },
    )
    assert res.status_code == 200
    assert app.state.config.srt_url == "srt://example.com:9000?streamid=abc"
    assert app.state.config.operator_url == "https://livevoice.io/de/events/operator/token"


def test_settings_post_saves_recording_filename_prefix(client, app, tmp_path):
    res = client.post(
        "/settings",
        data={
            "srt_url": "",
            "operator_url": "",
            "audio_device": "",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
            "recording_target": str(tmp_path),
            "recording_filename_prefix": "Konzert",
        },
    )
    assert res.status_code == 200
    assert app.state.config.recording_filename_prefix == "Konzert"


def test_settings_post_rejects_invalid_bitrate(client, app):
    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "999",
            "web_password": "",
        },
    )
    assert res.status_code == 400


def test_start_stop_stream_lifecycle(client):
    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://example.com:9000",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
        },
    )
    assert res.status_code == 200

    start_res = client.post("/api/stream/start")
    assert start_res.status_code == 200
    assert start_res.get_json()["running"] is True

    status_res = client.get("/api/status")
    assert status_res.get_json()["running"] is True

    stop_res = client.post("/api/stream/stop")
    assert stop_res.get_json()["running"] is False


def test_start_stream_without_config_fails(client):
    res = client.post("/api/stream/start")
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_audio_level_endpoint_reports_shape(client):
    res = client.get("/api/audio-level")
    assert res.status_code == 200
    data = res.get_json()
    assert "channel_peaks_db" in data
    assert "available" in data


def test_stream_start_does_not_touch_level_meter(client, app, monkeypatch):
    # Pegelmessung laeuft ueber den geteilten dsnoop-Alias unabhaengig vom
    # Stream weiter - stream/start darf sie nicht mehr stoppen.
    stop_calls = []
    monkeypatch.setattr(app.state.level_meter, "stop", lambda: stop_calls.append(True))

    client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
        },
    )
    res = client.post("/api/stream/start")

    assert res.status_code == 200
    assert stop_calls == []


def test_start_stop_recording_lifecycle(client, app, tmp_path):
    res = client.post("/settings", data={"recording_target": str(tmp_path)})
    assert res.status_code == 200
    assert app.state.config.recording_target == str(tmp_path)

    start_res = client.post("/api/recording/start")
    assert start_res.status_code == 200
    assert start_res.get_json()["running"] is True

    status_res = client.get("/api/recording/status")
    assert status_res.get_json()["running"] is True

    stop_res = client.post("/api/recording/stop")
    assert stop_res.get_json()["running"] is False


def test_stream_dismiss_error_clears_last_error(client, app, monkeypatch):
    clear_calls = []
    monkeypatch.setattr(app.state.stream_manager, "clear_error", lambda: clear_calls.append(True))

    res = client.post("/api/stream/dismiss-error")

    assert res.status_code == 200
    assert clear_calls == [True]


def test_recording_dismiss_error_clears_last_error(client, app, monkeypatch):
    clear_calls = []
    monkeypatch.setattr(app.state.recording_manager, "clear_error", lambda: clear_calls.append(True))

    res = client.post("/api/recording/dismiss-error")

    assert res.status_code == 200
    assert clear_calls == [True]


def test_start_recording_without_target_fails(client):
    res = client.post("/api/recording/start")
    assert res.status_code == 400
    assert "error" in res.get_json()


def test_recording_start_does_not_touch_level_meter(client, app, monkeypatch, tmp_path):
    stop_calls = []
    monkeypatch.setattr(app.state.level_meter, "stop", lambda: stop_calls.append(True))

    client.post("/settings", data={"recording_target": str(tmp_path)})
    res = client.post("/api/recording/start")

    assert res.status_code == 200
    assert stop_calls == []


def test_audio_level_endpoint_reports_recording_flag(client):
    res = client.get("/api/audio-level")
    assert res.status_code == 200
    assert "recording" in res.get_json()


def test_usb_drives_endpoint_reports_list(client, monkeypatch):
    from app import storage as storage_module
    from app.storage import UsbDrive

    monkeypatch.setattr(
        storage_module,
        "list_usb_drives",
        lambda: [UsbDrive(device="/dev/sda1", label="STICK", size_bytes=1000, mountpoint="/media/x")],
    )

    res = client.get("/api/usb-drives")
    assert res.status_code == 200
    data = res.get_json()
    assert data[0]["device"] == "/dev/sda1"
    assert data[0]["mounted"] is True


def test_usb_drives_mount_uses_mocked_backend(client, monkeypatch):
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "mount_drive", lambda device: ("/media/livevoice/STICK", None))

    res = client.post("/api/usb-drives/mount", json={"device": "/dev/sda1"})
    assert res.status_code == 200
    assert res.get_json()["mountpoint"] == "/media/livevoice/STICK"


def test_usb_drives_mount_reports_backend_error(client, monkeypatch):
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "mount_drive", lambda device: (None, "Nicht autorisiert."))

    res = client.post("/api/usb-drives/mount", json={"device": "/dev/sda1"})
    assert res.status_code == 400
    assert res.get_json()["error"] == "Nicht autorisiert."


def test_usb_drives_unmount_blocked_while_recording(client, app, tmp_path):
    client.post("/settings", data={"recording_target": str(tmp_path)})
    client.post("/api/recording/start")

    res = client.post("/api/usb-drives/unmount", json={"device": "/dev/sda1"})

    assert res.status_code == 409
    client.post("/api/recording/stop")


def test_usb_drives_format_uses_mocked_backend(client, monkeypatch):
    from app import storage as storage_module

    format_calls = []
    monkeypatch.setattr(
        storage_module, "format_drive", lambda device: format_calls.append(device) or (True, None)
    )

    res = client.post("/api/usb-drives/format", json={"device": "/dev/sda1"})

    assert res.status_code == 200
    assert res.get_json() == {"ok": True}
    assert format_calls == ["/dev/sda1"]


def test_usb_drives_format_reports_backend_error(client, monkeypatch):
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "format_drive", lambda device: (False, "Nicht autorisiert."))

    res = client.post("/api/usb-drives/format", json={"device": "/dev/sda1"})

    assert res.status_code == 400
    assert res.get_json()["error"] == "Nicht autorisiert."


def test_usb_drives_format_requires_device(client):
    res = client.post("/api/usb-drives/format", json={})
    assert res.status_code == 400


def test_usb_drives_format_blocked_while_recording(client, app, tmp_path):
    client.post("/settings", data={"recording_target": str(tmp_path)})
    client.post("/api/recording/start")

    res = client.post("/api/usb-drives/format", json={"device": "/dev/sda1"})

    assert res.status_code == 409
    client.post("/api/recording/stop")


def test_usb_drives_format_clears_stale_recording_target(client, app, monkeypatch, tmp_path):
    from app import storage as storage_module

    monkeypatch.setattr(storage_module, "format_drive", lambda device: (True, None))
    client.post("/settings", data={"recording_target": str(tmp_path)})

    res = client.post("/api/usb-drives/format", json={"device": "/dev/sda1"})

    assert res.status_code == 200
    assert app.state.config.recording_target == ""


def test_settings_toolbar_hides_logout_without_password(client):
    res = client.get("/settings")
    assert b"Jetzt abmelden" not in res.data


def test_settings_toolbar_shows_logout_with_password(client, app):
    app.state.config.web_password = "geheim"
    client.post("/login", data={"password": "geheim"})
    res = client.get("/settings")
    assert b"Jetzt abmelden" in res.data


def test_settings_page_has_expected_sections(client):
    res = client.get("/settings")
    assert res.status_code == 200
    for marker in (
        b"Verbindung",
        b"Audio",
        b"Aufnahme",
        b"recording_filename_prefix",
        b"Netzwerk",
        b"Weitere Einstellungen",
        b"web_password",
        b"Log herunterladen",
        b"hide_operator",
        b"log_level",
        b"password-toggle",
    ):
        assert marker in res.data


def test_settings_post_saves_log_level(client, app):
    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
            "log_level": "DEBUG",
        },
    )
    assert res.status_code == 200
    assert app.state.config.log_level == "DEBUG"


def test_settings_post_rejects_privileged_port_other_than_80(client):
    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
            "web_port": "443",
        },
    )
    assert res.status_code == 400


def test_hide_operator_removes_operator_section_from_index(client, app):
    res = client.post(
        "/settings",
        data={
            "srt_url": "",
            "operator_url": "https://livevoice.io/de/events/operator/token",
            "audio_device": "",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
            "hide_operator": "1",
        },
    )
    assert res.status_code == 200
    assert app.state.config.hide_operator is True

    res = client.get("/")
    assert b"LiveVoice Operator" not in res.data


def test_settings_page_includes_network_section_without_nmcli(client):
    # Der Testrechner hat i.d.R. kein nmcli - die Netzwerk-Sektion auf der
    # Settings-Seite muss trotzdem laden und den "nicht verfügbar"-Zustand
    # sauber anzeigen statt zu crashen.
    res = client.get("/settings")
    assert res.status_code == 200
    assert "Netzwerk".encode("utf-8") in res.data
    assert "web_port".encode("utf-8") in res.data


def test_wifi_page_route_was_removed(client):
    # WLAN ist in die Settings-Seite integriert - keine eigene /wifi-Seite mehr.
    res = client.get("/wifi")
    assert res.status_code == 404


def test_settings_post_with_changed_port_triggers_restart(client, app, monkeypatch):
    from app import routes as routes_module

    restart_calls = []
    monkeypatch.setattr(routes_module, "_schedule_restart", lambda *a, **kw: restart_calls.append(True))

    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
            "web_port": "8080",
        },
    )
    assert res.status_code == 200
    assert app.state.config.web_port == 8080
    assert restart_calls == [True]


def test_settings_post_without_port_change_does_not_restart(client, app, monkeypatch):
    from app import routes as routes_module

    restart_calls = []
    monkeypatch.setattr(routes_module, "_schedule_restart", lambda *a, **kw: restart_calls.append(True))

    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
            "web_port": str(app.state.config.web_port),
        },
    )
    assert res.status_code == 200
    assert restart_calls == []


def test_wifi_status_endpoint_reports_unavailable_gracefully(client):
    res = client.get("/api/wifi/status")
    assert res.status_code == 200
    data = res.get_json()
    assert "available" in data


def test_wifi_connect_uses_mocked_backend(client, monkeypatch):
    from app import wifi

    monkeypatch.setattr(wifi, "connect", lambda ssid, password="": (True, None))

    res = client.post("/api/wifi/connect", json={"ssid": "HomeWifi", "password": "secret"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_wifi_connect_reports_backend_error(client, monkeypatch):
    from app import wifi

    monkeypatch.setattr(wifi, "connect", lambda ssid, password="": (False, "Passwort falsch."))

    res = client.post("/api/wifi/connect", json={"ssid": "HomeWifi", "password": "wrong"})
    assert res.status_code == 400
    assert res.get_json()["error"] == "Passwort falsch."


def test_wifi_forget_uses_mocked_backend(client, monkeypatch):
    from app import wifi

    monkeypatch.setattr(wifi, "forget", lambda name: (True, None))

    res = client.post("/api/wifi/forget", json={"name": "HomeWifi"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_download_logs_returns_attachment(client, monkeypatch):
    from app import logs as logs_module

    monkeypatch.setattr(logs_module, "get_recent_log_text", lambda: "Beispiel-Log-Zeile")

    res = client.get("/api/logs/download")
    assert res.status_code == 200
    assert res.data == b"Beispiel-Log-Zeile"
    assert "attachment" in res.headers["Content-Disposition"]


def test_web_password_redirects_to_login_page(client, app):
    app.state.config.web_password = "geheim"

    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]

    res = client.get("/api/status")
    assert res.status_code == 401


def test_login_page_renders_when_password_set(client, app):
    app.state.config.web_password = "geheim"
    res = client.get("/login")
    assert res.status_code == 200
    assert b"password-toggle" in res.data


def test_login_page_redirects_to_index_without_password_set(client):
    res = client.get("/login", follow_redirects=False)
    assert res.status_code == 302


def test_login_with_correct_password_grants_access(client, app):
    app.state.config.web_password = "geheim"

    res = client.post("/login", data={"password": "geheim", "next": ""}, follow_redirects=False)
    assert res.status_code == 302

    res = client.get("/")
    assert res.status_code == 200


def test_login_with_wrong_password_shows_error(client, app):
    app.state.config.web_password = "geheim"

    res = client.post("/login", data={"password": "falsch"})
    assert res.status_code == 401
    assert "Falsches Passwort".encode("utf-8") in res.data

    res = client.get("/api/status")
    assert res.status_code == 401


def test_logout_clears_session(client, app):
    app.state.config.web_password = "geheim"
    client.post("/login", data={"password": "geheim"})
    assert client.get("/").status_code == 200

    client.get("/logout")
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 302


def test_network_set_dhcp_uses_mocked_backend(client, monkeypatch):
    from app import wifi

    monkeypatch.setattr(wifi, "set_ipv4_dhcp", lambda connection: (True, None))

    res = client.post("/api/network/set-dhcp", json={"connection": "Wired connection 1"})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_network_set_static_uses_mocked_backend(client, monkeypatch):
    from app import wifi

    monkeypatch.setattr(
        wifi,
        "set_ipv4_static",
        lambda connection, address, prefix, gateway, dns: (True, None),
    )

    res = client.post(
        "/api/network/set-static",
        json={
            "connection": "Wired connection 1",
            "address": "192.168.1.50",
            "prefix": 24,
            "gateway": "192.168.1.1",
            "dns": ["1.1.1.1", "8.8.8.8"],
        },
    )
    assert res.status_code == 200
    assert res.get_json()["ok"] is True


def test_recording_status_reports_storage_estimate(client, app, tmp_path, monkeypatch):
    from app import routes as routes_module

    client.post("/settings", data={"recording_target": str(tmp_path)})
    monkeypatch.setattr(routes_module.storage, "free_space_bytes", lambda path: 192000 * 60)

    res = client.get("/api/recording/status")

    assert res.status_code == 200
    data = res.get_json()
    assert data["target_free_bytes"] == 192000 * 60
    assert data["target_estimated_seconds"] == 60.0


def test_recording_status_without_target_has_no_storage_estimate(client):
    res = client.get("/api/recording/status")
    data = res.get_json()
    assert data["target_free_bytes"] is None
    assert data["target_estimated_seconds"] is None


def test_system_stats_endpoint_reports_shape(client):
    res = client.get("/api/system/stats")
    assert res.status_code == 200
    data = res.get_json()
    for key in ("load_average", "memory", "disk", "cpu_temp_celsius"):
        assert key in data


def test_system_power_rejects_unknown_action(client):
    res = client.post("/api/system/power", json={"action": "reset-to-factory"})
    assert res.status_code == 400


def test_system_power_poweroff_uses_mocked_backend(client, monkeypatch):
    from app import system as system_module

    monkeypatch.setattr(system_module, "poweroff", lambda: (True, None))

    res = client.post("/api/system/power", json={"action": "poweroff"})

    assert res.status_code == 200
    assert res.get_json() == {"ok": True}


def test_system_power_reboot_reports_backend_error(client, monkeypatch):
    from app import system as system_module

    monkeypatch.setattr(system_module, "reboot", lambda: (False, "Interactive authentication required."))

    res = client.post("/api/system/power", json={"action": "reboot"})

    assert res.status_code == 400
    assert res.get_json()["error"] == "Interactive authentication required."


def test_system_power_stops_running_stream_and_recording_first(client, app, tmp_path, monkeypatch):
    from app import system as system_module

    monkeypatch.setattr(system_module, "poweroff", lambda: (True, None))
    client.post(
        "/settings",
        data={
            "srt_url": "srt://example.com:9000",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "recording_target": str(tmp_path),
        },
    )
    client.post("/api/stream/start")
    client.post("/api/recording/start")

    res = client.post("/api/system/power", json={"action": "poweroff"})

    assert res.status_code == 200
    assert app.state.stream_manager.status().running is False
    assert app.state.recording_manager.status().running is False


def test_settings_post_syncs_dsnoop_config(client, app, monkeypatch):
    sync_calls = []
    monkeypatch.setattr(app.state, "sync_dsnoop_config", lambda: sync_calls.append(True))

    res = client.post(
        "/settings",
        data={
            "srt_url": "srt://x",
            "operator_url": "",
            "audio_device": "hw:1,0",
            "channels": "2",
            "sample_rate": "48000",
            "bitrate_kbps": "160",
            "web_password": "",
        },
    )
    assert res.status_code == 200
    assert sync_calls == [True]


def test_power_icons_only_shown_on_settings_page(client):
    res = client.get("/")
    assert b"btn-poweroff" not in res.data
    assert b"btn-reboot" not in res.data

    res = client.get("/settings")
    assert b"btn-poweroff" in res.data
    assert b"btn-reboot" in res.data


def test_network_set_static_reports_backend_error(client, monkeypatch):
    from app import wifi

    monkeypatch.setattr(
        wifi,
        "set_ipv4_static",
        lambda connection, address, prefix, gateway, dns: (False, "Ungültige IP-Adresse."),
    )

    res = client.post(
        "/api/network/set-static",
        json={"connection": "eth0", "address": "bad", "prefix": 24, "gateway": "1.1.1.1", "dns": []},
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "Ungültige IP-Adresse."
