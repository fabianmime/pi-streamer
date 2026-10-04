"""Netzwerk-Verwaltung über NetworkManager (`nmcli`).

Raspberry Pi OS verwendet seit Bookworm NetworkManager als Standard-
Netzwerkbackend (statt dhcpcd/wpa_supplicant), `nmcli` ist dort bereits
vorinstalliert. Kein zusätzliches Python-Paket nötig.

Deckt sowohl WLAN (Scan/Verbinden/Vergessen) als auch die Anzeige des
aktuellen Verbindungsstatus ab - letzteres unabhängig davon, ob das Gerät
gerade per WLAN oder per LAN-Kabel verbunden ist.

Damit der Service-User (nicht root, keine aktive Login-Session) WLANs
wechseln darf, braucht es eine Polkit-Regel - siehe DEPLOY.md.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_NMCLI = "nmcli"
_CONNECT_WAIT_SECONDS = 20
_DEFAULT_TIMEOUT = 15
_NETPLAN_SUFFIXED_RE = re.compile(r"^netplan-[^-]+-(.+)$")
_NETPLAN_BARE_RE = re.compile(r"^netplan-([^-]+)$")


@dataclass
class WifiNetwork:
    ssid: str
    signal: int
    secured: bool


@dataclass
class SavedConnection:
    name: str
    display_name: str
    active: bool


@dataclass
class IPv4Details:
    method: str  # "auto" (DHCP), "manual" (feste IP) oder "" wenn unbekannt
    address: str | None
    prefix: int | None
    gateway: str | None
    dns: list[str]
    dhcp_server: str | None


@dataclass
class ActiveConnection:
    device: str
    type: str
    name: str
    ipv4: IPv4Details | None = None

    @property
    def label(self) -> str:
        kind = {"wifi": "WLAN", "ethernet": "LAN"}.get(self.type, self.type)
        return f"{kind}: {_strip_netplan_prefix(self.name)}"


@dataclass
class NetworkStatus:
    available: bool
    active_connections: list[ActiveConnection]
    ip_addresses: list[str]
    error: str | None = None


def _run(args: list[str], timeout: float = _DEFAULT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_NMCLI, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _strip_netplan_prefix(name: str) -> str:
    """Zeigt z.B. 'netplan-wlan0-NET62-IOT' als 'NET62-IOT' an, und
    'netplan-eth0' (kein Suffix, z.B. bei LAN-Profilen ohne eigenen Namen)
    als schlicht 'eth0'.

    Auf Raspberry Pi OS werden WLAN-/LAN-Profile teils via netplan angelegt,
    die daraus generierten NetworkManager-Verbindungsnamen tragen dann diesen
    technischen Prefix - mit oder ohne weiteren Namensteil. Der ursprüngliche
    `name` bleibt für nmcli-Aufrufe (z.B. `connection delete`) unverändert -
    nur die Anzeige wird bereinigt.
    """
    match = _NETPLAN_SUFFIXED_RE.match(name)
    if match:
        return match.group(1)
    match = _NETPLAN_BARE_RE.match(name)
    if match:
        return match.group(1)
    return name


def _split_terse_line(line: str, expected_fields: int) -> list[str] | None:
    """Splittet eine `nmcli -t`-Zeile an unescaped ':'.

    nmcli escaped Doppelpunkte innerhalb von Feldwerten (z.B. in SSIDs) als
    '\\:' - ein simpler str.split(':') würde solche Felder zerreissen.
    """
    fields: list[str] = []
    current: list[str] = []
    escape = False
    for ch in line:
        if escape:
            current.append(ch)
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(ch)
    fields.append("".join(current))
    return fields if len(fields) == expected_fields else None


def is_available() -> bool:
    try:
        result = _run(["--version"], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def scan_networks(rescan: bool = True) -> list[WifiNetwork]:
    """Sucht WLANs in Reichweite. Bei mehreren Access-Points mit gleicher SSID
    (typisch bei Mesh/Repeatern) wird nur der mit dem stärksten Signal
    zurückgegeben."""
    args = ["-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"]
    if rescan:
        args += ["--rescan", "yes"]
    try:
        result = _run(args, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    best: dict[str, WifiNetwork] = {}
    for line in result.stdout.splitlines():
        fields = _split_terse_line(line, 3)
        if not fields:
            continue
        ssid, signal_raw, security = fields
        ssid = ssid.strip()
        if not ssid:
            continue
        security = security.strip()
        if "802.1x" in security.lower():
            # Enterprise-WLANs (WPA2/3-Enterprise) brauchen Benutzername +
            # meist eine Zertifikatsentscheidung (CA vertrauen/ablehnen) -
            # das ist hier bewusst nicht unterstützt, daher ausgeblendet
            # statt mit einem kaputten "nur Passwort"-Dialog anzuzeigen.
            continue
        try:
            signal = int(signal_raw)
        except ValueError:
            signal = 0
        secured = bool(security) and security != "--"

        existing = best.get(ssid)
        if existing is None or signal > existing.signal:
            best[ssid] = WifiNetwork(ssid=ssid, signal=signal, secured=secured)

    return sorted(best.values(), key=lambda n: n.signal, reverse=True)


def list_saved_connections() -> list[SavedConnection]:
    try:
        result = _run(["-t", "-f", "NAME,TYPE,ACTIVE", "connection", "show"], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []

    connections = []
    for line in result.stdout.splitlines():
        fields = _split_terse_line(line, 3)
        if not fields:
            continue
        name, conn_type, active = fields
        if conn_type not in ("wifi", "802-11-wireless"):
            continue
        connections.append(
            SavedConnection(
                name=name,
                display_name=_strip_netplan_prefix(name),
                active=active.strip() == "yes",
            )
        )
    return connections


def get_ipv4_details(device: str, connection_name: str | None = None) -> IPv4Details:
    """Liest IPv4-Konfiguration (Adresse/Gateway/DNS/DHCP-Server) eines Geräts
    sowie (falls ein Verbindungsname übergeben wird) ob das Profil auf DHCP
    ("auto") oder feste IP ("manual") steht."""
    method = ""
    if connection_name:
        try:
            result = _run(["-t", "-f", "ipv4.method", "connection", "show", connection_name], timeout=10)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if ":" in line:
                        method = line.split(":", 1)[1].strip()
        except (OSError, subprocess.TimeoutExpired):
            pass

    address: str | None = None
    prefix: int | None = None
    gateway: str | None = None
    dns: list[str] = []
    dhcp_server: str | None = None
    try:
        result = _run(
            ["-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,DHCP4.OPTION", "device", "show", device],
            timeout=10,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                value = value.strip()
                if key.startswith("IP4.ADDRESS"):
                    if "/" in value:
                        address, prefix_str = value.split("/", 1)
                        try:
                            prefix = int(prefix_str)
                        except ValueError:
                            prefix = None
                    elif value:
                        address = value
                elif key.startswith("IP4.GATEWAY"):
                    gateway = value or None
                elif key.startswith("IP4.DNS"):
                    if value:
                        dns.append(value)
                elif key.startswith("DHCP4.OPTION") and "dhcp_server_identifier" in value:
                    parts = value.split("=", 1)
                    if len(parts) == 2:
                        dhcp_server = parts[1].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass

    return IPv4Details(
        method=method, address=address, prefix=prefix, gateway=gateway, dns=dns, dhcp_server=dhcp_server
    )


def get_status() -> NetworkStatus:
    """Aktueller Verbindungsstatus - unabhängig davon, ob per WLAN oder per
    LAN-Kabel verbunden wird (zeigt beides, falls beides aktiv ist)."""
    if not is_available():
        return NetworkStatus(
            available=False,
            active_connections=[],
            ip_addresses=[],
            error="NetworkManager (nmcli) wurde auf diesem Gerät nicht gefunden.",
        )

    active_connections: list[ActiveConnection] = []
    try:
        result = _run(["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status"], timeout=10)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                fields = _split_terse_line(line, 4)
                if not fields:
                    continue
                device, conn_type, state, connection = fields
                if conn_type == "loopback":
                    continue
                if not state.startswith("connected"):
                    continue
                if not connection or connection == "--":
                    continue
                active_connections.append(
                    ActiveConnection(
                        device=device,
                        type=conn_type,
                        name=connection,
                        ipv4=get_ipv4_details(device, connection),
                    )
                )
    except (OSError, subprocess.TimeoutExpired):
        pass

    ip_addresses: list[str] = []
    try:
        ip_result = subprocess.run(
            ["hostname", "-I"], capture_output=True, text=True, timeout=5, check=False
        )
        if ip_result.returncode == 0:
            ip_addresses = ip_result.stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        pass

    return NetworkStatus(available=True, active_connections=active_connections, ip_addresses=ip_addresses)


_KNOWN_ERROR_HINTS = (
    ("secrets were required", "Passwort falsch oder nicht angegeben."),
    ("no network with ssid", "Netzwerk nicht gefunden - ausser Reichweite?"),
    ("no network with", "Netzwerk nicht gefunden - ausser Reichweite?"),
    ("timeout", "Zeitüberschreitung - Signal zu schwach oder Passwort falsch."),
)


def _friendly_error(raw: str) -> str:
    lowered = raw.lower()
    for needle, message in _KNOWN_ERROR_HINTS:
        if needle in lowered:
            return message
    return raw.strip() or "Verbindung fehlgeschlagen."


def connect(ssid: str, password: str = "") -> tuple[bool, str | None]:
    """Verbindet mit einem WLAN (legt bei Bedarf ein neues nmcli-Profil an).

    Gibt (erfolgreich, Fehlermeldung-oder-None) zurück statt zu werfen -
    Verbindungsfehler (falsches Passwort, ausser Reichweite) sind hier ein
    normaler, erwartbarer Fall und keine Ausnahme.
    """
    if not ssid.strip():
        return False, "SSID darf nicht leer sein."

    args = ["-w", str(_CONNECT_WAIT_SECONDS), "device", "wifi", "connect", ssid]
    if password:
        args += ["password", password]

    try:
        result = _run(args, timeout=_CONNECT_WAIT_SECONDS + 10)
    except (OSError, subprocess.TimeoutExpired):
        return False, "nmcli konnte nicht ausgeführt werden oder hat nicht rechtzeitig geantwortet."

    if result.returncode == 0:
        logger.info("WLAN verbunden: %s", ssid)
        return True, None
    error = _friendly_error(result.stderr or result.stdout)
    logger.warning("WLAN-Verbindung fehlgeschlagen (%s): %s", ssid, error)
    return False, error


_IPV4_RE = re.compile(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$")


def _valid_ipv4(value: str) -> bool:
    match = _IPV4_RE.match(value.strip())
    if not match:
        return False
    return all(0 <= int(part) <= 255 for part in match.groups())


def set_ipv4_dhcp(connection_name: str) -> tuple[bool, str | None]:
    """Stellt eine Verbindung auf DHCP (automatisch) um und aktiviert sie neu."""
    if not connection_name.strip():
        return False, "Kein Verbindungsprofil bekannt."
    try:
        result = _run(["connection", "modify", connection_name, "ipv4.method", "auto"], timeout=10)
        if result.returncode != 0:
            return False, _friendly_error(result.stderr or result.stdout)
        result = _run(["connection", "up", connection_name], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False, "nmcli konnte nicht ausgeführt werden oder hat nicht rechtzeitig geantwortet."
    if result.returncode != 0:
        return False, _friendly_error(result.stderr or result.stdout)
    logger.info("Netzwerk auf DHCP umgestellt: %s", connection_name)
    return True, None


def set_ipv4_static(
    connection_name: str, address: str, prefix: int, gateway: str, dns: list[str]
) -> tuple[bool, str | None]:
    """Stellt eine Verbindung auf eine feste IP um und aktiviert sie neu.

    Validiert Adresse/Gateway/DNS als Zahlenformat, bevor überhaupt etwas an
    nmcli geschickt wird - ein Tippfehler hier kann das Gerät sonst vom
    Netzwerk trennen und nur noch per Tastatur/Monitor am Gerät selbst
    behebbar machen.
    """
    if not connection_name.strip():
        return False, "Kein Verbindungsprofil bekannt."
    if not _valid_ipv4(address):
        return False, "Ungültige IP-Adresse."
    if not (0 <= prefix <= 32):
        return False, "Ungültiges Prefix (0-32)."
    if not _valid_ipv4(gateway):
        return False, "Ungültiges Gateway."
    dns_values = [d.strip() for d in dns if d.strip()]
    for entry in dns_values:
        if not _valid_ipv4(entry):
            return False, f"Ungültiger DNS-Server: {entry}"

    args = [
        "connection", "modify", connection_name,
        "ipv4.method", "manual",
        "ipv4.addresses", f"{address}/{prefix}",
        "ipv4.gateway", gateway,
        "ipv4.dns", ",".join(dns_values),
    ]
    try:
        result = _run(args, timeout=10)
        if result.returncode != 0:
            return False, _friendly_error(result.stderr or result.stdout)
        result = _run(["connection", "up", connection_name], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False, "nmcli konnte nicht ausgeführt werden oder hat nicht rechtzeitig geantwortet."
    if result.returncode != 0:
        return False, _friendly_error(result.stderr or result.stdout)
    logger.info("Netzwerk auf feste IP umgestellt: %s -> %s/%s", connection_name, address, prefix)
    return True, None


def forget(name: str) -> tuple[bool, str | None]:
    if not name.strip():
        return False, "Name darf nicht leer sein."
    try:
        result = _run(["connection", "delete", name], timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False, "nmcli konnte nicht ausgeführt werden oder hat nicht rechtzeitig geantwortet."
    if result.returncode == 0:
        logger.info("WLAN-Profil entfernt: %s", name)
        return True, None
    return False, _friendly_error(result.stderr or result.stdout)
