"""Routen-Tests für die Produktiv-Erweiterungen: Cockpit-/Admin-Passwort,
Aufnahme-Bereich ausblenden, Gerätename, Update-Endpunkte."""

import sys

import pytest

from app import create_app
from app.recorder import RecordingManager
from app.streamer import StreamManager


def _sleep_cmd_builder(_cfg):
    return [sys.executable, "-c", "import time; time.sleep(5)"]


def _sleep_record_cmd_builder(_cfg, _output_path):
    return [sys.executable, "-c", "import time; time.sleep(5)"]


@pytest.fixture
def app(tmp_path):
    flask_app = create_app(config_path=tmp_path / "config.json")
    flask_app.state.stream_manager = StreamManager(cmd_builder=_sleep_cmd_builder)
    flask_app.state.recording_manager = RecordingManager(cmd_builder=_sleep_record_cmd_builder)
    flask_app.config.update(TESTING=True)
    yield flask_app
    flask_app.state.stream_manager.stop()
    flask_app.state.recording_manager.stop()


@pytest.fixture
def client(app):
    return app.test_client()


# --- Aufnahme-Bereich ausblenden ------------------------------------------------


def test_hide_recording_removes_recording_card_from_cockpit(client, app):
    assert b'id="recording-toggle"' in client.get("/").data

    res = client.post("/settings", data={"hide_recording": "1"})
    assert res.status_code == 200
    assert app.state.config.hide_recording is True

    data = client.get("/").data
    assert b'id="recording-toggle"' not in data
    assert b'id="stream-toggle"' in data


def test_hide_recording_also_hides_storage_hint(client, app, tmp_path):
    client.post("/settings", data={"recording_target": str(tmp_path)})
    assert b"storage-mini" in client.get("/").data

    client.post("/settings", data={"recording_target": str(tmp_path), "hide_recording": "1"})
    assert b"storage-mini" not in client.get("/").data


def test_settings_offers_both_hide_options(client):
    data = client.get("/settings").data
    assert b'name="hide_operator"' in data
    assert b'name="hide_recording"' in data


# --- Zwei Passwortstufen ----------------------------------------------------------


def _set_passwords(app, cockpit="", admin=""):
    app.state.config.web_password = cockpit
    app.state.config.admin_password = admin


def test_cockpit_password_login_reaches_cockpit_but_not_settings(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")

    client.post("/login", data={"password": "bühne"})

    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    res = client.get("/settings", follow_redirects=False)
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]
    assert "admin=1" in res.headers["Location"]
    assert client.get("/api/system/stats").status_code == 403


