# LiveVoice Pi Streamer

Eigenständiger Audio-Streamer für einen Raspberry Pi: nimmt ein
angeschlossenes USB-Audio-Interface auf und streamt es per SRT zu
[LiveVoice](https://livevoice.io). Start/Stop des Streams läuft über ein
kleines, iPad-taugliches WebUI auf dem Pi selbst; der LiveVoice-Operator
(Übersetzungs-/Kanalsteuerung) wird im selben WebUI verlinkt (Button, neuer
Tab) - es gibt keine eigene Integration in die LiveVoice-ActionCable-API
(anders als das separate [Companion-Modul](../LiveVoice-Companion-Module/)),
und die Operator-Seite lässt sich hier nicht einbetten (LiveVoice blockiert
das serverseitig per `X-Frame-Options`, unabhängig vom Browser - das ist
kein Bug dieses Projekts, sondern von LiveVoice so vorgegeben).

> «LiveVoice» und das LiveVoice-Logo sind Marken der LiveVoice GmbH. Dieses
> Projekt ist ein unabhängiges Werkzeug, steht in keiner Verbindung zu
> LiveVoice und verwendet weder deren Logo noch deren Wortmarke als
> Produktname. Es werden keine Zugangsdaten oder Event-Daten an Dritte
> übertragen.

## Inhalt

- [Funktionsüberblick](#funktionsüberblick)
- [Marke / Icon](#marke--icon)
- [Architektur](#architektur)
- [Projektstruktur](#projektstruktur)
- [WebUI im Detail](#webui-im-detail)
- [Entwicklung](#entwicklung)
- [Deployment](#deployment)
- [Sicherheit / Risiken](#sicherheit--risiken)
- [Bekannte Grenzen](#bekannte-grenzen)
- [Offene Punkte / Ideen](#offene-punkte--ideen)

## Funktionsüberblick

- USB-Audio-Interface -> ffmpeg (AAC/MPEG-TS) -> SRT -> LiveVoice-Ingest,
  gesteuert über das "Cockpit" (Startseite, `/`) - ein WebUI, das für
  Touch/iPad ausgelegt ist.
- Beliebiges USB-Audio-Interface anschliessbar, ohne SSH/manuelles
  Konfigurieren: in den Einstellungen unter Audio "Neu suchen" tippen, Gerät
  wählen, speichern - der geteilte Capture-Alias fürs Interface wird dabei
  automatisch passend neu geschrieben (siehe "Recording: geteilter
  Audio-Zugriff").
- Start/Stop des Streams samt Live-Status (Laufzeit, Bitrate, Sample-Rate,
  Gerät, Fehler). Bekannte Fehlerbilder (Import bei LiveVoice noch nicht
  gestartet, Import von LiveVoice-Seite gestoppt) werden als verständlicher
  Hinweistext statt rohem ffmpeg-Log angezeigt; jede Fehlermeldung lässt sich
  einzeln über ein "×" schliessen.
- Eingangspegel-Meter ganz oben auf dem Cockpit (gehört zu Stream **und**
  Aufnahme) - läuft dauerhaft, auch während Stream und/oder Aufnahme aktiv
  sind (siehe "Eingangspegel-Meter: läuft dauerhaft mit"). Bei Stereo zwei
  eigene Balken/dB-Anzeigen für L und R statt nur einem kombinierten Wert.
- Parallele, verlustfreie WAV-Aufnahme auf einen USB-Stick - unabhängig vom
  Stream start-/stoppbar, übersteht also auch einen Netzwerk-Aussetzer des
  Streams (siehe "Recording: geteilter Audio-Zugriff"). Dateiname-Präfix in
  den Einstellungen frei wählbar (Standard "Aufnahme"). USB-Stick-Erkennung,
  Einhängen, sicheres Auswerfen und komplettes Neuformatieren (exFAT) direkt
  im WebUI (Einstellungen → Aufnahme).
- USB-Speicher des Aufnahme-Ziels + geschätzte Restlaufzeit direkt auf dem
  Cockpit (in der Pegel-Karte, siehe "Eingangspegel-Meter: läuft dauerhaft
  mit"), mit Warnfarbe bei knapper werdender Restlaufzeit.
- LiveVoice-Operator-Link als Button (neuer Tab); optional ganz ausblendbar.
- WLAN scannen/verbinden/vergessen sowie LAN- und WLAN-Status (IP, Gateway,
  DNS, DHCP-Server) direkt im WebUI - kein SSH nötig, um das Gerät zwischen
  Event-Locations umzuhängen.
- DHCP oder feste IP pro Netzwerkverbindung umschaltbar.
- Hell/Dunkel-Theme, eigenes Login-Screen (kein Browser-Popup), Log-Level
  und Log-Download für die Fehlersuche nach einem Event.

## Marke / Icon

Kopf-Logo und App-Icon (`app/static/brand/`) sind das eigens für dieses
Projekt entworfene "LiveVoice Streamer"-Set (gleiche Kachel-Grammatik/Farben
wie die Companion-Modul-Marke, aber eigenes Zeichen und eigene Wortmarke) -
für einen einheitlichen, aber unterscheidbaren Auftritt über Fabians
LiveVoice-Werkzeuge hinweg. `logo-lockup-{light,dark}.svg` im Header
wechseln automatisch mit dem Theme; `icon-180-apple-touch.png` dient als
`apple-touch-icon` fürs iPad "Zum Home-Bildschirm". Die Login-Seite zeigt
bewusst immer nur die helle Variante (einfacher, auf Wunsch so belassen).

## Architektur

```text
                              +--> ffmpeg (AAC/MPEG-TS) --SRT--> LiveVoice-Ingest
                              |
                              +--> ffmpeg (WAV, verlustfrei) --> USB-Stick
                              |
USB-Audio-Interface --ALSA (dsnoop-Alias, geteilt)
                              |
                              +--> ffmpeg (Pegel-Messung, verwirft Audio) --> WebUI
                                   ^
                                   | start/stop (je eigener subprocess)
                              Flask-WebUI (app/)
                                   |
                              Button (neuer Tab) -> LiveVoice Operator-Seite
```

Alle drei ffmpeg-Prozesse (Stream, Recording, Pegel-Messung) laufen
unabhängig voneinander und teilen sich denselben `dsnoop`-Capture-Stream -
siehe "Eingangspegel-Meter: läuft dauerhaft mit" und "Recording: geteilter
Audio-Zugriff" weiter unten.

Die WebUI-Session (Login) ist unabhängig vom Stream: ein abgelaufenes/
falsches Passwort blockiert nie den laufenden Stream selbst, nur den
Zugriff auf die Steuerseite.

### Audio-Gerät: `plughw:` statt `hw:`

`app/audio.py` liefert Geräte-Kennungen bewusst als `plughw:<card>,<device>`,
nicht als rohes `hw:<card>,<device>` - das ist die Lehre aus einem echten
Vorfall: ein Test-Headset (Yealink WH62) unterstützt hardwareseitig nur
Sample-Raten von 8-32 kHz. Die Konfiguration verlangte 48 kHz. Ein rohes
`hw:`-Gerät verweigert eine nicht unterstützte Rate nicht mit einem Fehler,
sondern klemmt sie still auf die nächstliegende Hardware-Rate herunter
(bestätigt mit `arecord`: "Warning: rate is not accurate ... got = 32000Hz").
ffmpeg fragt diese tatsächlich ausgehandelte Rate aber nicht zurück und
deklariert im AAC-Stream weiterhin die angeforderten 48 kHz. Ergebnis: die
SRT-Verbindung stand technisch fehlerfrei, das Audio kam aber verzerrt/mit
falscher Geschwindigkeit an - bei LiveVoice praktisch als "kein Ton" sichtbar,
obwohl auf dem Pi alles unauffällig aussah (kein Fehler im Log, Stream lief).

`plughw:` legt eine ALSA-Software-Konvertierungsschicht über das Gerät, die
Sample-Rate/Format/Kanalzahl transparent an das anpasst, was tatsächlich
angefragt wird - unabhängig davon, was die gerade angeschlossene Hardware
nativ unterstützt. Das macht die App robust gegenüber jedem künftigen
Audio-Interface, nicht nur diesem einen Test-Headset. Verifiziert per
`arecord -D plughw:X,Y -r 48000`: keine Warnung mehr, korrekt 48000 Hz.

Zusätzlich verifiziert per SRT-Loopback-Test direkt auf dem Pi (ein
ffmpeg als lokaler SRT-Listener, ein zweites ffmpeg mit denselben
Parametern wie der echte Stream als Sender): das empfangene MPEG-TS enthält
einen validen, korrekt deklarierten AAC-Stream mit echtem, schwankendem
Pegel (`volumedetect`) - die komplette Kette Mikrofon -> ALSA -> AAC ->
SRT ist damit als technisch korrekt bestätigt.

### Eingangspegel-Meter: läuft dauerhaft mit

Ein eigenständiger, leichter ffmpeg-Prozess (`LevelMeter` in
`app/levelmeter.py`, `astats`+`ametadata`-Filterkette, druckt den
Peak-Pegel laufend auf stdout, Audiodaten selbst werden verworfen) misst den
Eingangspegel. Anfangs öffnete er dafür das rohe `plughw:`-Gerät exklusiv
und musste darum pausieren, sobald Stream oder Recording liefen (USB-Audio-
Interfaces erlauben in aller Regel keinen gleichzeitigen Zugriff von zwei
Prozessen auf dasselbe Gerät - ein zweiter `ffmpeg -f alsa -i <device>`
bricht sonst mit "Device or resource busy" ab).

Seit auch Recording über den geteilten `dsnoop`-Alias `SHARED_CAPTURE_DEVICE`
liest (siehe nächster Abschnitt), liest die Pegel-Messung ebenfalls darüber
statt über `plughw:` - `dsnoop` erlaubt beliebig vielen Lesern gleichzeitigen
Zugriff. Die Anzeige läuft dadurch permanent, auch während Stream **und**
Recording aktiv sind, ohne "Device or resource busy". Der Pegel-Filter läuft
weiterhin **nicht** zusätzlich in der Streaming-Kommandozeile selbst
(Backpressure-Risiko: schreibt ffmpeg mehrmals pro Sekunde auf stdout und
liest der Python-Thread das auch nur kurz nicht schnell genug ab, kann
ffmpeg beim Schreiben blockieren - das darf die Live-Audio-Pipeline nie
riskieren), sondern bleibt ein komplett eigenständiger Prozess.

**Pro-Kanal statt nur ein kombinierter Wert:** `astats` misst standardmässig
sowohl pro Kanal (`lavfi.astats.1.Peak_level`, `.2.` bei Stereo) als auch
kombiniert (`lavfi.astats.Overall.Peak_level`). `build_level_filter()`
schränkt das bewusst über `measure_perchannel=Peak_level:measure_overall=none`
auf genau die eine gebrauchte Kennzahl pro Kanal ein (sonst würde jede
Metadaten-Zeile alle ~15 astats-Kennzahlen mal Kanal 1/2/Overall enthalten -
unnötig aufgebläht). Das WebUI zeigt bei Stereo dadurch zwei eigene
Balken/dB-Werte (L/R) statt eines einzelnen kombinierten Peaks - `/api/
audio-level` liefert dafür `channel_peaks_db` als Liste (Index 0 = Kanal 1/L,
Index 1 = Kanal 2/R bei Stereo).

**Stolperstein (live auf dem Pi gefunden):** `ametadata=print` puffert seine
Ausgabe intern, sobald das Ziel eine Pipe statt ein Terminal ist - ohne die
Filter-Option `direct=1` kam beim Lesen über
`subprocess.Popen(..., stdout=PIPE)` schlicht **nie** etwas an, bis der
Prozess beendet wurde (ein `stdbuf -oL`-Wrapper hilft hier NICHT, da es sich
um ffmpegs eigene avio-Pufferung handelt, nicht um libc-stdio-Pufferung).
`build_level_filter()` in `app/levelmeter.py` setzt `direct=1` deshalb fest.

### Recording: geteilter Audio-Zugriff (`dsnoop`)

Das Eingangssignal soll parallel zum Stream verlustfrei als WAV auf einen
USB-Stick aufgezeichnet werden können - unabhängig vom Stream start-/
stoppbar, damit ein Netzwerk-Aussetzer beim Streaming (siehe "Bekannte
Grenzen") nicht auch die Aufnahme abreissen lässt. Da ein USB-Audio-
Interface aber nur einem Prozess gleichzeitigen direkten Zugriff erlaubt
(siehe oben), lesen Stream- **und** Recording-ffmpeg stattdessen über einen
gemeinsamen ALSA-`dsnoop`-Alias (`livevoice_capture` - Konstante
`SHARED_CAPTURE_DEVICE` in `app/config.py`). `dsnoop` selbst öffnet die
Hardware genau einmal exklusiv und verteilt den Audio-Strom intern an
beliebig viele Leser; ein äusserer `plug`-Wrapper sorgt dafür, dass Stream
und Recording trotzdem unabhängig voneinander die in den Einstellungen
gewählte Kanalzahl/Sample-Rate anfordern können (analog zu `plughw:` bei der
Pegel-Messung).

**Automatisch generiert, kein manuelles Editieren mehr nötig:**
`app/audio.py::write_dsnoop_config` schreibt die ALSA-Konfiguration für
diesen Alias in die private `~/.asoundrc` des Dienstbenutzers
(`/opt/livevoice-streamer/.asoundrc`, kein root/sudo/Polkit nötig - der
Dienst besitzt sein eigenes Home-Verzeichnis bereits), passend zur
ALSA-Kurzkennung (`card_id` aus `arecord -l`) des aktuell in den
Einstellungen gewählten Audio-Geräts. `~/.asoundrc` wird von ALSA nach einem
eventuell vorhandenen system-weiten `/etc/asound.conf` gelesen und
überschreibt darin definierte PCMs mit demselben Namen (siehe DEPLOY.md).
`AppState.sync_dsnoop_config()` ruft das sowohl bei jedem Dienst-Start als
auch nach jedem Speichern der Einstellungen auf - ein anderes Interface
anschliessen, in den Einstellungen unter Audio "Neu suchen" tippen, das
neue Gerät wählen, speichern: fertig, kein SSH mehr nötig.

Bewusste Konsequenz dieser Entscheidung: `app/streamer.py::build_ffmpeg_cmd`
liest **immer** über `SHARED_CAPTURE_DEVICE`, nicht über das in den
Einstellungen gewählte `plughw:`-Gerät direkt - so muss der laufende Stream
nie neu gestartet werden, nur weil währenddessen eine Aufnahme gestartet
oder gestoppt wird. Dasselbe gilt für die Pegel-Messung (siehe vorheriger
Abschnitt). `cfg.audio_device` bleibt trotzdem Pflichtfeld und wird
weiterhin zur Anzeige im WebUI genutzt.

Recording läuft als eigener Prozess (`RecordingManager` in
`app/recorder.py`, strukturell identisch zu `StreamManager`), mit eigenem
Start/Stop-Button auf der Cockpit-Seite - siehe "WebUI im Detail". Das
Ziel (Mountpoint eines USB-Sticks) wird in den Einstellungen gewählt;
Erkennung, Einhängen, Auswerfen und Formatieren des Sticks übernimmt
`app/storage.py` über `lsblk`/`udisksctl`/`busctl` (siehe DEPLOY.md für die
dafür nötige Polkit-Freigabe). Der Dateiname jeder Aufnahme setzt sich aus
einem in den Einstellungen frei wählbaren Präfix (Standard "Aufnahme") sowie
Datum/Startzeit zusammen, z.B. `Aufnahme-20260910-193743.wav`
(`recording_filename` in `app/recorder.py`).

## Projektstruktur

```text
LiveVoice-Pi-Streamer/
├─ app/
│  ├─ __init__.py       - App-Factory, Logging-Setup, Secret-Key für Sessions,
│  │                      startet/überwacht die Pegel-Messung
│  ├─ routes.py         - alle Flask-Routen (Seiten + JSON-API)
│  ├─ config.py         - StreamerConfig (Dataclass), Laden/Speichern als JSON
│  ├─ streamer.py       - ffmpeg-Kommandozeile + Prozessverwaltung (StreamManager)
│  ├─ recorder.py       - ffmpeg-Kommandozeile + Prozessverwaltung fürs Recording (RecordingManager)
│  ├─ levelmeter.py     - Eingangspegel-Messung (eigenständig, läuft dauerhaft mit)
│  ├─ audio.py          - USB-Audiogeräte erkennen (`arecord -l`) + geteilten
│  │                      Capture-Alias fürs jeweils gewählte Gerät schreiben
│  ├─ storage.py        - USB-Sticks erkennen/einhängen/auswerfen/formatieren (`lsblk`/`udisksctl`/`busctl`)
│  ├─ wifi.py           - NetworkManager-Anbindung (WLAN + LAN-Status, DHCP/feste IP)
│  ├─ system.py         - CPU-Last/RAM/SD-Karte/Temperatur (`/proc`, `/sys`) + Herunterfahren/Neustart
│  ├─ logs.py           - systemd-Journal-Auszug für den Log-Download
│  ├─ static/
│  │  ├─ style.css      - ein Stylesheet für alle Seiten, CSS-Variablen fürs Theme
│  │  ├─ app.js         - ein Script für alle Seiten, per Element-ID/Klasse geschützt
│  │  └─ brand/         - Logo/Icon-Set (siehe "Marke / Icon")
│  └─ templates/
│     ├─ base.html      - Kopfzeile/Nav, wird von index/settings erweitert
│     ├─ index.html     - Cockpit-Seite ("/", Pegel + Stream + Aufnahme)
│     ├─ settings.html  - Einstellungen ("/settings")
│     └─ login.html     - eigenständige Login-Seite (erweitert base.html nicht)
├─ tests/                - pytest, siehe "Entwicklung"
├─ instance/
│  └─ config.example.json - Beispiel/Vorlage, echte config.json ist gitignored
├─ systemd/
│  └─ livevoice-streamer.service
├─ wsgi.py               - startet waitress programmatisch (Port aus der Config)
├─ conftest.py            - leer, sorgt nur dafür dass `app/` beim Testen importierbar ist
├─ requirements.txt / requirements-dev.txt
├─ DEPLOY.md              - Schritt-für-Schritt-Anleitung fürs Aufsetzen auf dem Pi
└─ README.md              - dieses Dokument
```

## WebUI im Detail

Hell/Dunkel-Theme umschaltbar (Kreis-Symbol oben rechts, folgt standardmässig
der Systemeinstellung, Wahl wird im Browser gemerkt - `localStorage`, pro
Gerät/Browser).

### Login (`/login`)

Erscheint nur, wenn in den Einstellungen ein WebUI-Passwort gesetzt ist.
Eigenes HTML-Formular mit Logo statt des generischen
Browser-Basic-Auth-Popups; Passwortfeld mit eigenem Anzeigen/Verbergen-Knopf
(👁) statt der uneinheitlichen nativen Browser-Reveal-Icons. Anmeldung läuft
über eine serverseitig signierte Session (30 Tage gültig, persistiert über
Neustarts über `instance/secret_key`) - kein HTTP Basic Auth mehr. Abmelden
über den Knopf oben in den Einstellungen (nur sichtbar, wenn ein Passwort
gesetzt ist).

### Cockpit (`/`)

Ganz oben eine eigene Karte mit dem **Eingangspegel-Meter** (Peak-Pegel in
dB, Farbverlauf grün/gelb/rot) - bewusst vor Stream und Aufnahme platziert,
da er zu beidem gehört und dauerhaft läuft, auch während Stream und/oder
Aufnahme aktiv sind (Details siehe "Eingangspegel-Meter: läuft dauerhaft
mit" weiter oben). Bei Stereo (Einstellungen -> Audio -> Kanäle) zwei
eigene, mit "L"/"R" beschriftete Balken statt eines einzelnen kombinierten
Werts - bei Mono bleibt es bei einer Zeile ("Eingangspegel").

Darunter der **Stream**-Block: grosser Start/Stop-Button, Live-Status
(Laufzeit, Bitrate, Sample-Rate, Gerät, Fehler). Bekannte Fehlerbilder
(Import bei LiveVoice noch nicht gestartet bzw. von LiveVoice-Seite
gestoppt) erscheinen als verständlicher Hinweistext statt rohem ffmpeg-Log
(siehe `app/streamer.py::_friendly_error`); jede Fehlermeldung lässt sich
über ein "×" daneben schliessen, ohne den Stream neu starten zu müssen.

Danach ein eigener **Aufnahme**-Block mit eigenem Start/Stop-Button und
eigenem Live-Status (Laufzeit, Ziel-Verzeichnis, aktuelle Datei, Fehler,
ebenfalls schliessbar) - unabhängig vom Stream-Button, siehe "Recording:
geteilter Audio-Zugriff" weiter oben. Ohne gewähltes Aufnahme-Ziel bleibt
der Button deaktiviert, mit Hinweis auf die Einstellungen.

Danach ein Button zum Öffnen des LiveVoice-Operators in
einem neuen Tab - der einzige Weg, da LiveVoice das Einbetten serverseitig
blockiert (`X-Frame-Options: SAMEORIGIN`, mit `curl` verifiziert - kein
client-seitiger Trick möglich). Der Operator-Bereich lässt sich in den
Einstellungen ganz ausblenden, falls nicht gebraucht.

### Einstellungen (`/settings`, Zahnrad-Icon oben)

"Speichern" liegt oben rechts (per HTML5 `form`-Attribut mit dem
weiter unten stehenden Formular verknüpft, kein Duplikat) - links davon
erscheint bei gesetztem Passwort "Jetzt abmelden".

- **System** (erste Karte, ganz oben - vor "Verbindung"): CPU-Last,
  CPU-Temperatur, RAM- und SD-Karten-Belegung (`app/system.py`, liest
  `/proc`/`/sys` - kein `vcgencmd` nötig) als kompakte Stat-Kacheln in einer
  Reihe, rein informativ, aktualisiert sich alle 10 Sekunden. Jede Kachel
  zeigt einen kurzen Wert (z.B. "22 %"), die vollen Details (absolute
  MB/GB-Werte, 5-/15-Minuten-Last) stecken im Tooltip.
- **Verbindung**: Stream-URL (SRT) und Operator-URL, je mit eigenem Titel
  über einem vollbreiten Eingabefeld.
- **Audio**: Aufnahmegerät (Dropdown, per Knopf "Neu suchen" neu einlesbar,
  auf gleicher Zeile), Kanäle, Sample-Rate, Bitrate.
- **Aufnahme**: Ziel-USB-Stick (Dropdown, per Knopf "Neu suchen" neu
  einlesbar), "Einhängen" für einen erkannten, aber noch nicht gemounteten
  Stick, "Sicher auswerfen" vor dem physischen Abziehen (verhindert
  Datenverlust an der zuletzt geschriebenen Aufnahme), "Formatieren" (fragt
  vorher per Browser-`confirm()` nach, löscht alle Daten auf dem Stick
  unwiderruflich und legt ihn neu als exFAT an - siehe
  `app/storage.py::format_drive`). Dateiname-Präfix frei wählbar (Standard
  "Aufnahme", z.B. `Aufnahme-20260910-193743.wav`, Vorschau im Hinweistext
  aktualisiert sich live beim Tippen statt erst nach dem Speichern);
  Aufnahmeformat selbst ist fest WAV (unkomprimiert), nicht konfigurierbar -
  Kanäle (Mono/Stereo) und Sample-Rate werden direkt unter "Audio" oben
  angezeigt/eingestellt, da Recording **und** Stream dieselben Werte nutzen
  (kein separates Downmixing des Streams auf Mono, siehe "Recording:
  geteilter Audio-Zugriff").
- **Netzwerk**:
  - Port unter "Erweitert": 80 (Standard) oder frei ab 1024 - jede Änderung
    startet den Dienst kurz neu (unvermeidbar, um den neuen Port zu binden).
  - Status: alle aktiven Verbindungen (WLAN **und** LAN gleichzeitig
    möglich), pro Verbindung IP-Adresse/Prefix, Gateway, DNS und (bei DHCP)
    der DHCP-Server, sowie ein Umschalter DHCP <-> Feste IP mit Validierung
    und deutlicher Warnung (Risiko: falsche Werte trennen das Gerät vom
    Netz, dann nur noch direkt am Gerät behebbar).
  - Gespeicherte WLANs (mit "Entfernen"): technische `netplan-`-Präfixe in
    Verbindungsnamen werden für die Anzeige entfernt (`netplan-wlan0-FOO` ->
    `FOO`, `netplan-eth0` -> `eth0`), der echte Name bleibt für nmcli intern
    erhalten.
  - WLAN-Suche mit Signalstärke-Anzeige und Verbinden-Dialog. Firmen-/
    Enterprise-WLANs (802.1X, brauchen Benutzername + Zertifikat) werden
    nicht angezeigt - bewusst nicht unterstützt.
  - Läuft über NetworkManager (`nmcli`) - Standard-Netzwerkbackend auf
    Raspberry Pi OS seit Bookworm.
- **Weitere Einstellungen** (eingeklappt):
  - *Anzeige*: Operator-Bereich auf der Cockpit-Seite ausblenden.
  - *Zugriff*: WebUI-Passwort (mit Anzeigen/Verbergen-Knopf), Passwort
    entfernen.
  - *Diagnose*: Log-Level (DEBUG/INFO/WARNING/ERROR, wirkt sofort ohne
    Neustart) und "Log herunterladen" (systemd-Journal des Diensts als
    Textdatei - Start/Stop, Fehler, ffmpeg-Meldungen, WLAN-Ereignisse).

**Power** (Neustart/Herunterfahren) liegt bewusst nicht in einer Karte,
sondern als zwei Icon-Knöpfe (↻/⏻, mit Tooltip) oben im Header - erscheinen
nur, während man sich auf der Einstellungen-Seite befindet (siehe
`base.html`). Beide fragen vorher per Browser-`confirm()` nach; ein
laufender Stream/eine laufende Aufnahme wird automatisch zuerst gestoppt
(u.a. damit die WAV-Datei korrekt finalisiert wird) - sauber statt am
Eventende den Stecker zu ziehen (vermeidet SD-Karten-Korruption). Läuft über
`systemctl poweroff`/`systemctl reboot`, braucht dafür eine eigene
Polkit-Freigabe für den Dienstbenutzer (siehe DEPLOY.md).

## Entwicklung

Lokale Entwicklung ist nur auf Linux mit ffmpeg + ALSA sinnvoll (die
`-f alsa`-Eingabe ist Linux-spezifisch); auf anderen Systemen lässt sich der
Code trotzdem lesen/testen, nur echtes Streamen funktioniert nicht.

```sh
python3 -m venv venv
venv/bin/pip install -r requirements-dev.txt
venv/bin/pytest
venv/bin/python wsgi.py   # WebUI unter http://localhost:80 (ggf. sudo oder Port in instance/config.json anpassen)
```

Die Tests (`tests/`, aktuell rund 200) brauchen kein echtes ffmpeg, kein
echtes Audio-Interface, keinen echten USB-Stick und kein echtes `nmcli`:

- `build_ffmpeg_cmd`/`build_ffmpeg_record_cmd` (streamer.py/recorder.py)
  werden als reine Funktionen getestet.
- Der `StreamManager`/`RecordingManager`-Lebenszyklus (start/stop/
  Crash-Erkennung) läuft gegen einen Dummy-Kindprozess (`python -c "..."`)
  statt echtem ffmpeg.
- `arecord -l`/`lsblk`/`nmcli`-Parsing (audio.py, storage.py, wifi.py) wird
  anhand gespeicherter Beispielausgaben getestet, `subprocess.run` wird
  dafür mit `monkeypatch` ersetzt.
- Die Flask-Routen (`test_routes.py`) laufen über Flask's Test-Client,
  inklusive Login/Logout-Flow, Port-Wechsel-Logik (mit gemocktem
  Selbst-Restart), Recording-Lifecycle sowie WLAN/Netzwerk- und
  USB-Stick-Endpunkten (mit gemockten `wifi`-/`storage`-Modulen).

Alle Quelldateien liegen ausschliesslich in diesem Projektordner - auf dem
Pi selbst liegt nur eine Kopie davon (siehe DEPLOY.md), plus die dort
generierte `instance/config.json` und `instance/secret_key`, die bewusst
nicht Teil des Projekts sind (gitignored, geräte-/installationsspezifisch).
Ein Redeploy auf einen neu geflashten Pi braucht also nur dieses
Projektverzeichnis plus DEPLOY.md.

## Deployment

Siehe [DEPLOY.md](./DEPLOY.md) - Flashen, SSH-Zugang, Systempakete,
Polkit-Regel fürs WLAN, geteilter Audio-Zugriff fürs Recording (`dsnoop`) +
USB-Stick-Verwaltung (udisks2/Polkit), Dienstbenutzer, systemd-Service (inkl.
Port-80-Berechtigung und Journal-Leserecht fürs Log-Download),
Erstkonfiguration, iPad-Nutzung (Add-to-Homescreen), Update/Redeploy.

## Sicherheit / Risiken

- `instance/config.json` enthält die SRT-Passphrase im Klartext,
  `instance/secret_key` signiert die Login-Sessions - beide Dateien bleiben
  lokal auf dem Pi, sind in `.gitignore` und dürfen nicht committed werden.
- Login läuft über eine serverseitig signierte Session (kein HTTP Basic
  Auth mehr) - ohne TLS ist das Passwort beim Login trotzdem im Klartext im
  Netz sichtbar. Für den Einsatz im vertrauenswürdigen Event-LAN gedacht,
  nicht fürs offene Internet.
- Die Polkit-Regel für WLAN-Verwaltung (siehe DEPLOY.md) erlaubt dem
  Service-User volle NetworkManager-Kontrolle ohne Passwortabfrage - bewusst
  auf diesen einen dienstlichen Systembenutzer (keine Login-Shell)
  beschränkt, nicht auf `pi`/den eigenen Account.
- Die `CAP_NET_BIND_SERVICE`-Fähigkeit (siehe DEPLOY.md/systemd-Unit) erlaubt
  dem Dienst, Port 80 ohne root zu binden - bewusst über
  systemd-Capabilities statt `setuid`/root; jeder andere erlaubte Port liegt
  ab 1024 und braucht diese Fähigkeit nicht.
- Die `systemd-journal`-Gruppenmitgliedschaft (siehe DEPLOY.md) erlaubt dem
  Dienst, seine eigenen Logs für den Download zu lesen - keine
  Schreibrechte, nur lesend auf das Journal.
- Die Polkit-Regel für udisks2 (siehe DEPLOY.md) erlaubt dem Service-User
  `rec` das Ein-/Aushängen/Formatieren von Wechseldatenträgern ohne
  Passwortabfrage - bewusst wie bei der NetworkManager-Regel auf diesen
  einen dienstlichen Systembenutzer beschränkt, nicht auf `pi`/den eigenen
  Account. Der Benutzer heisst `rec` statt `livevoice`, weil UDisks2 Sticks
  automatisch unter `/media/<Benutzername>/...` einhängt - `rec` ist dort
  als Ordnername selbsterklärender.
- Aufnahmen (`instance/`-fremd, liegen auf dem USB-Stick, nicht im
  Projektordner) enthalten das komplette, unkomprimierte Audiosignal des
  Events im Klartext - Zugriff auf den Stick ist gleichbedeutend mit Zugriff
  auf den vollständigen Ton der Veranstaltung.
- Feste IP setzen (Netzwerk-Sektion) kann das Gerät bei falschen Werten vom
  Netz trennen - die WebUI selbst validiert Adresse/Gateway/DNS als
  IPv4-Zahlenformat, kann aber nicht prüfen, ob die Werte im jeweiligen
  Zielnetz tatsächlich sinnvoll/erreichbar sind.
- Die Polkit-Regel für logind (siehe DEPLOY.md) erlaubt dem Service-User
  `rec` das Herunterfahren/Neustarten des ganzen Geräts ohne Passwortabfrage
  - bewusst wie bei den Regeln für NetworkManager/udisks2 auf diesen einen
  dienstlichen Systembenutzer beschränkt. Jeder, der Zugriff auf das WebUI
  hat (bzw. bei gesetztem Passwort: eingeloggt ist), kann den Pi damit
  jederzeit ausschalten - eingedämmt durch das optionale WebUI-Passwort,
  nicht durch eine zusätzliche Bestätigung auf Betriebssystemebene.

## Bekannte Grenzen

- Kein IPv6.
- Keine Unterstützung für Enterprise-WLANs (802.1X) - bewusste
  Scope-Entscheidung, siehe oben.
- Der Versuch, die LiveVoice-Operator-Seite einzubetten, wurde probiert und
  verworfen - LiveVoice blockiert das serverseitig (`X-Frame-Options`), ein
  echter Fix bräuchte einen Reverse-Proxy für die komplette LiveVoice-Seite
  (fragil, hoher Wartungsaufwand) oder eine eigene Steuerung über die
  LiveVoice-API/ActionCable (bewusst nicht gewählt, um das Projekt einfach
  zu halten - siehe stattdessen das separate
  [Companion-Modul](../LiveVoice-Companion-Module/) für diesen Ansatz).
- Recording ist bewusst auf das Nötigste beschränkt: festes Format (WAV,
  unkomprimiert), eine Datei pro Aufnahme-Lauf (Start bis Stop), kein
  Datei-Browser/Download/Löschen im WebUI - Dateien liegen einfach im
  gewählten Zielverzeichnis auf dem Stick und werden wie gewohnt am PC
  weiterverarbeitet.
- Speicherplatz-Warnung auf dem Cockpit basiert auf der geschätzten
  Restlaufzeit (Warnfarbe unter 60 bzw. 15 Minuten bei aktueller
  WAV-Konfiguration), nicht auf freien Bytes absolut - bei einer nachträglich
  stark geänderten Sample-Rate/Kanalzahl kann die alte Faustregel kurz
  ungenau sein, bis der nächste Poll (2 s) die neue Rate berücksichtigt.

## Offene Punkte / Ideen

Aktuell keine offenen Punkte dokumentiert. Die zuvor hier gelisteten Ideen
(Powermanagement, USB-Speicherplatz auf dem Cockpit, Pi-Systemauslastung)
sind umgesetzt - siehe "WebUI im Detail" (Cockpit; Einstellungen → System
ganz oben; Power-Icons im Header) und DEPLOY.md (Polkit-Regel fürs
Herunterfahren/Neustarten).
