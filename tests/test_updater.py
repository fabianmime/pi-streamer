"""Updater gegen echte, lokale Git-Repositories in tmp_path - "GitHub" ist
hier ein Bare-Repo, das Gerät ein Arbeitsverzeichnis daneben."""

import shutil
import subprocess
from pathlib import Path

import pytest

from app.updater import Updater, UpdateError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git nicht installiert")


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def remote(tmp_path):
    """Bare-Repo als Update-Quelle + Arbeitskopie zum "Pushen"."""
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(bare))
    work = tmp_path / "dev"
    work.mkdir()
    _git(work, "init", "--quiet", "--initial-branch=main")
    (work / "app.txt").write_text("v1\n", encoding="utf-8")
    (work / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (work / ".gitignore").write_text("instance/\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", "Version 1")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "--quiet", "origin", "main")
    return bare, work


def _push(work: Path, filename: str, content: str, message: str) -> None:
    (work / filename).parent.mkdir(parents=True, exist_ok=True)
    (work / filename).write_text(content, encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "--quiet", "-m", message)
    _git(work, "push", "--quiet", "origin", "main")


def _device_dir(tmp_path: Path) -> Path:
    """Gerät, wie es heute per rsync installiert ist: Dateien, aber kein .git,
    plus Konfiguration in instance/ (darf nie angefasst werden)."""
    device = tmp_path / "device"
    device.mkdir()
    (device / "app.txt").write_text("alt, per rsync\n", encoding="utf-8")
    (device / "instance").mkdir()
    (device / "instance" / "config.json").write_text('{"srt_url": "srt://behalten"}', encoding="utf-8")
    return device


class _Recorder:
    """Lässt git echt laufen, ersetzt nur pip (kein Netz im Test)."""

    def __init__(self, pip_ok: bool = True):
        self.pip_calls = []
        self.pip_ok = pip_ok

    def __call__(self, cmd, timeout, cwd):
        if "pip" in cmd:
            self.pip_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0 if self.pip_ok else 1, "", "" if self.pip_ok else "kaputt")
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def test_first_install_turns_rsync_deploy_into_git_checkout(tmp_path, remote):
    bare, _work = remote
    device = _device_dir(tmp_path)
    restarts = []
    updater = Updater(app_dir=device, runner=_Recorder(), restart=lambda: restarts.append(True))

    status = updater.check(str(bare), "main")
    assert status["update_available"] is True
    assert status["current_commit"] is None

    status = updater.install(str(bare), "main", is_busy=lambda: False)

    assert status["state"] == "restarting"
    assert (device / "app.txt").read_text(encoding="utf-8") == "v1\n"
    assert (device / "instance" / "config.json").read_text(encoding="utf-8") == '{"srt_url": "srt://behalten"}'
    assert status["current_commit"] == _git(bare, "rev-parse", "main")
    assert restarts == [True]


def test_check_lists_pending_commits_and_install_applies_them(tmp_path, remote):
    bare, work = remote
    device = _device_dir(tmp_path)
    runner = _Recorder()
    updater = Updater(app_dir=device, runner=runner, restart=lambda: None)
    updater.install(str(bare), "main", is_busy=lambda: False)
    runner.pip_calls.clear()

    _push(work, "app.txt", "v2\n", "Auto-Reconnect")
    status = updater.check(str(bare), "main")

    assert status["update_available"] is True
    assert len(status["pending_commits"]) == 1
    assert status["pending_commits"][0].endswith("Auto-Reconnect")

    status = updater.install(str(bare), "main", is_busy=lambda: False)

    assert (device / "app.txt").read_text(encoding="utf-8") == "v2\n"
    assert runner.pip_calls == []  # requirements.txt unverändert -> kein pip
    assert status["systemd_files_changed"] is False


def test_up_to_date_after_install(tmp_path, remote):
    bare, _work = remote
    device = _device_dir(tmp_path)
    updater = Updater(app_dir=device, runner=_Recorder(), restart=lambda: None)
    updater.install(str(bare), "main", is_busy=lambda: False)

    status = updater.check(str(bare), "main")

    assert status["update_available"] is False
    assert status["state"] == "idle"


def test_changed_requirements_trigger_pip(tmp_path, remote):
    bare, work = remote
    device = _device_dir(tmp_path)
    runner = _Recorder()
    updater = Updater(app_dir=device, runner=runner, restart=lambda: None)
    updater.install(str(bare), "main", is_busy=lambda: False)
    runner.pip_calls.clear()

    _push(work, "requirements.txt", "flask\nwaitress\n", "Neue Abhängigkeit")
    updater.install(str(bare), "main", is_busy=lambda: False)

    assert len(runner.pip_calls) == 1


def test_failed_pip_rolls_back_to_previous_version(tmp_path, remote):
    bare, work = remote
    device = _device_dir(tmp_path)
    runner = _Recorder()
    updater = Updater(app_dir=device, runner=runner, restart=lambda: None)
    updater.install(str(bare), "main", is_busy=lambda: False)
    old_commit = updater.status()["current_commit"]

    runner.pip_ok = False
    _push(work, "requirements.txt", "gibtsnicht\n", "Kaputte Abhängigkeit")
    _push(work, "app.txt", "v3\n", "v3")

    with pytest.raises(UpdateError):
        updater.install(str(bare), "main", is_busy=lambda: False)

    assert updater.status()["state"] == "error"
    assert "alter Stand wiederhergestellt" in updater.status()["message"]
    assert updater.status()["current_commit"] == old_commit
    assert (device / "app.txt").read_text(encoding="utf-8") == "v1\n"


def test_systemd_changes_are_flagged(tmp_path, remote):
    bare, work = remote
    device = _device_dir(tmp_path)
    updater = Updater(app_dir=device, runner=_Recorder(), restart=lambda: None)
    updater.install(str(bare), "main", is_busy=lambda: False)

    _push(work, "systemd/neu.rules", "x\n", "Neue Polkit-Regel")
    status = updater.install(str(bare), "main", is_busy=lambda: False)

    assert status["systemd_files_changed"] is True
    assert "DEPLOY.md" in status["message"]


def test_install_refused_while_busy(tmp_path, remote):
    bare, _work = remote
    device = _device_dir(tmp_path)
    updater = Updater(app_dir=device, runner=_Recorder(), restart=lambda: None)

    with pytest.raises(UpdateError):
        updater.install(str(bare), "main", is_busy=lambda: True)

    assert (device / "app.txt").read_text(encoding="utf-8") == "alt, per rsync\n"


def test_unreachable_source_reports_error(tmp_path):
    device = _device_dir(tmp_path)
    updater = Updater(app_dir=device, runner=_Recorder(), restart=lambda: None)

    with pytest.raises(UpdateError):
        updater.check(str(tmp_path / "gibt-es-nicht.git"), "main")

    assert updater.status()["state"] == "error"


def test_missing_source_reports_error(tmp_path):
    updater = Updater(app_dir=_device_dir(tmp_path), runner=_Recorder(), restart=lambda: None)
    with pytest.raises(UpdateError):
        updater.check("", "main")