def test_cockpit_user_can_start_and_stop_stream(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")
    app.state.config.srt_url = "srt://x"
    app.state.config.audio_device = "hw:1,0"
    client.post("/login", data={"password": "bühne"})

    assert client.post("/api/stream/start").status_code == 200
    assert client.post("/api/stream/stop").status_code == 200


def test_admin_password_login_reaches_everything(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")

    client.post("/login", data={"password": "chef"})

    assert client.get("/").status_code == 200
    assert client.get("/settings").status_code == 200
    assert client.get("/api/system/stats").status_code == 200


def test_admin_login_page_rejects_cockpit_password(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")

    res = client.post("/login", data={"password": "bühne", "need_admin": "1", "next": "/settings"})

    assert res.status_code == 401
    assert "Falsches Admin-Passwort".encode("utf-8") in res.data


def test_admin_login_page_shows_admin_hint(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")
    res = client.get("/login?next=/settings&admin=1")
    assert res.status_code == 200
    assert "Admin-Passwort".encode("utf-8") in res.data


def test_only_admin_password_leaves_cockpit_open(client, app):
    _set_passwords(app, admin="chef")

    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/settings", follow_redirects=False).status_code == 302
    assert client.get("/api/wifi/status").status_code == 401


def test_only_cockpit_password_protects_settings_like_before(client, app):
    _set_passwords(app, cockpit="bühne")

    assert client.get("/settings", follow_redirects=False).status_code == 302
    client.post("/login", data={"password": "bühne"})
    assert client.get("/settings").status_code == 200


def test_changed_admin_password_invalidates_admin_session(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")
    client.post("/login", data={"password": "chef"})
    assert client.get("/settings").status_code == 200

    app.state.config.admin_password = "neu"

    assert client.get("/settings", follow_redirects=False).status_code == 302


def test_removed_cockpit_password_does_not_lock_out(client, app):
    _set_passwords(app, cockpit="bühne")
    client.post("/login", data={"password": "bühne"})

    app.state.config.web_password = ""

    assert client.get("/").status_code == 200
    assert client.get("/settings").status_code == 200


def test_setting_admin_password_keeps_current_session_logged_in(client, app):
    res = client.post("/settings", data={"admin_password": "chef"})

    assert res.status_code == 200
    assert app.state.config.admin_password == "chef"
    assert client.get("/settings").status_code == 200


def test_clear_admin_password(client, app):
    _set_passwords(app, admin="chef")
    client.post("/login", data={"password": "chef"})

    client.post("/settings", data={"clear_admin_password": "1"})

    assert app.state.config.admin_password == ""


def test_empty_password_fields_keep_existing_passwords(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")
    client.post("/login", data={"password": "chef"})

    client.post("/settings", data={"web_password": "", "admin_password": ""})

    assert app.state.config.web_password == "bühne"
    assert app.state.config.admin_password == "chef"


def test_settings_page_never_contains_passwords(client, app):
    _set_passwords(app, cockpit="geheimcockpit", admin="geheimadmin")
    client.post("/login", data={"password": "geheimadmin"})

    data = client.get("/settings").data

    assert b"geheimcockpit" not in data
    assert b"geheimadmin" not in data


def test_session_cookie_is_samesite_lax(app):
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


# --- Weitere neue Einstellungen ---------------------------------------------------


def test_settings_post_saves_new_fields(client, app):
    res = client.post(
        "/settings",
        data={
            "recording_format": "flac",
            "stream_auto_reconnect": "1",
            "update_repo_url": "https://github.com/fabianmime/pi-streamer.git",
            "update_branch": "main",
            "update_auto": "1",
        },
    )
    assert res.status_code == 200
    cfg = app.state.config
    assert cfg.recording_format == "flac"
    assert cfg.stream_auto_reconnect is True
    assert cfg.update_auto is True


def test_settings_post_rejects_unsafe_update_source(client, app):
    res = client.post("/settings", data={"update_repo_url": "file:///etc"})
    assert res.status_code == 400
    assert app.state.config.update_repo_url != "file:///etc"


def test_changing_audio_device_resets_card_id(client, app):
    app.state.config.audio_device = "plughw:1,0"
    app.state.config.audio_card_id = "WH62"

    client.post("/settings", data={"audio_device": "plughw:2,0"})

    assert app.state.config.audio_card_id == ""


# --- Gerätename -----------------------------------------------------------------


def test_settings_shows_current_hostname(client, monkeypatch):
    from app import system

    monkeypatch.setattr(system, "current_hostname", lambda: "buehne-links")
    data = client.get("/settings").data
    assert b'value="buehne-links"' in data
    assert b"buehne-links</span>.local" in data


def test_settings_post_changes_hostname(client, monkeypatch):
    from app import system

    monkeypatch.setattr(system, "current_hostname", lambda: "alt")
    calls = []
    monkeypatch.setattr(system, "set_hostname", lambda name: calls.append(name) or (True, None))

    res = client.post("/settings", data={"hostname": "neu"})

    assert res.status_code == 200
    assert calls == ["neu"]
    assert "Gerätename geändert".encode("utf-8") in res.data


def test_settings_post_unchanged_hostname_does_nothing(client, monkeypatch):
    from app import system

    monkeypatch.setattr(system, "current_hostname", lambda: "gleich")
    calls = []
    monkeypatch.setattr(system, "set_hostname", lambda name: calls.append(name) or (True, None))

    client.post("/settings", data={"hostname": "gleich"})

    assert calls == []


def test_settings_post_invalid_hostname_is_rejected_before_saving(client, app, monkeypatch):
    from app import system

    monkeypatch.setattr(system, "current_hostname", lambda: "alt")
    calls = []
    monkeypatch.setattr(system, "set_hostname", lambda name: calls.append(name) or (True, None))

    res = client.post("/settings", data={"hostname": "kein gültiger name", "srt_url": "srt://neu"})

    assert res.status_code == 400
    assert calls == []
    assert app.state.config.srt_url != "srt://neu"


def test_settings_post_reports_hostname_failure(client, monkeypatch):
    from app import system

    monkeypatch.setattr(system, "current_hostname", lambda: "alt")
    monkeypatch.setattr(system, "set_hostname", lambda name: (False, "Zugriff verweigert"))

    res = client.post("/settings", data={"hostname": "neu"})

    assert res.status_code == 200
    assert "Gerätename nicht geändert: Zugriff verweigert".encode("utf-8") in res.data


# --- Update ---------------------------------------------------------------------


def test_update_status_reports_shape(client):
    data = client.get("/api/update/status").get_json()
    for key in ("state", "update_available", "pending_commits", "configured", "busy"):
        assert key in data
    assert data["configured"] is True  # GitHub-Repo ist Standard


def test_update_check_starts_background_check(client, app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.state.updater, "start_async", lambda action: calls.append(action))

    res = client.post("/api/update/check")

    assert res.status_code == 202
    assert len(calls) == 1


def test_update_install_refused_while_streaming(client, app, monkeypatch):
    app.state.config.srt_url = "srt://x"
    app.state.config.audio_device = "hw:1,0"
    client.post("/api/stream/start")
    calls = []
    monkeypatch.setattr(app.state.updater, "start_async", lambda action: calls.append(action))

    res = client.post("/api/update/install")

    assert res.status_code == 409
    assert calls == []


def test_update_install_starts_when_idle(client, app, monkeypatch):
    calls = []
    monkeypatch.setattr(app.state.updater, "start_async", lambda action: calls.append(action))

    res = client.post("/api/update/install")

    assert res.status_code == 202
    assert len(calls) == 1


def test_update_without_source_is_rejected(client, app):
    app.state.config.update_repo_url = ""
    assert client.post("/api/update/check").status_code == 400
    assert client.post("/api/update/install").status_code == 400


def test_update_endpoints_need_admin(client, app):
    _set_passwords(app, cockpit="bühne", admin="chef")
    client.post("/login", data={"password": "bühne"})

    assert client.post("/api/update/install").status_code == 403
    assert client.get("/api/update/status").status_code == 403
