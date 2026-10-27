"""Pi-Systemzustand (CPU-Last, RAM, SD-Karte, Temperatur) und Power-Management.

Liest ausschliesslich aus dem `/proc`- und `/sys`-Pseudo-Dateisystem statt
externe Tools wie `vcgencmd` aufzurufen - das funktioniert ohne
Zusatzpakete/-rechte auf jedem Linux, nicht nur auf einem Raspberry Pi mit
`vcgencmd` im `video`-Gruppenzugriff.

Herunterfahren/Neustart laufen über `systemctl poweroff`/`systemctl reboot`,
die intern logind (`org.freedesktop.login1.*`) per D-Bus ansprechen - der
Dienstbenutzer braucht dafür eine Polkit-Freigabe (siehe
`systemd/50-livevoice-logind.rules`, analog zu den bestehenden Regeln für
NetworkManager/udisks2 in DEPLOY.md).
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

_LOADAVG_PATH = Path("/proc/loadavg")
_MEMINFO_PATH = Path("/proc/meminfo")
_THERMAL_ZONE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")

# Aufnahme ist fest S16_LE (siehe app/recorder.py::build_ffmpeg_record_cmd) -
# 2 Byte je Sample und Kanal, vor einer allfälligen Kompression (FLAC).
_BYTES_PER_SAMPLE = 2


@dataclass
class MemoryInfo:
    total_bytes: int
    available_bytes: int

    @property
    def used_bytes(self) -> int:
        return max(self.total_bytes - self.available_bytes, 0)

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.total_bytes) * 100


@dataclass
class DiskInfo:
    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return (self.used_bytes / self.total_bytes) * 100


def load_average(path: Path = _LOADAVG_PATH) -> tuple[float, float, float] | None:
    """1-/5-/15-Minuten-Last aus /proc/loadavg (erste drei Felder)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parts = raw.split()
    if len(parts) < 3:
        return None
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except ValueError:
        return None


def memory_usage(path: Path = _MEMINFO_PATH) -> MemoryInfo | None:
    """`MemAvailable` (nicht `MemFree`) ist der realistische freie Wert -
    berücksichtigt reclaimable Caches/Buffers, die der Kernel bei Bedarf
    sofort wieder freigibt (anders als `MemFree`, das Caches als "belegt"
    zählen würde und damit ständig fälschlich knapp aussieht)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, int] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        rest = rest.strip()
        if rest.endswith("kB"):
            try:
                values[key.strip()] = int(rest[:-2].strip()) * 1024
            except ValueError:
                continue
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return MemoryInfo(total_bytes=total, available_bytes=available)


def disk_usage(path: str = "/") -> DiskInfo | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return DiskInfo(total_bytes=usage.total, used_bytes=usage.used, free_bytes=usage.free)


def cpu_temperature_celsius(path: Path = _THERMAL_ZONE_PATH) -> float | None:
    """Liest die SoC-Temperatur direkt aus dem Kernel-Thermalzonen-Sysfs
    (Millidegrees Celsius als reine Zahl) - auf jedem Raspberry Pi OS ohne
    Zusatzpaket vorhanden, anders als `vcgencmd` (braucht `video`-Gruppe)."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        millidegrees = int(raw)
    except ValueError:
        return None
    return millidegrees / 1000.0


def estimate_recording_seconds(free_bytes: int, channels: int, sample_rate: int, size_factor: float = 1.0) -> float:
    """Grobe Restlaufzeit-Schätzung fürs Aufnahme-Ziel auf dem Cockpit:
    freier Speicher / Bytes-pro-Sekunde bei der aktuell konfigurierten
    Aufnahme. `size_factor` ist das Grössenverhältnis des Formats zu
    unkomprimiertem PCM (siehe app/recorder.py::RECORDING_FORMATS)."""
    bytes_per_second = channels * sample_rate * _BYTES_PER_SAMPLE * size_factor
    if bytes_per_second <= 0 or free_bytes <= 0:
        return 0.0
    return free_bytes / bytes_per_second


# RFC 1123: Buchstaben, Ziffern, Bindestrich, max. 63 Zeichen, nicht mit
# Bindestrich beginnend/endend - so ist der Name auch als `<name>.local`
# (mDNS/Bonjour) und im DNS des Routers gültig.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def current_hostname() -> str:
    return socket.gethostname()


def validate_hostname(name: str) -> str | None:
    """Gibt eine Fehlermeldung zurück oder None, wenn der Name gültig ist."""
    if not _HOSTNAME_RE.match(name):
        return (
            "Gerätename darf nur Buchstaben, Ziffern und Bindestriche enthalten "
            "(max. 63 Zeichen, nicht mit Bindestrich beginnen/enden)."
        )
    return None


def set_hostname(name: str, timeout: float = 10.0) -> tuple[bool, str | None]:
    """Setzt den Gerätenamen über `hostnamectl` (systemd-hostnamed per
    D-Bus) - braucht die Polkit-Freigabe aus
    `systemd/50-livevoice-hostname.rules`. Die Pfad-Unit
    `livevoice-hostname-sync.path` zieht danach /etc/hosts nach und startet
    avahi neu, damit `<name>.local` sofort unter dem neuen Namen auftaucht
    (siehe DEPLOY.md)."""
    error = validate_hostname(name)
    if error:
        return False, error
    try:
        result = subprocess.run(
            ["hostnamectl", "set-hostname", name],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Gerätename konnte nicht gesetzt werden: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or "Gerätename konnte nicht gesetzt werden."
    return True, None


def _run_power_action(cmd: list[str], timeout: float, label: str) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{label} fehlgeschlagen: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, detail or f"{label} fehlgeschlagen."
    return True, None


def poweroff(timeout: float = 10.0) -> tuple[bool, str | None]:
    """Sauberes Herunterfahren statt Stecker ziehen - vermeidet SD-Karten-
    Korruption bei einer gerade laufenden Aufnahme/einem Schreibvorgang.
    Braucht die Polkit-Freigabe aus `systemd/50-livevoice-logind.rules`."""
    return _run_power_action(["systemctl", "poweroff"], timeout, "Herunterfahren")


def reboot(timeout: float = 10.0) -> tuple[bool, str | None]:
    return _run_power_action(["systemctl", "reboot"], timeout, "Neustart")
