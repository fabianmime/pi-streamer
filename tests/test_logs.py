import subprocess

from app import logs


class _FakeResult:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_get_recent_log_text_returns_stdout(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "journalctl"
        assert "-u" in cmd and logs.UNIT_NAME in cmd
        return _FakeResult(stdout="Zeile 1\nZeile 2\n", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert logs.get_recent_log_text() == "Zeile 1\nZeile 2\n"


def test_get_recent_log_text_handles_missing_journalctl(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = logs.get_recent_log_text()
    assert "konnte nicht gelesen werden" in result


def test_get_recent_log_text_handles_permission_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeResult(returncode=1, stderr="No permissions to access journal files")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = logs.get_recent_log_text()
    assert "systemd-journal" in result


def test_get_recent_log_text_empty_output_has_fallback_message(monkeypatch):
    def fake_run(cmd, **kwargs):
        return _FakeResult(stdout="", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert logs.get_recent_log_text() == "Keine Log-Einträge gefunden."
