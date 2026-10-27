from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime

from flask import Flask, Response, current_app, jsonify, redirect, render_template, request, session, url_for

from . import configure_logging, logs, recorder, storage, system, wifi
from .config import VALID_BITRATES_KBPS, VALID_LOG_LEVELS, VALID_SAMPLE_RATES, StreamerConfig

logger = logging.getLogger(__name__)


def _schedule_restart(delay: float = 1.5) -> None:
    """Beendet den Prozess verzögert (nach dem Absenden der HTTP-Antwort).

    systemd startet den Dienst danach neu (Restart=always) - der einzige
    saubere Weg, einen neuen Port zu binden, ohne dem Service-User
    weitreichende Rechte (sudo systemctl restart o.ä.) einzuräumen.
    """
    threading.Timer(delay, os._exit, args=(0,)).start()


def _settings_context(state, **extra) -> dict:
    status = wifi.get_status()
    context = {
        "cfg": state.config.to_public_dict(),
        "audio_devices": state.refresh_audio_devices(),
        "usb_drives": state.refresh_usb_drives(),
        "valid_bitrates": VALID_BITRATES_KBPS,
        "valid_sample_rates": VALID_SAMPLE_RATES,
        "valid_log_levels": VALID_LOG_LEVELS,
        "network_available": status.available,
        "active_connections": status.active_connections,
        "ip_addresses": status.ip_addresses,
        "saved_connections": wifi.list_saved_connections() if status.available else [],
        # Echtes Beispiel-Datum/-Zeit statt Buchstaben-Platzhalter im
        # Dateiname-Präfix-Hinweis (Einstellungen -> Aufnahme) - selbes
        # Format wie recorder.py::recording_filename.
        "example_timestamp": datetime.now().strftime("%Y%m%d-%H%M%S"),
        "recording_formats": recorder.RECORDING_FORMATS,
        "hostname": system.current_hostname(),
        "update_status": state.updater.status(),
    }
    context.update(extra)
    return context


_PUBLIC_ENDPOINTS = {"login", "login_submit", "logout", "static"}

# Was mit dem Cockpit-Passwort erreichbar ist: Stream/Aufnahme bedienen und
# ihren Status sehen. Alles andere (Einstellungen, WLAN, USB-Verwaltung,
# Power, Logs, Update) braucht - falls gesetzt - das Admin-Passwort.
_COCKPIT_ENDPOINTS = {
    "index",
    "api_status",
    "api_start",
    "api_stop",
    "api_stream_dismiss_error",
    "api_audio_level",
    "api_recording_status",
    "api_recording_start",
    "api_recording_stop",
    "api_recording_dismiss_error",
}

ROLE_USER = "user"
ROLE_ADMIN = "admin"


