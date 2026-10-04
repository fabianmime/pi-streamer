"""Einstiegspunkt für Produktivbetrieb (systemd) und lokale Entwicklung.

Der Port kommt aus der Konfiguration (Standard: 80, änderbar über die
Netzwerk-Sektion der Settings-Seite) - deshalb wird waitress hier
programmatisch aus Python heraus gestartet statt über die `waitress-serve`
CLI, die den Port sonst schon vor dem Config-Laden bräuchte.
"""

import sys

import waitress

from app import create_app

app = create_app()

if __name__ == "__main__":
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
