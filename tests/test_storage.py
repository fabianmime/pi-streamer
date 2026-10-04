import json

from app.storage import UsbDrive, format_drive, list_usb_drives, mount_drive, parse_lsblk_output, unmount_drive

SAMPLE_LSBLK_OUTPUT = json.dumps(
    {
        "blockdevices": [
            {
                "name": "mmcblk0",
                "label": None,
                "size": "31914983424",
                "mountpoint": None,
                "rm": False,
                "tran": None,
                "fstype": None,
                "type": "disk",
                "children": [
                    {
                        "name": "mmcblk0p1",
                        "label": "bootfs",
                        "size": "545259520",
                        "mountpoint": "/boot/firmware",
                        "rm": False,
                        "tran": None,
                        "fstype": "vfat",
                        "type": "part",
                    }
                ],
            },
            {
                "name": "sda",
                "label": None,
                "size": "32017047552",
                "mountpoint": None,
                "rm": True,
                "tran": "usb",
                "fstype": None,
                "type": "disk",
                "children": [
                    {
                        "name": "sda1",
                        "label": "REC-STICK",
                        "size": "32016994304",
                        "mountpoint": "/media/livevoice/REC-STICK",
                        "rm": True,
                        "tran": None,
                        "fstype": "exfat",
                        "type": "part",
                    }
                ],
            },
        ]
    }
)


def test_parse_lsblk_output_extracts_only_usb_partitions_with_filesystem():
    drives = parse_lsblk_output(SAMPLE_LSBLK_OUTPUT)

    assert drives == [
        UsbDrive(
            device="/dev/sda1",
            label="REC-STICK",
            size_bytes=32016994304,
            mountpoint="/media/livevoice/REC-STICK",
        )
    ]


def test_parse_lsblk_output_invalid_json_returns_empty():
    assert parse_lsblk_output("not json") == []


def test_parse_lsblk_output_empty_devices():
    assert parse_lsblk_output(json.dumps({"blockdevices": []})) == []


def test_usb_drive_display_label_falls_back_to_device():
    drive = UsbDrive(device="/dev/sda1", label="", size_bytes=None, mountpoint=None)
    assert drive.display_label == "/dev/sda1"
    assert drive.mounted is False


def test_usb_drive_mounted_true_with_mountpoint():
    drive = UsbDrive(device="/dev/sda1", label="STICK", size_bytes=1000, mountpoint="/media/x")
    assert drive.mounted is True


def test_list_usb_drives_returns_empty_when_lsblk_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("lsblk not found")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert list_usb_drives() == []


def test_mount_drive_parses_mountpoint_from_output(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "Mounted /dev/sda1 at /media/livevoice/REC-STICK.\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())

    mountpoint, error = mount_drive("/dev/sda1")

    assert mountpoint == "/media/livevoice/REC-STICK"
    assert error is None


def test_mount_drive_reports_error_on_failure(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "Error mounting /dev/sda1: not authorized"

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())

    mountpoint, error = mount_drive("/dev/sda1")

    assert mountpoint is None
    assert "not authorized" in error


def test_unmount_drive_success(monkeypatch):
    class FakeResult:
        returncode = 0
        stdout = "Unmounted /dev/sda1.\n"
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())

    ok, error = unmount_drive("/dev/sda1")

    assert ok is True
    assert error is None


def test_format_drive_unmounts_first_then_formats(monkeypatch):
    # Block.Format haengt ein gemountetes Ziel NICHT von sich aus aus (live
    # auf dem Pi widerlegt: "Device or resource busy" ohne expliziten
    # Unmount vorher) - format_drive muss darum selbst zuerst unmounten.
    class FakeResult:
        returncode = 0
        stdout = ""
        stderr = ""

    calls = []
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: calls.append(a[0]) or FakeResult())

    ok, error = format_drive("/dev/sda1")

    assert ok is True
    assert error is None
    assert len(calls) == 2
    assert calls[0][:3] == ["udisksctl", "unmount", "-b"]
    assert calls[1][0] == "busctl"
    assert "/org/freedesktop/UDisks2/block_devices/sda1" in calls[1]
    assert "exfat" in calls[1]


def test_format_drive_proceeds_even_if_unmount_fails(monkeypatch):
    # Haeufigster Fall: der Stick ist schon ausgehaengt, "unmount" schlaegt
    # dann erwartungsgemaess fehl - das darf format_drive nicht blockieren.
    class FakeUnmountResult:
        returncode = 1
        stdout = ""
        stderr = "Error unmounting /dev/sda1: not mounted"

    class FakeFormatResult:
        returncode = 0
        stdout = ""
        stderr = ""

    results = iter([FakeUnmountResult(), FakeFormatResult()])
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: next(results))

    ok, error = format_drive("/dev/sda1")

    assert ok is True
    assert error is None


def test_format_drive_reports_error_on_failure(monkeypatch):
    class FakeResult:
        returncode = 1
        stdout = ""
        stderr = "GDBus.Error:org.freedesktop.PolicyKit1.Error.NotAuthorized: Not authorized"

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: FakeResult())

    ok, error = format_drive("/dev/sda1")

    assert ok is False
    assert "NotAuthorized" in error


def test_format_drive_handles_missing_busctl(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("busctl not found")

    monkeypatch.setattr("subprocess.run", fake_run)

    ok, error = format_drive("/dev/sda1")

    assert ok is False
    assert error is not None
