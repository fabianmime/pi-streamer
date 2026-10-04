# Deployment auf dem Raspberry Pi

## 1. Pi flashen

1. [Raspberry Pi Imager](https://www.raspberrypi.com/software/) installieren.
2. Als OS **Raspberry Pi OS Lite (64-bit)** wählen (kein Desktop nötig -
   das Gerät läuft headless, Bedienung nur über das WebUI).
3. Im Imager unter dem Zahnrad (erweiterte Optionen) vorab konfigurieren:
   - Hostname: `livevoice-streamer`
   - Benutzername: `mpadmin` (gleiche Konvention wie bei den anderen privaten
     Pis von Fabian) **mit Passwort** - wird gleich noch für den SSH-Key-
     Transfer und den einmaligen `sudo`-Bootstrap gebraucht
   - SSH aktivieren -> **"Use password authentication"** (einfacher als den
     Key manuell ins Imager-Feld zu tippen - der Key wird gleich nach dem
     ersten Boot per `ssh-copy-id` übertragen)
   - WLAN-Zugangsdaten (falls kein LAN-Kabel am Eventort verfügbar ist)
   - Locale/Zeitzone
4. SD-Karte flashen, Pi damit booten.

## 2. SSH-Key übertragen + passwortloses sudo einrichten (einmalig, von dir selbst auszuführen)

Zwei Dinge müssen einmalig von dir selbst gemacht werden (eigenes Terminal
oder mit vorangestelltem `!` in dieser Session) - beide brauchen das gerade
gesetzte `mpadmin`-Passwort interaktiv, das darf nicht durch Claude Code
laufen:

**a) Public Key auf den Pi kopieren** (fragt einmal nach dem Passwort):

```sh
ssh-copy-id -i ~/.ssh/livevoice_streamer_ed25519.pub mpadmin@livevoice-streamer.local
```

Danach funktioniert `ssh livevoice-streamer` (der Alias aus `~/.ssh/config`)
per Public Key, ohne Passwort.

**b) Passwortloses sudo einrichten** - SSH läuft jetzt per Key, aber der
allererste `sudo`-Aufruf braucht trotzdem noch einmal das Account-Passwort,
und Claude Code blockt `sudo`-Befehle über SSH grundsätzlich hart, auch
nach Freigabe im Chat (gleiches Verhalten wie beim HA-Host, siehe
Projekt-Notizen):

```sh
ssh livevoice-streamer
```

Und dann in der geöffneten Session (fragt beim ersten `sudo` einmal nach dem
Passwort, die restlichen verketteten Aufrufe nutzen den sudo-Cache):

```sh
echo "mpadmin ALL=(ALL) NOPASSWD:ALL" | sudo tee /etc/sudoers.d/010-mpadmin-nopasswd > /dev/null && sudo chmod 0440 /etc/sudoers.d/010-mpadmin-nopasswd && sudo visudo -c
```

`sudo visudo -c` am Ende validiert alle sudoers-Dateien - sollte
`/etc/sudoers.d/010-mpadmin-nopasswd: parsed OK` (o.ä.) ausgeben.

Danach kann Claude Code die restlichen Schritte (3-9) direkt per
`ssh livevoice-streamer` ausführen, ohne dass du jedes Mal ein Passwort
eintippen musst.

## 3. Systempakete

```sh
sudo apt update
sudo apt install -y ffmpeg python3-venv python3-pip alsa-utils
```

USB-Audio-Interface anschliessen und prüfen, dass es erkannt wird:

```sh
arecord -l
```

Sollte ungefähr so aussehen:

```
**** Liste der Hardware-Geräte (CAPTURE) ****
card 1: Audio [Scarlett 2i2 USB], device 0: USB Audio [USB Audio]
```

Der relevante Wert für die Settings-Seite ist `plughw:1,0` (Card- und
Device-Nummer aus der Ausgabe, mit `plughw:`- statt `hw:`-Präfix, damit ALSA
Sample-Rate/Format automatisch an das jeweilige Interface anpasst statt bei
nicht unterstützten Werten still falsches Audio zu produzieren) - die App
liest und zeigt diese Liste aber ohnehin direkt im WebUI an, manuelles
Ablesen ist nur zur Fehlersuche nötig.

