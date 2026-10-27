"""Einstiegspunkt für Produktivbetrieb (systemd) und lokale Entwicklung.

Der Port kommt aus der Konfiguration (Standard: 80, änderbar über die
Netzwerk-Sektion der Settings-Seite) - deshalb wird waitress hier
programmatisch aus Python heraus gestartet statt über die `waitress-serve`
CLI, die den Port sonst schon vor dem Config-Laden bräuchte.
"""

import sys
import threading

import waitress

from app import create_app

app = create_app()

if __name__ == "__main__":
    # Nur im echten Dienst, nicht in Tests: Stream nach Stromausfall
    # fortsetzen und die automatische Update-Prüfung starten.
    app.state.resume_stream_if_wanted()
    threading.Thread(
        target=app.state.updater.auto_loop,
        args=(lambda: app.state.config, app.state.is_busy),
        daemon=True,
    ).start()

    port = app.state.config.web_port
    try:
        waitress.serve(app, host="0.0.0.0", port=port)
    except PermissionError:
        print(
            f"Fehler: Keine Berechtigung, Port {port} zu binden. "
            "Ports unter 1024 brauchen entweder root-Rechte oder die "
            "CAP_NET_BIND_SERVICE-Fähigkeit (siehe systemd-Unit) - oder in "
            "den Einstellungen unter 'Netzwerk' einen Port ab 1024 setzen.",
            file=sys.stderr,
        )
        sys.exit(1)
