"""Erkennung und Verwaltung von USB-Sticks als Recording-Ziel.

Nutzt `lsblk` (Teil von util-linux, immer vorhanden) zum Auflisten und
`udisksctl` (Teil von udisks2, siehe DEPLOY.md) zum Mounten/Auswerfen ohne
Desktop-Session - auf einem headless Pi OS Lite gibt es keinen
automatischen Autofs-Mount wie auf einem Desktop-System, `udisksctl`
funktioniert aber auch für einen Systembenutzer ohne aktive Login-Session
(dank der Polkit-Regel aus DEPLOY.md).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class UsbDrive:
    device: str  # z.B. "/dev/sda1"
    label: str
    size_bytes: int | None
    mountpoint: str | None

    @property
    def display_label(self) -> str:
        return self.label or self.device

    @property
    def mounted(self) -> bool:
        return bool(self.mountpoint)


def _collect_usb_partitions(node: dict, drives: list[UsbDrive], parent_usb: bool = False) -> None:
    is_usb = parent_usb or node.get("tran") == "usb"
    if node.get("type") == "part" and is_usb and node.get("fstype"):
        size_raw = node.get("size")
        size_bytes = int(size_raw) if isinstance(size_raw, (int, str)) and str(size_raw).isdigit() else None
        drives.append(
            UsbDrive(
                device=f"/dev/{node['name']}",
                label=(node.get("label") or "").strip(),
                size_bytes=size_bytes,
                mountpoint=node.get("mountpoint") or None,
            )
        )
    for child in node.get("children", []) or []:
        _collect_usb_partitions(child, drives, parent_usb=is_usb)


def parse_lsblk_output(raw_json: str) -> list[UsbDrive]:
    """Nur Partitionen (nicht ganze Laufwerke) mit USB-Transport und
    vorhandenem Dateisystem - eine unformatierte/leere Partition kann
    ohnehin nicht als Aufnahme-Ziel gemountet werden."""
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError:
        return []
    drives: list[UsbDrive] = []
    for blockdev in data.get("blockdevices", []):
        _collect_usb_partitions(blockdev, drives)
    return drives


def list_usb_drives(timeout: float = 5.0) -> list[UsbDrive]:
    """Gibt eine leere Liste zurück, wenn `lsblk` fehlt oder fehlschlägt,
    statt eine Exception zu werfen - die Settings-Seite soll auch ohne
    angeschlossenen USB-Stick benutzbar bleiben."""
    try:
        result = subprocess.run(
            ["lsblk", "-J", "-b", "-o", "NAME,LABEL,SIZE,MOUNTPOINT,RM,TRAN,FSTYPE,TYPE"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return parse_lsblk_output(result.stdout)


def mount_drive(device: str, timeout: float = 15.0) -> tuple[str | None, str | None]:
    """Mountet per udisksctl (kein Passwort nötig dank Polkit-Regel, siehe
    DEPLOY.md). Gibt (mountpoint, error) zurück."""
    try:
        result = subprocess.run(
            ["udisksctl", "mount", "-b", device],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"Einhängen fehlgeschlagen: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if "already mounted" in detail.lower():
            for drive in list_usb_drives():
                if drive.device == device and drive.mountpoint:
                    return drive.mountpoint, None
        return None, detail or "Einhängen fehlgeschlagen."

    # udisksctl beendet die Erfolgsmeldung mit einem Satzpunkt direkt hinter
    # dem Pfad ("Mounted /dev/sda1 at /media/x/STICK.") - der muss weg,
    # sonst landet er als Teil des Mountpoints in der Config.
    match = re.search(r"at (/\S+)", result.stdout)
    return (match.group(1).rstrip(".") if match else None), None


def unmount_drive(device: str, timeout: float = 15.0) -> tuple[bool, str | None]:
    """Hängt den Stick sauber aus - vor dem physischen Abziehen nutzen, um
    Datenverlust an der zuletzt geschriebenen Aufnahme zu vermeiden."""
    try:
        result = subprocess.run(
            ["udisksctl", "unmount", "-b", device],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Auswerfen fehlgeschlagen: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or "Auswerfen fehlgeschlagen."
    return True, None


def format_drive(device: str, fs_type: str = "exfat", timeout: float = 180.0) -> tuple[bool, str | None]:
    """Formatiert den Stick komplett neu - **löscht alle Daten darauf
    unwiderruflich**. Zielformat exFAT (kein 4-GB-Datei-Limit wie FAT32, das
    bei langen Aufnahmen relevant werden kann, und unter Windows/macOS ohne
    Zusatztreiber lesbar).

    Läuft über die UDisks2-D-Bus-Methode `Block.Format` per `busctl`, nicht
    über `udisksctl format` (siehe `mount_drive`/`unmount_drive`): die auf
    dem Pi installierte udisksctl-Version (2.10.1, Stand 2026-09) kennt
    diesen Unterbefehl schlicht nicht (`Unknown command 'format'`), die
    zugrundeliegende D-Bus-Methode existiert aber.

    Hängt ein gemountetes Ziel vorher explizit selbst aus (`unmount_drive`) -
    live verifiziert, dass `Block.Format` das **nicht** von sich aus tut wie
    ursprünglich angenommen: ohne den expliziten Unmount bricht der Aufruf
    mit "Failed to open the device ...: Device or resource busy" ab, wenn
    der Stick gerade als Aufnahme-Ziel eingehängt ist (der Normalfall). Ein
    Fehler beim Unmount-Versuch wird bewusst ignoriert (z.B. weil der Stick
    ohnehin schon ausgehängt ist) - schlägt das Aushängen aus einem echten
    Grund fehl, meldet der nachfolgende Format-Aufruf das dann selbst klar
    als "Device or resource busy".

    Braucht zusätzlich zur Mount/Unmount-Polkit-Freigabe die Aktion
    `org.freedesktop.udisks2.modify-device` für den Dienstbenutzer (siehe
    DEPLOY.md) - ohne die bricht der Aufruf mit "Not authorized" ab.
    """
    unmount_drive(device)
    object_path = "/org/freedesktop/UDisks2/block_devices/" + device.rsplit("/", 1)[-1]
    try:
        result = subprocess.run(
            [
                "busctl", "call",
                "org.freedesktop.UDisks2", object_path,
                "org.freedesktop.UDisks2.Block", "Format",
                "sa{sv}", fs_type, "0",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Formatieren fehlgeschlagen: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or "Formatieren fehlgeschlagen."
    return True, None


def free_space_bytes(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None