## 4. WLAN-Verwaltung erlauben (Polkit)

Raspberry Pi OS Lite (Bookworm und neuer) verwendet standardmässig
NetworkManager; `nmcli` ist bereits vorinstalliert, `network-manager` muss
also nicht nachinstalliert werden. Prüfen:

```sh
nmcli --version
systemctl is-active NetworkManager
```

Der Streamer-Dienst läuft als eigener Systembenutzer ohne Login-Session -
ohne weitere Freigabe würde NetworkManager WLAN-Änderungen von diesem
Benutzer per Polkit ablehnen. Deshalb eine Polkit-Regel anlegen, die genau
diesem Benutzer NetworkManager-Aktionen erlaubt:

```sh
sudo tee /etc/polkit-1/rules.d/50-livevoice-networkmanager.rules > /dev/null <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.NetworkManager.") === 0 &&
        subject.user == "rec") {
        return polkit.Result.YES;
    }
});
EOF
sudo systemctl restart polkit
```

(Der Systembenutzer heisst bewusst `rec`, nicht `livevoice` - sonst würde
UDisks2 USB-Sticks automatisch unter `/media/livevoice/...` einhängen, was
beim Aufnahme-Ziel in den Einstellungen verwirrend aussieht. Der Benutzer
wird erst im nächsten Schritt angelegt - die Regel kann trotzdem schon jetzt
hinterlegt werden, sie greift ab dem Moment, in dem es den Benutzer gibt.)

## 4b. Herunterfahren/Neustarten erlauben (Polkit)

Die "Neustart"/"Herunterfahren"-Icons im Header (nur sichtbar auf der
Einstellungen-Seite, siehe README) rufen `systemctl poweroff`/
`systemctl reboot` auf - das spricht intern `logind` per D-Bus an. Ohne
Freigabe lehnt `logind` das für den Dienstbenutzer `rec` ab, der keine
eigene Login-Session hat - analog zu den Polkit-Regeln für
NetworkManager/udisks2:

```sh
sudo tee /etc/polkit-1/rules.d/50-livevoice-logind.rules > /dev/null <<'EOF'
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.login1.power-off" ||
         action.id == "org.freedesktop.login1.power-off-multiple-sessions" ||
         action.id == "org.freedesktop.login1.reboot" ||
         action.id == "org.freedesktop.login1.reboot-multiple-sessions") &&
        subject.user == "rec") {
        return polkit.Result.YES;
    }
});
EOF
sudo systemctl restart polkit
```

Die `-multiple-sessions`-Varianten werden angefragt, wenn zusätzlich noch
eine andere Session aktiv ist (z.B. eine offene SSH-Sitzung von `mpadmin`) -
beide Varianten pauschal mit erlauben.

**Live getestet und funktionsfähig (Stand 2026-09-11):** Autorisierung per
`pkcheck --action-id org.freedesktop.login1.power-off --process <PID des
Diensts>` (als root, da `pkcheck` fremde UIDs nur root prüfen lässt)
verifiziert, ohne den Pi dafür tatsächlich herunterzufahren/neu zu starten.

## 5. Recording: geteilter Audio-Zugriff (dsnoop) + USB-Stick-Verwaltung

Für die parallele WAV-Aufnahme neben dem Stream (siehe README, Abschnitt
"Recording") braucht es zwei separate Einmal-Einrichtungen.

### a) ALSA-`dsnoop`-Alias für gleichzeitigen Zugriff

Ein USB-Audio-Interface erlaubt normalerweise nur einem Prozess gleichzeitigen
Zugriff (siehe README, "Eingangspegel-Meter: läuft dauerhaft mit"). Damit
Stream- und Recording-ffmpeg dasselbe Interface gleichzeitig lesen können,
teilt ALSAs `dsnoop`-Plugin einen einzelnen Hardware-Zugriff auf mehrere
Leser auf.

