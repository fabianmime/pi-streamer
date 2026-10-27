"""Selbst-Update des Geräts aus einem Git-Repository (z.B. GitHub).

Ablauf: neuen Code auf GitHub pushen -> in den Einstellungen unter "Update"
"Nach Updates suchen" -> "Jetzt installieren" (oder automatisch, siehe
`Updater.auto_loop`). Technisch:

1. `git fetch <Update-Quelle> <Branch>` in eine eigene Remote-Ref - kein
   konfiguriertes `origin` nötig, die Quelle kommt aus den Einstellungen.
2. `git reset --hard` auf den geholten Stand. `instance/` (Konfiguration,
   Schlüssel), `venv/` und `.asoundrc` sind per .gitignore ausgenommen und
   bleiben unberührt.
3. Hat sich `requirements.txt` geändert, `pip install -r requirements.txt` im
   laufenden venv - schlägt das fehl, wird auf den alten Stand zurückgesetzt.
4. Dienst beendet sich selbst, systemd startet ihn mit dem neuen Code neu
   (Restart=always, wie beim Port-Wechsel).

Ist das Programmverzeichnis noch kein Git-Checkout (Erstinstallation per
rsync/scp, siehe DEPLOY.md), legt der erste Lauf das Repository an - die
vorhandenen Dateien werden dabei durch den Stand aus Git ersetzt.

Bewusst NICHT automatisch aktualisiert werden Dateien ausserhalb des
Programmverzeichnisses (systemd-Unit, Polkit-Regeln unter `systemd/`) -
die brauchen root. Ändert ein Update etwas unter `systemd/`, zeigt die
Update-Karte einen Hinweis auf DEPLOY.md.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
_UPDATE_REF = "refs/remotes/livevoice-update/head"
_MAX_PENDING_COMMITS = 20

AUTO_CHECK_INTERVAL_SECONDS = 6 * 3600
AUTO_FIRST_CHECK_DELAY_SECONDS = 10 * 60

# Kein Passwort-/Benutzername-Prompt (würde den Dienst blockieren) und
# GitHub-Hostkey beim ersten Kontakt übernehmen (privates Repo per
# Deploy-Key, siehe DEPLOY.md).
_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new",
}

def schedule_process_restart(delay: float = 3.0) -> None:
    """Beendet den Prozess verzögert - systemd startet ihn mit dem neuen
    Code neu (Restart=always). Die Verzögerung lässt dem WebUI Zeit, den
    Status "startet neu" noch abzuholen."""
    threading.Timer(delay, os._exit, args=(0,)).start()


def _run(cmd: list[str], timeout: float, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **_GIT_ENV},
    )


@dataclass
class UpdateStatus:
    # idle | checking | installing | restarting | error
    state: str = "idle"
    message: str | None = None
    current_commit: str | None = None
    current_summary: str | None = None
    remote_commit: str | None = None
    update_available: bool = False
    pending_commits: list[str] = field(default_factory=list)
    last_check: str | None = None
    systemd_files_changed: bool = False


class UpdateError(Exception):
    pass


class Updater:
    def __init__(
        self,
        app_dir: Path = APP_DIR,
        runner: Callable[[list[str], float, Path], subprocess.CompletedProcess] = _run,
        restart: Callable[[], None] | None = None,
    ) -> None:
        self._app_dir = app_dir
        self._runner = runner
        self._restart = restart
        self._busy = threading.Lock()
        self._status = UpdateStatus()
        self._refresh_current()

    # --- öffentliche API ----------------------------------------------------

    def status(self) -> dict:
        return asdict(self._status)

    def check(self, repo_url: str, branch: str) -> dict:
        """Holt den Stand der Update-Quelle und vergleicht ihn mit dem
        installierten. Blockiert (Aufruf aus einem Thread/der Auto-Schleife)."""
        if not self._busy.acquire(blocking=False):
            raise UpdateError("Es läuft bereits eine Update-Aktion.")
        try:
            self._status.state = "checking"
            self._status.message = None
            self._fetch(repo_url, branch)
            self._compare()
            self._status.state = "idle"
            self._status.last_check = datetime.now().strftime("%d.%m.%Y %H:%M")
            if self._status.update_available:
                self._status.message = f"Update verfügbar ({len(self._status.pending_commits)} Änderung(en))."
            else:
                self._status.message = "Gerät ist auf dem neusten Stand."
            return self.status()
        except UpdateError as exc:
            self._fail(str(exc))
            raise
        finally:
            self._busy.release()

    def install(self, repo_url: str, branch: str, is_busy: Callable[[], bool]) -> dict:
        """Holt den neusten Stand und installiert ihn. Verweigert, solange
        ein Stream/eine Aufnahme läuft (Dienst-Neustart würde sie abbrechen)."""
        if is_busy():
            raise UpdateError("Erst Stream und Aufnahme stoppen, dann aktualisieren.")
        if not self._busy.acquire(blocking=False):
            raise UpdateError("Es läuft bereits eine Update-Aktion.")
        try:
            self._status.state = "installing"
            self._status.message = "Lade Update..."
            self._fetch(repo_url, branch)
            self._compare()
            if not self._status.update_available:
                self._status.state = "idle"
                self._status.message = "Gerät ist auf dem neusten Stand."
                return self.status()
            if is_busy():  # könnte während des Fetch gestartet worden sein
                raise UpdateError("Erst Stream und Aufnahme stoppen, dann aktualisieren.")

            old_commit = self._status.current_commit
            target = self._status.remote_commit
            changed = self._changed_files(old_commit, target)
            self._git(["reset", "--quiet", "--hard", target], 120, "Update konnte nicht angewendet werden")
            logger.info("Update angewendet: %s -> %s", old_commit or "(kein Git-Stand)", target)

            if changed is None or "requirements.txt" in changed:
                self._status.message = "Installiere Python-Pakete..."
                self._pip_install(old_commit)

            self._status.systemd_files_changed = changed is None or any(p.startswith("systemd/") for p in changed)
            self._refresh_current()
            self._status.update_available = False
            self._status.pending_commits = []
            self._status.state = "restarting"
            self._status.message = "Update installiert - Dienst startet neu."
            if self._status.systemd_files_changed:
                self._status.message += (
                    " Hinweis: Dateien unter systemd/ wurden geändert - diese per SSH "
                    "gemäss DEPLOY.md nachziehen."
                )
            if self._restart is not None:
                self._restart()
            return self.status()
        except UpdateError as exc:
            self._fail(str(exc))
            raise
        finally:
            self._busy.release()

    def start_async(self, action: Callable[[], object]) -> None:
        """Startet check/install im Hintergrund - das WebUI pollt
        /api/update/status, statt eine HTTP-Anfrage minutenlang offen zu
        halten (pip kann auf dem Pi dauern)."""

        def run():
            try:
                action()
            except UpdateError:
                pass  # steht bereits im Status
            except Exception as exc:  # noqa: BLE001 - nie den Thread lautlos sterben lassen
                logger.exception("Update-Aktion fehlgeschlagen")
                self._fail(f"Unerwarteter Fehler: {exc}")

        threading.Thread(target=run, daemon=True).start()

    def auto_loop(self, get_config: Callable[[], object], is_busy: Callable[[], bool]) -> None:
        """Endlosschleife für den Hintergrund-Thread: prüft alle
        AUTO_CHECK_INTERVAL_SECONDS und installiert, wenn "Updates
        automatisch installieren" aktiv ist und gerade weder gestreamt noch
        aufgenommen wird. Sonst wird nur angezeigt, dass ein Update da ist."""
        time.sleep(AUTO_FIRST_CHECK_DELAY_SECONDS)
        while True:
            cfg = get_config()
            url = getattr(cfg, "update_repo_url", "")
            branch = getattr(cfg, "update_branch", "main")
            if url:
                try:
                    self.check(url, branch)
                    if getattr(cfg, "update_auto", False) and self._status.update_available and not is_busy():
                        logger.info("Automatisches Update wird installiert")
                        self.install(url, branch, is_busy)
                except UpdateError as exc:
                    logger.warning("Automatische Update-Prüfung fehlgeschlagen: %s", exc)
                except Exception:  # noqa: BLE001
                    logger.exception("Automatische Update-Prüfung fehlgeschlagen")
            time.sleep(AUTO_CHECK_INTERVAL_SECONDS)

    # --- intern ---------------------------------------------------------------

    def _fail(self, message: str) -> None:
        self._status.state = "error"
        self._status.message = message
        logger.warning("Update: %s", message)

    def _git(self, args: list[str], timeout: float, error_prefix: str) -> str:
        try:
            result = self._runner(["git", *args], timeout, self._app_dir)
        except FileNotFoundError as exc:
            raise UpdateError("git ist auf diesem Gerät nicht installiert (sudo apt install git).") from exc
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UpdateError(f"{error_prefix}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise UpdateError(f"{error_prefix}: {detail[-1] if detail else 'unbekannter Fehler'}")
        return result.stdout.strip()

    def _is_repo(self) -> bool:
        return (self._app_dir / ".git").exists()

    def _refresh_current(self) -> None:
        if not self._is_repo():
            self._status.current_commit = None
            self._status.current_summary = None
            return
        try:
            self._status.current_commit = self._git(["rev-parse", "HEAD"], 10, "Version nicht lesbar")
            self._status.current_summary = self._git(
                ["log", "-1", "--format=%h vom %cd: %s", "--date=format:%d.%m.%Y"], 10, "Version nicht lesbar"
            )
        except UpdateError:
            # Frisch angelegtes Repo ohne Commit oder git fehlt - kein Fehler
            # beim Start, nur "Version unbekannt" in der Anzeige.
            self._status.current_commit = None
            self._status.current_summary = None

    def _fetch(self, repo_url: str, branch: str) -> None:
        if not repo_url:
            raise UpdateError("Keine Update-Quelle eingetragen (Einstellungen -> Update).")
        if not self._is_repo():
            self._git(["init", "--quiet"], 30, "Git-Repository konnte nicht angelegt werden")
        self._git(
            # repo_url/branch sind in config.validated() auf https://... bzw.
            # git@host:... und einen harmlosen Branchnamen eingeschränkt -
            # kein führendes "-", das git als Option lesen könnte.
            ["fetch", "--quiet", "--no-tags", repo_url, f"+refs/heads/{branch}:{_UPDATE_REF}"],
            180,
            "Update-Quelle nicht erreichbar",
        )

    def _compare(self) -> None:
        self._refresh_current()
        remote = self._git(["rev-parse", _UPDATE_REF], 10, "Update-Stand nicht lesbar")
        self._status.remote_commit = remote
        current = self._status.current_commit
        if current == remote:
            self._status.update_available = False
            self._status.pending_commits = []
            return
        self._status.update_available = True
        range_spec = f"{current}..{remote}" if current else remote
        log = self._git(
            ["log", f"--max-count={_MAX_PENDING_COMMITS}", "--format=%h %s", range_spec],
            30,
            "Änderungsliste nicht lesbar",
        )
        self._status.pending_commits = [line for line in log.splitlines() if line.strip()]

    def _changed_files(self, old: str | None, new: str) -> set[str] | None:
        """None = unbekannt (Erstinstallation) -> alles als geändert behandeln."""
        if not old:
            return None
        out = self._git(["diff", "--name-only", old, new], 30, "Änderungen nicht lesbar")
        return {line.strip() for line in out.splitlines() if line.strip()}

    def _pip_install(self, rollback_to: str | None) -> None:
        cmd = [sys.executable, "-m", "pip", "install", "--quiet", "-r", "requirements.txt"]
        try:
            result = self._runner(cmd, 900, self._app_dir)
            ok = result.returncode == 0
            detail = (result.stderr or result.stdout or "").strip().splitlines()
        except (OSError, subprocess.TimeoutExpired) as exc:
            ok, detail = False, [str(exc)]
        if ok:
            return
        message = f"Python-Pakete konnten nicht installiert werden: {detail[-1] if detail else 'unbekannter Fehler'}"
        if rollback_to:
            try:
                self._git(["reset", "--quiet", "--hard", rollback_to], 120, "Zurücksetzen fehlgeschlagen")
                message += " - alter Stand wiederhergestellt."
            except UpdateError as exc:
                message += f" - Zurücksetzen fehlgeschlagen: {exc}"
        self._refresh_current()
        raise UpdateError(message)