def _safe_next_url(raw: str | None) -> str | None:
    """Nur relative Pfade als Redirect-Ziel zulassen (kein Open-Redirect)."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return None


def _password_fingerprint(password: str) -> str:
    """Kennung des Passworts, mit dem eine Session angemeldet wurde - wird
    das Passwort geändert/entfernt, verliert die Session ihre Rechte, statt
    30 Tage lang mit dem alten Passwort weiterzugelten."""
    key = str(current_app.secret_key).encode("utf-8")
    return hmac.new(key, password.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _password_matches(submitted: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def required_role(cfg: StreamerConfig, endpoint: str | None) -> str | None:
    """None = frei zugänglich. Ohne Admin-Passwort schützt das
    Cockpit-Passwort wie bisher alles; ohne Cockpit-Passwort ist das Cockpit
    offen, die Einstellungen aber trotzdem per Admin-Passwort geschützt."""
    if endpoint in _PUBLIC_ENDPOINTS:
        return None
    if endpoint in _COCKPIT_ENDPOINTS:
        return ROLE_USER if cfg.web_password else None
    if cfg.admin_password:
        return ROLE_ADMIN
    return ROLE_USER if cfg.web_password else None


def effective_role(cfg: StreamerConfig) -> str | None:
    role = session.get("role")
    fingerprint = session.get("fp")
    if role == ROLE_ADMIN and cfg.admin_password and fingerprint == _password_fingerprint(cfg.admin_password):
        return ROLE_ADMIN
    if role in (ROLE_USER, ROLE_ADMIN) and cfg.web_password and fingerprint == _password_fingerprint(cfg.web_password):
        return ROLE_USER
    return None


def _role_satisfies(have: str | None, need: str | None) -> bool:
    if need is None:
        return True
    if need == ROLE_USER:
        return have in (ROLE_USER, ROLE_ADMIN)
    return have == ROLE_ADMIN


def _grant_session(role: str, password: str) -> None:
    session.permanent = True
    session["role"] = role
    session["fp"] = _password_fingerprint(password)


def register(app: Flask) -> None:
    state = app.state  # type: ignore[attr-defined]

    @app.before_request
    def require_login():
        need = required_role(state.config, request.endpoint)
        have = effective_role(state.config)
        if _role_satisfies(have, need):
            return None
        if request.path.startswith("/api/"):
            message = "Admin-Anmeldung erforderlich." if have else "Nicht angemeldet."
            return jsonify({"error": message}), 401 if not have else 403
        return redirect(
            url_for(
                "login",
                next=request.full_path if request.query_string else request.path,
                admin=1 if need == ROLE_ADMIN else None,
            )
        )

    @app.context_processor
    def inject_auth():
        cfg = state.config
        return {
            "logged_in_role": effective_role(cfg),
            "any_password_set": bool(cfg.web_password or cfg.admin_password),
        }

    @app.get("/login")
    def login():
        next_url = _safe_next_url(request.args.get("next")) or ""
        need_admin = bool(request.args.get("admin")) and bool(state.config.admin_password)
        need = ROLE_ADMIN if need_admin else (ROLE_USER if state.config.web_password else None)
        if need is None or _role_satisfies(effective_role(state.config), need):
            return redirect(next_url or url_for("index"))
        return render_template("login.html", error=None, next=next_url, need_admin=need_admin)

    @app.post("/login")
    def login_submit():
        submitted = request.form.get("password", "")
        next_url = _safe_next_url(request.form.get("next"))
        need_admin = bool(request.form.get("need_admin"))
        cfg = state.config
        if _password_matches(submitted, cfg.admin_password):
            _grant_session(ROLE_ADMIN, cfg.admin_password)
            logger.info("WebUI-Login erfolgreich (Admin)")
            return redirect(next_url or url_for("index"))
        if not need_admin and _password_matches(submitted, cfg.web_password):
            _grant_session(ROLE_USER, cfg.web_password)
            logger.info("WebUI-Login erfolgreich (Cockpit)")
            return redirect(next_url or url_for("index"))
        logger.warning("WebUI-Login fehlgeschlagen")
        error = "Falsches Admin-Passwort." if need_admin else "Falsches Passwort."
        return render_template("login.html", error=error, next=next_url or "", need_admin=need_admin), 401

    @app.route("/logout", methods=["GET", "POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            operator_url=state.config.operator_url,
            hide_operator=state.config.hide_operator,
            hide_recording=state.config.hide_recording,
            cfg_bitrate=state.config.bitrate_kbps,
            cfg_sample_rate=state.config.sample_rate,
            cfg_device=state.config.audio_device,
            cfg_channels=state.config.channels,
            recording_target=state.config.recording_target,
        )

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", **_settings_context(state))

    @app.post("/settings")
    def save_settings():
        form = request.form
        old = state.config
        old_port = old.web_port
        new_cfg = StreamerConfig(
            srt_url=form.get("srt_url", "").strip(),
            operator_url=form.get("operator_url", "").strip(),
            audio_device=form.get("audio_device", "").strip(),
            audio_card_id=old.audio_card_id,
            channels=int(form.get("channels", old.channels)),
            sample_rate=int(form.get("sample_rate", old.sample_rate)),
            bitrate_kbps=int(form.get("bitrate_kbps", old.bitrate_kbps)),
            web_password=form.get("web_password", "") or old.web_password,
            admin_password=form.get("admin_password", "") or old.admin_password,
            web_port=int(form.get("web_port", old.web_port) or old.web_port),
            hide_operator=bool(form.get("hide_operator")),
            hide_recording=bool(form.get("hide_recording")),
            log_level=form.get("log_level", old.log_level) or old.log_level,
            recording_target=form.get("recording_target", old.recording_target).strip(),
            recording_filename_prefix=form.get("recording_filename_prefix", old.recording_filename_prefix).strip(),
            recording_format=form.get("recording_format", old.recording_format) or old.recording_format,
            stream_auto_reconnect=bool(form.get("stream_auto_reconnect")),
            update_repo_url=form.get("update_repo_url", old.update_repo_url).strip(),
            update_branch=form.get("update_branch", old.update_branch).strip() or old.update_branch,
            update_auto=bool(form.get("update_auto")),
        )
        if form.get("clear_password"):
            new_cfg.web_password = ""
        if form.get("clear_admin_password"):
            new_cfg.admin_password = ""
        # Anderes Gerät gewählt -> alte Kartenkennung verwerfen, die
        # Synchronisierung unten setzt die passende neu.
        if new_cfg.audio_device != old.audio_device:
            new_cfg.audio_card_id = ""

        new_hostname = form.get("hostname", "").strip()
        current_hostname = system.current_hostname()
        hostname_changed = bool(new_hostname) and new_hostname != current_hostname

        try:
            new_cfg.validated()
            if hostname_changed:
                hostname_error = system.validate_hostname(new_hostname)
                if hostname_error:
                    raise ValueError(hostname_error)
        except ValueError as exc:
            return render_template(
                "settings.html", **_settings_context(state, cfg=new_cfg.to_public_dict(), error=str(exc))
            ), 400

        state.config = new_cfg
        state.save()
        configure_logging(new_cfg.log_level)
        # Regeneriert ~/.asoundrc fürs (ggf. neu gewählte) Audio-Gerät - macht
        # den Wechsel auf ein anderes USB-Interface plug-and-play, siehe
        # AppState.sync_dsnoop_config.
        state.sync_dsnoop_config()
        logger.info("Einstellungen gespeichert (log_level=%s)", new_cfg.log_level)

        # Wer speichern durfte, bleibt angemeldet - auch wenn hier gerade
        # ein (neues) Passwort gesetzt wurde, das die eigene Session sonst
        # sofort aussperren würde.
        if new_cfg.admin_password:
            _grant_session(ROLE_ADMIN, new_cfg.admin_password)
        elif new_cfg.web_password:
            _grant_session(ROLE_USER, new_cfg.web_password)

        hostname_error = None
        if hostname_changed:
            ok, hostname_error = system.set_hostname(new_hostname)
            if ok:
                logger.info("Gerätename geändert: %s -> %s", current_hostname, new_hostname)
            else:
                logger.warning("Gerätename konnte nicht geändert werden: %s", hostname_error)

        port_changed = new_cfg.web_port != old_port
        if port_changed:
            logger.info("WebUI-Port geändert: %s -> %s, Dienst startet neu", old_port, new_cfg.web_port)
            _schedule_restart()

        return render_template(
            "settings.html",
            **_settings_context(
                state,
                saved=True,
                port_changed=port_changed,
                new_port=new_cfg.web_port,
                hostname_changed=hostname_changed and not hostname_error,
                error=f"Gerätename nicht geändert: {hostname_error}" if hostname_error else None,
            ),
        )

    @app.get("/api/status")
    def api_status():
        status = state.stream_manager.status()
        payload = asdict(status)
        payload["config_ready"] = state.config.is_ready_to_stream()
        return jsonify(payload)

    @app.post("/api/stream/start")
    def api_start():
        if not state.config.is_ready_to_stream():
            return jsonify({"error": "Stream-URL und Audio-Gerät müssen in den Einstellungen gesetzt sein."}), 400
        try:
            status = state.stream_manager.start(state.config)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(asdict(status))

    @app.post("/api/stream/stop")
    def api_stop():
        status = state.stream_manager.stop()
        return jsonify(asdict(status))

    @app.post("/api/stream/dismiss-error")
    def api_stream_dismiss_error():
        state.stream_manager.clear_error()
        return jsonify(asdict(state.stream_manager.status()))

    @app.get("/api/audio-devices")
    def api_audio_devices():
        devices = state.refresh_audio_devices()
        return jsonify([{"hw_id": d.hw_id, "label": d.label} for d in devices])

    @app.get("/api/audio-level")
    def api_audio_level():
        state.ensure_level_meter()
        reading = state.current_audio_level()
        return jsonify(
            {
                "channel_peaks_db": reading.channel_peaks_db,
                "available": reading.available,
                "streaming": state.stream_manager.status().running,
                "recording": state.recording_manager.status().running,
            }
        )

    @app.get("/api/recording/status")
    def api_recording_status():
        status = state.recording_manager.status()
        payload = asdict(status)
        payload["config_ready"] = state.config.is_ready_to_record()
        target = state.config.recording_target
        free_bytes = storage.free_space_bytes(target) if target else None
        payload["target_free_bytes"] = free_bytes
        payload["target_estimated_seconds"] = (
            system.estimate_recording_seconds(
                free_bytes,
                state.config.channels,
                state.config.sample_rate,
                recorder.recording_format(state.config.recording_format).size_factor,
            )
            if free_bytes is not None
            else None
        )
        return jsonify(payload)

    @app.post("/api/recording/start")
    def api_recording_start():
        if not state.config.is_ready_to_record():
            return jsonify({"error": "Aufnahme-Ziel muss in den Einstellungen gewählt sein."}), 400
        try:
            status = state.recording_manager.start(state.config, state.config.recording_target)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify(asdict(status))

    @app.post("/api/recording/stop")
    def api_recording_stop():
        status = state.recording_manager.stop()
        return jsonify(asdict(status))

    @app.post("/api/recording/dismiss-error")
    def api_recording_dismiss_error():
        state.recording_manager.clear_error()
        return jsonify(asdict(state.recording_manager.status()))

    @app.get("/api/usb-drives")
    def api_usb_drives():
        drives = state.refresh_usb_drives()
        return jsonify(
            [
                {
                    "device": d.device,
                    "label": d.display_label,
                    "size_bytes": d.size_bytes,
                    "mountpoint": d.mountpoint,
                    "mounted": d.mounted,
                    "free_bytes": storage.free_space_bytes(d.mountpoint) if d.mountpoint else None,
                }
                for d in drives
            ]
        )

    @app.post("/api/usb-drives/mount")
    def api_usb_drives_mount():
        data = request.get_json(silent=True) or {}
        device = str(data.get("device", "")).strip()
        if not device:
            return jsonify({"error": "Kein Gerät angegeben."}), 400
        mountpoint, error = storage.mount_drive(device)
        if error:
            return jsonify({"error": error}), 400
        return jsonify({"mountpoint": mountpoint})

    @app.post("/api/usb-drives/unmount")
    def api_usb_drives_unmount():
        data = request.get_json(silent=True) or {}
        device = str(data.get("device", "")).strip()
        if not device:
            return jsonify({"error": "Kein Gerät angegeben."}), 400
        if state.recording_manager.status().running:
            return jsonify({"error": "Erst die laufende Aufnahme stoppen, bevor der Stick ausgeworfen wird."}), 409
        ok, error = storage.unmount_drive(device)
        if not ok:
            return jsonify({"error": error}), 400
        return jsonify({"ok": True})

    @app.post("/api/usb-drives/format")
    def api_usb_drives_format():
        data = request.get_json(silent=True) or {}
        device = str(data.get("device", "")).strip()
        if not device:
            return jsonify({"error": "Kein Gerät angegeben."}), 400
        if state.recording_manager.status().running:
            return jsonify({"error": "Erst die laufende Aufnahme stoppen, bevor der Stick formatiert wird."}), 409
        ok, error = storage.format_drive(device)
        if not ok:
            logger.warning("Formatieren von %s fehlgeschlagen: %s", device, error)
            return jsonify({"error": error}), 400
        logger.info("Stick formatiert: %s", device)
        # Der bisherige Mountpoint existiert nach dem Neuformatieren nicht
        # mehr in dieser Form - eine veraltete Config wuerde beim naechsten
        # Aufnahme-Start mit "Aufnahme-Ziel nicht erreichbar" fehlschlagen.
        if state.config.recording_target:
            state.config.recording_target = ""
            state.save()
        return jsonify({"ok": True})

    def _serialize_active_connection(c):
        ipv4 = None
        if c.ipv4 is not None:
            ipv4 = {
                "method": c.ipv4.method,
                "address": c.ipv4.address,
                "prefix": c.ipv4.prefix,
                "gateway": c.ipv4.gateway,
                "dns": c.ipv4.dns,
                "dhcp_server": c.ipv4.dhcp_server,
            }
        return {
            "device": c.device,
            "type": c.type,
            "name": c.name,
            "label": c.label,
            "ipv4": ipv4,
        }

    @app.get("/api/wifi/status")
    def api_wifi_status():
        status = wifi.get_status()
        return jsonify(
            {
                "available": status.available,
                "active_connections": [_serialize_active_connection(c) for c in status.active_connections],
                "ip_addresses": status.ip_addresses,
                "error": status.error,
            }
        )

    @app.post("/api/wifi/scan")
    def api_wifi_scan():
        networks = wifi.scan_networks()
        return jsonify(
            [{"ssid": n.ssid, "signal": n.signal, "secured": n.secured} for n in networks]
        )

    @app.post("/api/wifi/connect")
    def api_wifi_connect():
        data = request.get_json(silent=True) or {}
        ssid = str(data.get("ssid", ""))
        password = str(data.get("password", ""))
        ok, error = wifi.connect(ssid, password)
        if not ok:
            return jsonify({"error": error}), 400
        return jsonify({"ok": True})

    @app.post("/api/wifi/forget")
    def api_wifi_forget():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", ""))
        ok, error = wifi.forget(name)
        if not ok:
            return jsonify({"error": error}), 400
        return jsonify({"ok": True})

    @app.post("/api/network/set-dhcp")
    def api_network_set_dhcp():
        data = request.get_json(silent=True) or {}
        connection = str(data.get("connection", ""))
        ok, error = wifi.set_ipv4_dhcp(connection)
        if not ok:
            return jsonify({"error": error}), 400
        return jsonify({"ok": True})

    @app.post("/api/network/set-static")
    def api_network_set_static():
        data = request.get_json(silent=True) or {}
        connection = str(data.get("connection", ""))
        address = str(data.get("address", ""))
        gateway = str(data.get("gateway", ""))
        dns = data.get("dns") or []
        if not isinstance(dns, list):
            dns = [str(dns)]
        try:
            prefix = int(data.get("prefix", 24))
        except (TypeError, ValueError):
            return jsonify({"error": "Ungültiges Prefix."}), 400
        ok, error = wifi.set_ipv4_static(connection, address, prefix, gateway, [str(d) for d in dns])
        if not ok:
            return jsonify({"error": error}), 400
        return jsonify({"ok": True})

    @app.get("/api/system/stats")
    def api_system_stats():
        load = system.load_average()
        mem = system.memory_usage()
        disk = system.disk_usage()
        return jsonify(
            {
                "load_average": list(load) if load else None,
                "memory": (
                    {
                        "total_bytes": mem.total_bytes,
                        "used_bytes": mem.used_bytes,
                        "used_percent": mem.used_percent,
                    }
                    if mem
                    else None
                ),
                "disk": (
                    {
                        "total_bytes": disk.total_bytes,
                        "used_bytes": disk.used_bytes,
                        "free_bytes": disk.free_bytes,
                        "used_percent": disk.used_percent,
                    }
                    if disk
                    else None
                ),
                "cpu_temp_celsius": system.cpu_temperature_celsius(),
            }
        )

    @app.post("/api/system/power")
    def api_system_power():
        data = request.get_json(silent=True) or {}
        action = str(data.get("action", "")).strip()
        if action not in ("poweroff", "reboot"):
            return jsonify({"error": "Ungültige Aktion."}), 400

        # Sauber beenden statt den Stecker zu ziehen (siehe README, "Offene
        # Punkte") - ein Stream/eine Aufnahme wird vorher selbst gestoppt,
        # damit z.B. die WAV-Datei korrekt finalisiert wird, statt sich auf
        # den Shutdown-Kill der ganzen systemd-Unit zu verlassen.
        if state.recording_manager.status().running:
            state.recording_manager.stop()
        if state.stream_manager.status().running:
            state.stream_manager.stop()

        ok, error = system.poweroff() if action == "poweroff" else system.reboot()
        if not ok:
            logger.warning("%s fehlgeschlagen: %s", action, error)
            return jsonify({"error": error}), 400
        logger.info("%s ausgelöst", action)
        return jsonify({"ok": True})

    @app.get("/api/update/status")
    def api_update_status():
        payload = state.updater.status()
        payload["configured"] = bool(state.config.update_repo_url)
        payload["busy"] = state.is_busy()
        return jsonify(payload)

    @app.post("/api/update/check")
    def api_update_check():
        cfg = state.config
        if not cfg.update_repo_url:
            return jsonify({"error": "Keine Update-Quelle eingetragen."}), 400
        state.updater.start_async(lambda: state.updater.check(cfg.update_repo_url, cfg.update_branch))
        return jsonify({"started": True}), 202

    @app.post("/api/update/install")
    def api_update_install():
        cfg = state.config
        if not cfg.update_repo_url:
            return jsonify({"error": "Keine Update-Quelle eingetragen."}), 400
        if state.is_busy():
            return jsonify({"error": "Erst Stream und Aufnahme stoppen, dann aktualisieren."}), 409
        logger.info("Update-Installation über WebUI angestossen")
        state.updater.start_async(
            lambda: state.updater.install(cfg.update_repo_url, cfg.update_branch, state.is_busy)
        )
        return jsonify({"started": True}), 202

    @app.get("/api/logs/download")
    def download_logs():
        content = logs.get_recent_log_text()
        filename = f"livevoice-streamer-log-{datetime.now():%Y%m%d-%H%M%S}.txt"
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