**Kein manueller Schritt mehr nötig:** `app/audio.py::write_dsnoop_config`
schreibt die dafür nötige ALSA-Konfiguration automatisch in `~/.asoundrc`
des Dienstbenutzers (`/opt/livevoice-streamer/.asoundrc`, kein root/sudo
nötig - der Dienst besitzt sein eigenes Home-Verzeichnis bereits), passend
zur ALSA-Kurzkennung (`card_id` aus `arecord -l`, z.B. `card 1: Audio
[Scarlett 2i2 USB]` → Kennung `Audio`) des aktuell in den Einstellungen
gewählten Audio-Geräts. `~/.asoundrc` wird von ALSA nach einem eventuell
vorhandenen `/etc/asound.conf` gelesen und überschreibt darin definierte
PCMs mit demselben Namen - ein anderes USB-Interface anschliessen,
in den Einstellungen unter Audio "Neu suchen" tippen, das neue Gerät wählen,
speichern - fertig, kein SSH/manuelles Editieren mehr nötig. Läuft
zusätzlich automatisch bei jedem Dienst-Start (App-Factory,
`AppState.sync_dsnoop_config`), damit ein Reboot mit zwischenzeitlich
gewechseltem Interface (sofern es noch an derselben `plughw:`-Position
hängt) korrekt bleibt.

Testen (kein echter Stream/keine echte Aufnahme nötig):

```sh
arecord -D livevoice_capture -d 2 -f S16_LE -r 48000 -c 2 /tmp/test.wav && aplay /tmp/test.wav
```

(Funktioniert erst, nachdem in den Einstellungen einmal ein Audio-Gerät
gewählt und gespeichert wurde - vorher existiert `~/.asoundrc` noch nicht.)

### b) udisks2 + Polkit-Regel fürs Ein-/Aushängen des USB-Sticks

Raspberry Pi OS Lite hat keine Desktop-Session, die USB-Sticks automatisch
mountet - das WebUI übernimmt das stattdessen selbst über `udisksctl` (Teil
von udisks2), was auch ohne Login-Session funktioniert:

```sh
sudo apt install -y udisks2 exfatprogs
```

(`exfatprogs` liefert `mkfs.exfat` - gebraucht fürs "Stick formatieren"-
Feature, siehe README, "Recording: geteilter Audio-Zugriff".)

Wie bei der WLAN-Verwaltung (siehe Schritt 4) braucht der Dienstbenutzer
`rec` (wird erst im nächsten Schritt angelegt - die Regel greift ab dem
Moment, in dem es den Benutzer gibt) eine Polkit-Freigabe, um Wechseldaten-
träger ohne Passwortabfrage ein-/aushängen bzw. formatieren zu können:

```sh
sudo tee /etc/polkit-1/rules.d/50-livevoice-udisks2.rules > /dev/null <<'EOF'
polkit.addRule(function(action, subject) {
    if (action.id.indexOf("org.freedesktop.udisks2.filesystem-mount") === 0 &&
        subject.user == "rec") {
        return polkit.Result.YES;
    }
    if (action.id == "org.freedesktop.udisks2.filesystem-unmount-others" &&
        subject.user == "rec") {
        return polkit.Result.YES;
    }
    // Fuers "Stick formatieren"-Feature (Block.Format per busctl, siehe
    // app/storage.py). Praefix-Vergleich statt exaktem "==", da UDisks2 je
    // nach Geraet auch "modify-device-system" oder "modify-device-other-seat"
    // anfragen kann, nicht nur das nackte "modify-device" (live auf dem Pi
    // festgestellt: mit exaktem "==" schlug das Formatieren mit "Not
    // authorized" fehl).
    if (action.id.indexOf("org.freedesktop.udisks2.modify-device") === 0 &&
        subject.user == "rec") {
        return polkit.Result.YES;
    }
});
EOF
sudo systemctl restart polkit
```

**Formatieren-Feature: live getestet und funktionsfähig (Stand 2026-09-10).**
Zwei Stolpersteine dabei gefunden und behoben:

