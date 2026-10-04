import subprocess

from app import wifi


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_split_terse_line_handles_escaped_colon():
    fields = wifi._split_terse_line(r"Cafe\:Free:60:WPA2", 3)
    assert fields == ["Cafe:Free", "60", "WPA2"]


def test_split_terse_line_rejects_wrong_field_count():
    assert wifi._split_terse_line("a:b", 3) is None


def test_strip_netplan_prefix():
    assert wifi._strip_netplan_prefix("netplan-wlan0-NET62-IOT") == "NET62-IOT"
    assert wifi._strip_netplan_prefix("MeinWLAN") == "MeinWLAN"


def test_strip_netplan_prefix_without_suffix():
    # LAN-Profile werden von netplan teils ohne weiteren Namensteil angelegt.
    assert wifi._strip_netplan_prefix("netplan-eth0") == "eth0"


def test_scan_networks_parses_and_dedupes_by_strongest_signal(monkeypatch):
    output = "\n".join(
        [
            "HomeWifi:45:WPA2",
            "HomeWifi:78:WPA2",
            "Guest:60:--",
            r"Cafe\:Free:30:WPA1 WPA2",
        ]
    )

    def fake_run(args, **kwargs):
        return _FakeResult(stdout=output, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    networks = wifi.scan_networks()

    assert networks[0] == wifi.WifiNetwork(ssid="HomeWifi", signal=78, secured=True)
    assert wifi.WifiNetwork(ssid="Guest", signal=60, secured=False) in networks
    assert wifi.WifiNetwork(ssid="Cafe:Free", signal=30, secured=True) in networks
    # stärkstes Signal zuerst
    assert networks == sorted(networks, key=lambda n: n.signal, reverse=True)


def test_scan_networks_hides_enterprise_networks(monkeypatch):
    output = "\n".join(
        [
            "HomeWifi:70:WPA2",
            "CorpWifi:80:WPA2 802.1X",
            "CorpWifi3:60:WPA3 802.1X",
        ]
    )

    def fake_run(args, **kwargs):
        return _FakeResult(stdout=output, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    networks = wifi.scan_networks()

    ssids = {n.ssid for n in networks}
    assert ssids == {"HomeWifi"}


def test_scan_networks_returns_empty_on_nmcli_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert wifi.scan_networks() == []


def test_list_saved_connections_filters_wifi_type_and_strips_netplan_prefix(monkeypatch):
    output = "\n".join(
        [
            "netplan-wlan0-HomeWifi:wifi:yes",
            "Ethernet eth0:ethernet:no",
            "OldProfile:802-11-wireless:no",
        ]
    )

    def fake_run(args, **kwargs):
        return _FakeResult(stdout=output, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    connections = wifi.list_saved_connections()

    home = next(c for c in connections if c.name == "netplan-wlan0-HomeWifi")
    assert home.display_name == "HomeWifi"
    assert home.active is True

    old = next(c for c in connections if c.name == "OldProfile")
    assert old.display_name == "OldProfile"
    assert old.active is False

    assert all(c.name != "Ethernet eth0" for c in connections)


def test_connect_rejects_empty_ssid():
    ok, error = wifi.connect("")
    assert ok is False
    assert error


def test_connect_success(monkeypatch):
    def fake_run(args, **kwargs):
        assert "device" in args and "wifi" in args and "connect" in args
        assert "mypassword" in args
        return _FakeResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, error = wifi.connect("HomeWifi", "mypassword")
    assert ok is True
    assert error is None


def test_connect_wrong_password_gives_friendly_error(monkeypatch):
    def fake_run(args, **kwargs):
        return _FakeResult(
            returncode=1,
            stderr="Error: Connection activation failed: (7) Secrets were required, but not provided.",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, error = wifi.connect("HomeWifi", "wrong")
    assert ok is False
    assert "Passwort" in error


def test_forget_rejects_empty_name():
    ok, error = wifi.forget("")
    assert ok is False
    assert error


def test_forget_success(monkeypatch):
    def fake_run(args, **kwargs):
        assert "connection" in args and "delete" in args and "HomeWifi" in args
        return _FakeResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    ok, error = wifi.forget("HomeWifi")
    assert ok is True
    assert error is None


def test_get_status_when_nmcli_unavailable(monkeypatch):
    def fake_run(args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    status = wifi.get_status()
    assert status.available is False
    assert status.error


def test_get_status_reports_ethernet_and_wifi_active_connections(monkeypatch):
    device_status_output = "\n".join(
        [
            "eth0:ethernet:connected:Wired connection 1",
            "wlan0:wifi:connected:netplan-wlan0-NET62-IOT",
            "lo:loopback:connected (externally):lo",
        ]
    )

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "nmcli"
        if cmd[1:2] == ["--version"]:
            return _FakeResult(returncode=0)
        if "status" in cmd:
            return _FakeResult(stdout=device_status_output, returncode=0)
        if "connection" in cmd and "show" in cmd:
            return _FakeResult(stdout="ipv4.method:auto\n", returncode=0)
        if "device" in cmd and "show" in cmd:
            return _FakeResult(stdout="", returncode=0)
        raise AssertionError(f"unerwarteter nmcli-Aufruf: {cmd}")

    def fake_hostname(cmd, **kwargs):
        return _FakeResult(stdout="10.11.4.170 \n", returncode=0)

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kwargs: (
        fake_hostname(cmd, **kwargs) if cmd[0] == "hostname" else fake_run(cmd, **kwargs)
    ))

    status = wifi.get_status()

    assert status.available is True
    labels = {c.label for c in status.active_connections}
    assert "LAN: Wired connection 1" in labels
    assert "WLAN: NET62-IOT" in labels
    assert status.ip_addresses == ["10.11.4.170"]


def test_valid_ipv4():
    assert wifi._valid_ipv4("192.168.1.1") is True
    assert wifi._valid_ipv4("255.255.255.255") is True
    assert wifi._valid_ipv4("256.1.1.1") is False
    assert wifi._valid_ipv4("192.168.1") is False
    assert wifi._valid_ipv4("not-an-ip") is False


def test_get_ipv4_details_parses_dhcp_connection(monkeypatch):
    def fake_run(cmd, **kwargs):
        if "ipv4.method" in cmd:
            return _FakeResult(stdout="ipv4.method:auto\n", returncode=0)
        if "device" in cmd and "show" in cmd:
            output = "\n".join(
                [
                    "IP4.ADDRESS[1]:192.168.1.50/24",
                    "IP4.GATEWAY:192.168.1.1",
                    "IP4.DNS[1]:192.168.1.1",
                    "IP4.DNS[2]:8.8.8.8",
                    "DHCP4.OPTION[1]:requested_broadcast_address = 1",
                    "DHCP4.OPTION[5]:dhcp_server_identifier = 192.168.1.1",
                ]
            )
            return _FakeResult(stdout=output, returncode=0)
        raise AssertionError(f"unerwarteter Aufruf: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    details = wifi.get_ipv4_details("eth0", "Wired connection 1")

    assert details.method == "auto"
    assert details.address == "192.168.1.50"
    assert details.prefix == 24
    assert details.gateway == "192.168.1.1"
    assert details.dns == ["192.168.1.1", "8.8.8.8"]
    assert details.dhcp_server == "192.168.1.1"


def test_set_ipv4_static_rejects_invalid_address():
    ok, error = wifi.set_ipv4_static("eth0", "999.999.999.999", 24, "192.168.1.1", [])
    assert ok is False
    assert "IP-Adresse" in error


def test_set_ipv4_static_rejects_invalid_prefix():
    ok, error = wifi.set_ipv4_static("eth0", "192.168.1.50", 99, "192.168.1.1", [])
    assert ok is False
    assert "Prefix" in error


def test_set_ipv4_static_rejects_invalid_gateway():
    ok, error = wifi.set_ipv4_static("eth0", "192.168.1.50", 24, "not-an-ip", [])
    assert ok is False
    assert "Gateway" in error


def test_set_ipv4_static_rejects_invalid_dns():
    ok, error = wifi.set_ipv4_static("eth0", "192.168.1.50", 24, "192.168.1.1", ["not-an-ip"])
    assert ok is False
    assert "DNS" in error


def test_set_ipv4_static_success(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, error = wifi.set_ipv4_static("eth0", "192.168.1.50", 24, "192.168.1.1", ["1.1.1.1", "8.8.8.8"])

    assert ok is True
    assert error is None
    modify_call = calls[0]
    assert "192.168.1.50/24" in modify_call
    assert "1.1.1.1,8.8.8.8" in modify_call
    assert "connection" in calls[1] and "up" in calls[1]


def test_set_ipv4_dhcp_success(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeResult(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, error = wifi.set_ipv4_dhcp("eth0")

    assert ok is True
    assert error is None


def test_set_ipv4_dhcp_rejects_empty_connection():
    ok, error = wifi.set_ipv4_dhcp("")
    assert ok is False
    assert error
