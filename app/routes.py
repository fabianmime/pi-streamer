from __future__ import annotations

import logging
import os
import threading
from dataclasses import asdict
from datetime import datetime

from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

from . import configure_logging, logs, storage, system, wifi
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
    }
    context.update(extra)
    return context


_PUBLIC_ENDPOINTS = {"login", "login_submit", "logout", "static"}


def _safe_next_url(raw: str | None) -> str | None:
    """Nur relative Pfade als Redirect-Ziel zulassen (kein Open-Redirect)."""
    if raw and raw.startswith("/") and not raw.startswith("//"):
        return raw
    return None


def register(app: Flask) -> None:
    state = app.state  # type: ignore[attr-defined]

    @app.before_request
    def require_login():
        password = state.config.web_password
        if not password:
            return None
        if request.endpoint in _PUBLIC_ENDPOINTS:
            return None
        if session.get("authenticated"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Nicht angemeldet."}), 401
        return redirect(url_for("login", next=request.full_path if request.query_string else request.path))

    @app.get("/login")
    def login():
        if not state.config.web_password or session.get("authenticated"):
            return redirect(url_for("index"))
        return render_template("login.html", error=None, next=_safe_next_url(request.args.get("next")) or "")

    @app.post("/login")
    def login_submit():
        submitted = request.form.get("password", "")
        next_url = _safe_next_url(request.form.get("next"))
        if submitted and state.config.web_password and submitted == state.config.web_password:
            session.permanent = True
            session["authenticated"] = True
            logger.info("WebUI-Login erfolgreich")
            return redirect(next_url or url_for("index"))
        logger.warning("WebUI-Login fehlgeschlagen")
        return render_template("login.html", error="Falsches Passwort.", next=next_url or ""), 401

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
        old_port = state.config.web_port
        new_cfg = StreamerConfig(
            srt_url=form.get("srt_url", "").strip(),
            operator_url=form.get("operator_url", "").strip(),
            audio_device=form.get("audio_device", "").strip(),
            channels=int(form.get("channels", state.config.channels)),
            sample_rate=int(form.get("sample_rate", state.config.sample_rate)),
            bitrate_kbps=int(form.get("bitrate_kbps", state.config.bitrate_kbps)),
            web_password=form.get("web_password", "") or state.config.web_password,
            web_port=int(form.get("web_port", state.config.web_port) or state.config.web_port),
            hide_operator=bool(form.get("hide_operator")),
            log_level=form.get("log_level", state.config.log_level) or state.config.log_level,
            recording_target=form.get("recording_target", state.config.recording_target).strip(),
            recording_filename_prefix=form.get(
                "recording_filename_prefix", state.config.recording_filename_prefix
            ).strip(),
        )
        if form.get("clear_password"):
            new_cfg.web_password = ""

        try:
            new_cfg.validated()
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

        port_changed = new_cfg.web_port != old_port
        if port_changed:
            logger.info("WebUI-Port geändert: %s -> %s, Dienst startet neu", old_port, new_cfg.web_port)
            _schedule_restart()

        return render_template(
            "settings.html", **_settings_context(state, saved=True, port_changed=port_changed, new_port=new_cfg.web_port)
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
            system.estimate_recording_seconds(free_bytes, state.config.channels, state.config.sample_rate)
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

    @app.get("/api/logs/download")
    def download_logs():
        content = logs.get_recent_log_text()
        filename = f"livevoice-streamer-log-{datetime.now():%Y%m%d-%H%M%S}.txt"
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