- Die Polkit-Action-ID muss per Präfix verglichen werden (siehe oben) - ein
  exakter `==`-Vergleich auf `"org.freedesktop.udisks2.modify-device"` schlug
  mit "Not authorized" fehl.
- `Block.Format` hängt ein gemountetes Ziel **nicht** von selbst aus (anders
  als ursprünglich angenommen) - ohne expliziten Unmount vorher bricht der
  Aufruf mit "Device or resource busy" ab. `app/storage.py::format_drive()`
  hängt darum jetzt selbst zuerst aus.

Meldet das WebUI trotzdem noch "Not authorized" o.ä.: `journalctl -f`
während des Versuchs mitlaufen lassen, die dort protokollierte Action-ID mit
obiger Regel abgleichen und ggf. anpassen (gleiches Vorgehen wie beim
Mount/Unmount-Polkit-Troubleshooting weiter unten).

**Mount/Unmount: live getestet und funktionsfähig** (Stand 2026-09-10). Die
genauen Polkit-Action-IDs können je nach udisks2-Version dennoch leicht
abweichen. Meldet das WebUI beim Einhängen/Auswerfen "Not authorized" o.ä.:
`journalctl -f` während des Versuchs mitlaufen lassen, die dort
protokollierte Action-ID mit obiger Regel abgleichen und ggf. anpassen (zur
Fehlersuche testweise auch mit
`action.id.indexOf("org.freedesktop.udisks2.") === 0` als breiterer Treffer
möglich, danach aber wieder auf die genaue Action-ID einschränken).

## 6. Dienstbenutzer + Projekt kopieren

Der Benutzer heisst bewusst `rec`, nicht `livevoice` (bzw. wie der Projekt-
/Hostname) - UDisks2 hängt USB-Sticks automatisch unter `/media/<Benutzer-
name>/...` ein, und "rec" ist dort deutlich selbsterklärender als
"livevoice" als Ordnername im Dateimanager. Das Installationsverzeichnis
`/opt/livevoice-streamer` bleibt trotzdem beim Projektnamen:

```sh
sudo useradd --system --home /opt/livevoice-streamer --shell /usr/sbin/nologin rec
sudo usermod -aG audio rec
sudo usermod -aG systemd-journal rec
sudo mkdir -p /opt/livevoice-streamer
sudo chown rec:rec /opt/livevoice-streamer
```

(`systemd-journal` erlaubt dem Dienst, seine eigenen Logs für den
"Log herunterladen"-Knopf unter Einstellungen → Weitere Einstellungen zu
lesen - ohne diese Gruppe liefert `journalctl` einen Permission-Fehler.)

Projekt auf den Pi bringen (z. B. per `scp`/`rsync` von diesem Rechner aus,
oder per `git clone`, falls das Projekt in ein eigenes Repo ausgelagert
wird):

```sh
# Vom Entwicklungsrechner aus, Beispiel mit rsync:
rsync -av --exclude venv --exclude instance/config.json --exclude instance/secret_key \
  --exclude __pycache__ \
  "LiveVoice-Pi-Streamer/" livevoice-streamer:/tmp/livevoice-streamer/

# Auf dem Pi:
sudo rsync -av /tmp/livevoice-streamer/ /opt/livevoice-streamer/
sudo chown -R rec:rec /opt/livevoice-streamer
```

## 7. Python-Umgebung

```sh
cd /opt/livevoice-streamer
sudo -u rec python3 -m venv venv
sudo -u rec venv/bin/pip install -r requirements.txt
```

## 8. systemd-Service

Der Dienst läuft als unprivilegierter Benutzer, bindet aber standardmässig
Port 80 (< 1024, normalerweise nur mit root-Rechten möglich). Die
mitgelieferte Unit-Datei löst das sauber über eine gezielte
Linux-Capability (`CAP_NET_BIND_SERVICE`) statt über root/setuid:

```sh
sudo cp systemd/livevoice-streamer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now livevoice-streamer
sudo systemctl status livevoice-streamer
```

