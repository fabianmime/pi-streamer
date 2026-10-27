#!/bin/sh
# Wird als root von livevoice-hostname-sync.service ausgeführt, sobald sich
# /etc/hostname ändert (Gerätename im WebUI geändert, siehe app/system.py).
# Installiert nach /usr/local/sbin/livevoice-hostname-sync (siehe DEPLOY.md).
set -eu

name=$(tr -d '[:space:]' < /etc/hostname)
[ -n "$name" ] || exit 0

# 127.0.1.1-Zeile auf den neuen Namen setzen - sonst meldet sudo danach
# "unable to resolve host" und wird spürbar langsam.
if grep -q '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
    sed -i "s/^127\.0\.1\.1[[:space:]].*/127.0.1.1\t${name}/" /etc/hosts
else
    printf '127.0.1.1\t%s\n' "$name" >> /etc/hosts
fi

# avahi übernimmt Namensänderungen nicht von selbst (und SetHostName per
# D-Bus ist für Nicht-root gesperrt) - neu starten, damit <name>.local
# sofort unter dem neuen Namen angekündigt wird.
systemctl try-restart avahi-daemon.service