Logs bei Problemen:

```sh
journalctl -u livevoice-streamer -f
```

Wird der Port später über die Settings-Seite geändert, beendet sich der
Dienst absichtlich selbst - `Restart=always` in der Unit sorgt dafür, dass
systemd ihn mit dem neuen Port aus `instance/config.json` sofort neu startet
(ein reguläres `systemctl stop` ist davon nicht betroffen).

## 9. Erstkonfiguration

WebUI im Browser öffnen: `http://livevoice-streamer.local/settings`
(Standardport 80, daher keine `:80` in der URL nötig).

- **Stream-URL**: die SRT-Ziel-URL inkl. Passphrase/Streamid, wie sie bereits
  erfolgreich in OBS als SRT-Ausgabe verwendet wurde.
- **Operator-URL**: der LiveVoice-Operator-Link für das jeweilige Event.
- **Audio-Gerät**: aus dem Dropdown wählen (USB-Interface muss dafür
  angeschlossen sein - "Neu suchen" tippen, falls es nicht in der Liste ist).
- **Aufnahme-Ziel**: USB-Stick anschliessen, unter "Aufnahme" mit "Neu
  suchen" erkennen lassen und bei Bedarf über "Einhängen" aktivieren -
  danach als Ziel auswählen und speichern. Optional, nur nötig, wenn parallel
  zum Stream aufgezeichnet werden soll.
- Optional ein WebUI-Passwort setzen (unter "Weitere Einstellungen" ->
  Zugriff), wenn das Gerät in einem WLAN steht, in dem auch andere Personen
  (Publikum, Freiwillige) hängen. Danach zeigt jeder weitere Aufruf zuerst
  eine eigene Login-Seite (kein Browser-Popup).

Weitere Einstellungsmöglichkeiten (Log-Level, Log-Download, Operator
ausblenden, DHCP/feste IP) sind im README unter "WebUI im Detail"
beschrieben.

Falls der Pi am Eventort per WLAN statt LAN-Kabel angebunden wird: im
Abschnitt **Netzwerk** derselben Settings-Seite das Venue-WLAN suchen und
verbinden (siehe Schritt 4 für die nötige Polkit-Freigabe). Ist der Pi
gerade selbst per WLAN erreichbar, kann die Seite beim Verbindungswechsel
kurz nicht antworten - bei Erstkonfiguration daher wenn möglich per
LAN-Kabel arbeiten. Der WebUI-Port lässt sich dort ebenfalls (unter
"Erweitert") ändern - erlaubt ist 80 oder ein Port ab 1024 (z.B. 8080) -,
eine Änderung startet den Dienst kurz neu.

Danach auf der Startseite (`/`) den Stream starten und prüfen, dass er in
LiveVoice ankommt.

## 10. iPad-Nutzung

- Safari auf dem iPad öffnen: `http://livevoice-streamer.local`
- Teilen-Symbol -> "Zum Home-Bildschirm" - startet danach im
  Vollbildmodus ohne Safari-Chrome (die `apple-mobile-web-app-capable`
  Meta-Tags sind dafür bereits gesetzt).
- Der Operator wird über einen Button in einem neuen Tab geöffnet -
  LiveVoice blockiert das Einbetten serverseitig (`X-Frame-Options`), ein
  Versuch, es doch einzubetten, ist deshalb nicht Teil des WebUI. Der
  Operator-Bereich lässt sich in den Einstellungen unter "Weitere
  Einstellungen" bei Bedarf auch ganz von der Cockpit-Seite ausblenden.

## 11. Update / Redeploy

Bei Code-Änderungen: Schritt 6 (rsync) wiederholen, danach:

```sh
sudo -u rec /opt/livevoice-streamer/venv/bin/pip install -r /opt/livevoice-streamer/requirements.txt
sudo systemctl restart livevoice-streamer
```

Die Konfiguration (`instance/config.json`) bleibt dabei erhalten, solange sie
nicht mit überschrieben wird (deshalb im rsync-Beispiel oben explizit
ausgeschlossen).
