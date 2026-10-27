(function () {
  "use strict";

  // Theme-Toggle (hell/dunkel) - Default folgt der Systemeinstellung
  // (prefers-color-scheme), manuelle Wahl wird in localStorage gemerkt und
  // von base.html schon vor dem CSS-Laden angewendet (kein Flackern).
  const themeToggleBtn = document.getElementById("theme-toggle");
  if (themeToggleBtn) {
    function currentTheme() {
      const attr = document.documentElement.getAttribute("data-theme");
      if (attr === "light" || attr === "dark") return attr;
      return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    themeToggleBtn.addEventListener("click", () => {
      const next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try {
        localStorage.setItem("theme", next);
      } catch (e) {
        /* localStorage evtl. nicht verfügbar - Wahl gilt dann nur für diese Seite */
      }
    });
  }

  // Passwort-Felder: eigener Anzeigen/Verbergen-Knopf statt der
  // uneinheitlichen nativen Browser-Reveal-Icons.
  document.querySelectorAll(".password-toggle").forEach((btn) => {
    const input = document.getElementById(btn.dataset.target);
    if (!input) return;
    btn.addEventListener("click", () => {
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      btn.textContent = showing ? "👁" : "🙈";
      btn.setAttribute("aria-label", showing ? "Passwort anzeigen" : "Passwort verbergen");
    });
  });

  // Wiederverwendete Formatierer für Speicherplatz/Restlaufzeit (Cockpit-
  // Storage-Anzeige UND Settings-Systemstatistik).
  function formatGB(bytes) {
    if (bytes === null || bytes === undefined) return "-";
    return (bytes / 1024 ** 3).toFixed(1) + " GB";
  }

  function formatDurationApprox(seconds) {
    if (seconds === null || seconds === undefined || seconds < 0) return "-";
    const totalMin = Math.floor(seconds / 60);
    if (totalMin < 1) return "< 1 Min.";
    const h = Math.floor(totalMin / 60);
    const m = totalMin % 60;
    return h > 0 ? `${h} Std. ${m} Min.` : `${m} Min.`;
  }

  const toggleBtn = document.getElementById("stream-toggle");
  const dot = document.getElementById("status-dot");
  const text = document.getElementById("status-text");
  const uptimeEl = document.getElementById("status-uptime");
  const bitrateEl = document.getElementById("status-bitrate");
  const samplerateEl = document.getElementById("status-samplerate");
  const deviceEl = document.getElementById("status-device");
  const errorEl = document.getElementById("status-error");
  const errorRowEl = document.getElementById("status-error-row");
  const errorDismissBtn = document.getElementById("status-error-dismiss");

  let busy = false;
  let lastKnownRunning = false;

  function formatUptime(seconds) {
    if (!seconds || seconds < 1) return "-";
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return [h, m, sec].map((v) => String(v).padStart(2, "0")).join(":");
  }

  const reconnectsEl = document.getElementById("status-reconnects");

  function render(status) {
    // Während des automatischen Neuverbindens gilt der Stream als "soll
    // laufen" - der Knopf stoppt dann das Neuverbinden.
    lastKnownRunning = status.running || status.reconnecting;
    dot.classList.remove("running", "stopped", "error", "reconnecting");

    if (status.running) {
      dot.classList.add("running");
      text.textContent = "Streaming läuft";
      toggleBtn.textContent = "Stream stoppen";
      toggleBtn.classList.remove("btn-start");
      toggleBtn.classList.add("btn-stop");
    } else if (status.reconnecting) {
      dot.classList.add("reconnecting");
      const secs = Math.ceil(status.reconnect_in_seconds || 0);
      text.textContent =
        `Verbindung unterbrochen - neuer Versuch (${status.reconnect_attempt}) ` +
        (secs > 0 ? `in ${secs} s` : "läuft...");
      toggleBtn.textContent = "Stream stoppen";
      toggleBtn.classList.remove("btn-start");
      toggleBtn.classList.add("btn-stop");
    } else if (status.last_error) {
      dot.classList.add("error");
      text.textContent = "Gestoppt (Fehler)";
      toggleBtn.textContent = "Stream starten";
      toggleBtn.classList.remove("btn-stop");
      toggleBtn.classList.add("btn-start");
    } else {
      dot.classList.add("stopped");
      text.textContent = "Gestoppt";
      toggleBtn.textContent = "Stream starten";
      toggleBtn.classList.remove("btn-stop");
      toggleBtn.classList.add("btn-start");
    }

    toggleBtn.disabled = busy || (!lastKnownRunning && !status.config_ready);
    if (!status.config_ready && !lastKnownRunning) {
      toggleBtn.textContent = "Erst Einstellungen ausfüllen";
    }

    uptimeEl.textContent = formatUptime(status.uptime_seconds);
    if (reconnectsEl) {
      reconnectsEl.textContent = status.reconnects_total ? `${status.reconnects_total}×` : "-";
    }

    if (status.last_error) {
      errorRowEl.hidden = false;
      errorEl.textContent = status.last_error;
    } else {
      errorRowEl.hidden = true;
    }
  }

  async function refreshStatus() {
    try {
      const res = await fetch("/api/status");
      if (!res.ok) return;
      const status = await res.json();
      render(status);
    } catch (e) {
      // Netzwerkfehler beim Polling ignorieren, nächster Tick versucht's erneut
    }
  }

  async function toggleStream() {
    if (busy) return;
    busy = true;
    toggleBtn.disabled = true;
    const endpoint = lastKnownRunning ? "/api/stream/stop" : "/api/stream/start";
    try {
      const res = await fetch(endpoint, { method: "POST" });
      const data = await res.json();
      if (!res.ok && data.error) {
        errorRowEl.hidden = false;
        errorEl.textContent = data.error;
      }
    } finally {
      busy = false;
      await refreshStatus();
    }
  }

  if (errorDismissBtn) {
    errorDismissBtn.addEventListener("click", async () => {
      errorRowEl.hidden = true;
      try {
        await fetch("/api/stream/dismiss-error", { method: "POST" });
      } catch (e) {
        // Anzeige ist schon ausgeblendet - beim naechsten Poll kommt der
        // Fehler zurueck, falls das Schliessen serverseitig nicht ankam.
      }
      refreshStatus();
    });
  }

  if (toggleBtn) {
    toggleBtn.addEventListener("click", toggleStream);

    // Config-Werte (Gerät/Bitrate/Sample-Rate) sind serverseitig fix pro Stream,
    // werden hier nur informativ aus dem initial gerenderten HTML übernommen.
    const cfgNode = document.getElementById("stream-config-data");
    if (cfgNode) {
      try {
        const cfg = JSON.parse(cfgNode.textContent);
        bitrateEl.textContent = cfg.bitrate_kbps ? cfg.bitrate_kbps + " kbps" : "-";
        samplerateEl.textContent = cfg.sample_rate ? cfg.sample_rate + " Hz" : "-";
        deviceEl.textContent = cfg.audio_device || "-";
      } catch (e) {
        /* ignore */
      }
    }

    refreshStatus();
    setInterval(refreshStatus, 2000);
  }

  // Aufnahme-Start/Stop: eigener Button/Status auf derselben Seite wie der
  // Stream, aber unabhaengig davon steuerbar (siehe app/recorder.py).
  const recToggleBtn = document.getElementById("recording-toggle");
  const recDot = document.getElementById("recording-status-dot");
  const recText = document.getElementById("recording-status-text");
  const recUptimeEl = document.getElementById("recording-status-uptime");
  const recFileEl = document.getElementById("recording-status-file");
  const recErrorEl = document.getElementById("recording-status-error");
  const recErrorRowEl = document.getElementById("recording-status-error-row");
  const recErrorDismissBtn = document.getElementById("recording-status-error-dismiss");
  const storageMiniValue = document.getElementById("storage-mini-value");

  let recBusy = false;
  let recLastKnownRunning = false;

  function renderRecording(status) {
    recLastKnownRunning = status.running;
    recDot.classList.remove("running", "stopped", "error");

    if (status.running) {
      recDot.classList.add("running");
      recText.textContent = "Aufnahme läuft";
      recToggleBtn.textContent = "Aufnahme stoppen";
      recToggleBtn.classList.remove("btn-start");
      recToggleBtn.classList.add("btn-stop");
    } else if (status.last_error) {
      recDot.classList.add("error");
      recText.textContent = "Gestoppt (Fehler)";
      recToggleBtn.textContent = "Aufnahme starten";
      recToggleBtn.classList.remove("btn-stop");
      recToggleBtn.classList.add("btn-start");
    } else {
      recDot.classList.add("stopped");
      recText.textContent = "Gestoppt";
      recToggleBtn.textContent = "Aufnahme starten";
      recToggleBtn.classList.remove("btn-stop");
      recToggleBtn.classList.add("btn-start");
    }

    recToggleBtn.disabled = recBusy || (!status.running && !status.config_ready);
    if (!status.config_ready && !status.running) {
      recToggleBtn.textContent = "Erst Aufnahme-Ziel wählen";
    }

    recUptimeEl.textContent = formatUptime(status.uptime_seconds);
    if (recFileEl) recFileEl.textContent = status.output_path || "-";

    if (status.last_error) {
      recErrorRowEl.hidden = false;
      recErrorEl.textContent = status.last_error;
    } else {
      recErrorRowEl.hidden = true;
    }

    // USB-Speicher des Aufnahme-Ziels + geschätzte Restlaufzeit, oben auf dem
    // Cockpit in der Pegel-Karte (siehe README, "USB-Stick-Speicherplatz
    // prominent anzeigen"). Warnfarbe analog zur Pegel-Ampel bei knapper
    // werdender Restlaufzeit.
    if (storageMiniValue) {
      storageMiniValue.classList.remove("storage-warn-yellow", "storage-warn-red");
      if (status.target_free_bytes === null || status.target_free_bytes === undefined) {
        storageMiniValue.textContent = "-";
      } else {
        const eta = status.target_estimated_seconds;
        storageMiniValue.textContent =
          formatGB(status.target_free_bytes) + " frei · noch ca. " + formatDurationApprox(eta);
        if (eta !== null && eta !== undefined) {
          if (eta < 900) {
            storageMiniValue.classList.add("storage-warn-red");
          } else if (eta < 3600) {
            storageMiniValue.classList.add("storage-warn-yellow");
          }
        }
      }
    }
  }

  async function refreshRecordingStatus() {
    try {
      const res = await fetch("/api/recording/status");
      if (!res.ok) return;
      const status = await res.json();
      renderRecording(status);
    } catch (e) {
      // Netzwerkfehler beim Polling ignorieren, naechster Tick versucht's erneut
    }
  }

  async function toggleRecording() {
    if (recBusy) return;
    recBusy = true;
    recToggleBtn.disabled = true;
    const endpoint = recLastKnownRunning ? "/api/recording/stop" : "/api/recording/start";
    try {
      const res = await fetch(endpoint, { method: "POST" });
      const data = await res.json();
      if (!res.ok && data.error) {
        recErrorRowEl.hidden = false;
        recErrorEl.textContent = data.error;
      }
    } finally {
      recBusy = false;
      await refreshRecordingStatus();
    }
  }

  if (recErrorDismissBtn) {
    recErrorDismissBtn.addEventListener("click", async () => {
      recErrorRowEl.hidden = true;
      try {
        await fetch("/api/recording/dismiss-error", { method: "POST" });
      } catch (e) {
        // Anzeige ist schon ausgeblendet - beim naechsten Poll kommt der
        // Fehler zurueck, falls das Schliessen serverseitig nicht ankam.
      }
      refreshRecordingStatus();
    });
  }

  if (recToggleBtn) {
    recToggleBtn.addEventListener("click", toggleRecording);
    refreshRecordingStatus();
    setInterval(refreshRecordingStatus, 2000);
  }

  // Eingangspegel-Meter: laeuft immer, auch waehrend Stream/Aufnahme
  // (siehe app/levelmeter.py, geteiltes dsnoop-Geraet). Eigenes, schnelleres
  // Polling-Intervall als der Stream-Status, damit sich der Balken "live"
  // anfuehlt. Bei Stereo zwei Zeilen (L/R, siehe index.html), sonst eine -
  // `channel_peaks_db` liefert pro Kanal einen eigenen Wert.
  const levelRows = document.querySelectorAll(".level-meter-row");
  const levelHintEl = document.getElementById("level-meter-hint");
  if (levelRows.length) {
    function dbToPercent(db) {
      const clamped = Math.max(-60, Math.min(0, db));
      return ((clamped + 60) / 60) * 100;
    }
    function renderLevelRow(row, db) {
      const fill = row.querySelector(".level-meter-fill");
      const dbEl = row.querySelector(".level-meter-db");
      fill.classList.remove("level-yellow", "level-red");
      if (db === null || db === undefined) {
        fill.style.width = "0%";
        if (dbEl) dbEl.textContent = "-";
        return;
      }
      fill.style.width = dbToPercent(db) + "%";
      if (dbEl) dbEl.textContent = Math.round(db) + " dB";
      if (db > -3) {
        fill.classList.add("level-red");
      } else if (db > -12) {
        fill.classList.add("level-yellow");
      }
    }
    async function refreshLevel() {
      try {
        const res = await fetch("/api/audio-level");
        const data = await res.json();
        const peaks = data.channel_peaks_db || [];
        if (!data.available || peaks.length === 0) {
          levelRows.forEach((row) => renderLevelRow(row, null));
          if (levelHintEl) {
            levelHintEl.hidden = false;
            levelHintEl.textContent = "Kein Eingangssignal erkannt - Gerät angeschlossen?";
          }
          return;
        }
        if (levelHintEl) levelHintEl.hidden = true;
        levelRows.forEach((row, i) => renderLevelRow(row, peaks[i]));
      } catch (e) {
        // Netzwerkfehler beim Polling ignorieren, naechster Tick versucht's erneut
      }
    }
    refreshLevel();
    setInterval(refreshLevel, 200);
  }

  // Settings-Seite: USB-Audio-Geräte per AJAX neu einlesen, ohne die Seite
  // (und damit die restlichen Formularfelder) komplett neu zu laden.
  const refreshDevicesBtn = document.getElementById("refresh-devices");
  const deviceSelect = document.getElementById("audio_device");
  if (refreshDevicesBtn && deviceSelect) {
    const hint = document.getElementById("refresh-devices-hint");
    refreshDevicesBtn.addEventListener("click", async () => {
      refreshDevicesBtn.disabled = true;
      refreshDevicesBtn.textContent = "Suche...";
      try {
        const res = await fetch("/api/audio-devices");
        const devices = await res.json();
        const previouslySelected = deviceSelect.value;

        deviceSelect.innerHTML = "";
        if (devices.length === 0) {
          const opt = document.createElement("option");
          opt.value = previouslySelected;
          opt.textContent = previouslySelected || "Kein Gerät erkannt";
          opt.selected = true;
          deviceSelect.appendChild(opt);
          if (hint) hint.textContent = "Kein USB-Audio-Interface gefunden. Anschliessen und erneut suchen.";
        } else {
          devices.forEach((dev) => {
            const opt = document.createElement("option");
            opt.value = dev.hw_id;
            opt.textContent = dev.label;
            opt.selected = dev.hw_id === previouslySelected;
            deviceSelect.appendChild(opt);
          });
          if (hint) hint.textContent = devices.length + " Gerät(e) gefunden.";
        }
      } catch (e) {
        if (hint) hint.textContent = "Suche fehlgeschlagen. Bitte erneut versuchen.";
      } finally {
        refreshDevicesBtn.disabled = false;
        refreshDevicesBtn.textContent = "Neu suchen";
      }
    });
  }

  // Dateiname-Präfix (Teil der Settings-Seite): Vorschau im Hinweistext
  // soll sofort beim Tippen mitgehen, nicht erst nach dem Speichern den
  // zuletzt gespeicherten Wert zeigen.
  const filenamePrefixInput = document.getElementById("recording_filename_prefix");
  const filenamePreviewEl = document.getElementById("recording-filename-preview");
  if (filenamePrefixInput && filenamePreviewEl) {
    filenamePrefixInput.addEventListener("input", () => {
      filenamePreviewEl.textContent = filenamePrefixInput.value || "Aufnahme";
    });
  }
  const formatSelect = document.getElementById("recording_format");
  const extensionPreviewEl = document.getElementById("recording-extension-preview");
  if (formatSelect && extensionPreviewEl) {
    formatSelect.addEventListener("change", () => {
      const opt = formatSelect.options[formatSelect.selectedIndex];
      extensionPreviewEl.textContent = (opt && opt.dataset.extension) || "wav";
    });
  }
  const hostnameInput = document.getElementById("hostname");
  const hostnamePreviewEl = document.getElementById("hostname-preview");
  if (hostnameInput && hostnamePreviewEl) {
    hostnameInput.addEventListener("input", () => {
      hostnamePreviewEl.textContent = hostnameInput.value || "…";
    });
  }

  // Einstellungen -> Weitere Einstellungen -> Update: Prüfen/Installieren
  // laufen serverseitig im Hintergrund (app/updater.py), hier wird nur der
  // Status gepollt. Nach "Installieren" startet der Dienst neu - danach
  // wird die Seite automatisch neu geladen, sobald er wieder antwortet.
  const updateCheckBtn = document.getElementById("update-check");
  const updateInstallBtn = document.getElementById("update-install");
  if (updateCheckBtn && updateInstallBtn) {
    const updateMessageEl = document.getElementById("update-message");
    const updateCurrentEl = document.getElementById("update-current");
    const updateLastCheckEl = document.getElementById("update-last-check");
    const updateCommitsEl = document.getElementById("update-commits");
    let updatePollTimer = null;

    function renderUpdate(s) {
      const active = s.state === "checking" || s.state === "installing" || s.state === "restarting";
      if (updateMessageEl) {
        let msg = s.message || "";
        if (s.state === "checking") msg = "Suche nach Updates...";
        if (!s.configured) msg = "Keine Update-Quelle eingetragen.";
        updateMessageEl.textContent = msg;
        updateMessageEl.hidden = !msg;
        updateMessageEl.classList.toggle("error", s.state === "error");
      }
      if (updateCurrentEl && s.current_summary) updateCurrentEl.textContent = s.current_summary;
      if (updateLastCheckEl) updateLastCheckEl.textContent = s.last_check || "-";
      if (updateCommitsEl) {
        updateCommitsEl.innerHTML = "";
        (s.update_available ? s.pending_commits || [] : []).forEach((line) => {
          const li = document.createElement("li");
          li.textContent = line;
          updateCommitsEl.appendChild(li);
        });
      }
      updateCheckBtn.disabled = active || !s.configured;
      updateInstallBtn.hidden = !s.update_available;
      updateInstallBtn.disabled = active || s.busy;
      updateInstallBtn.title = s.busy ? "Erst Stream und Aufnahme stoppen" : "";
      return active;
    }

    async function pollUpdate() {
      try {
        const res = await fetch("/api/update/status");
        if (!res.ok) return;
        const s = await res.json();
        const active = renderUpdate(s);
        if (s.state === "restarting") {
          waitForRestart();
          return;
        }
        if (active) {
          updatePollTimer = setTimeout(pollUpdate, 1500);
        }
      } catch (e) {
        updatePollTimer = setTimeout(pollUpdate, 3000);
      }
    }

    function waitForRestart() {
      // Dienst ist kurz weg und kommt mit dem neuen Code zurück.
      setTimeout(async function retry() {
        try {
          const res = await fetch("/api/update/status", { cache: "no-store" });
          if (res.ok) {
            const s = await res.json();
            if (s.state !== "restarting") {
              window.location.reload();
              return;
            }
          }
        } catch (e) {
          /* noch nicht wieder da */
        }
        setTimeout(retry, 2000);
      }, 4000);
    }

    async function startUpdateAction(url) {
      updateCheckBtn.disabled = true;
      updateInstallBtn.disabled = true;
      try {
        const res = await fetch(url, { method: "POST" });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          if (updateMessageEl) {
            updateMessageEl.textContent = data.error || "Fehlgeschlagen.";
            updateMessageEl.hidden = false;
            updateMessageEl.classList.add("error");
          }
        }
      } finally {
        clearTimeout(updatePollTimer);
        setTimeout(pollUpdate, 500);
      }
    }

    updateCheckBtn.addEventListener("click", () => startUpdateAction("/api/update/check"));
    updateInstallBtn.addEventListener("click", () => {
      if (
        window.confirm(
          "Update jetzt installieren? Der Dienst startet dabei kurz neu (ca. 10-30 Sekunden), " +
            "Einstellungen bleiben erhalten."
        )
      ) {
        startUpdateAction("/api/update/install");
      }
    });
    pollUpdate();
  }

  // Aufnahme-Sektion (Teil der Settings-Seite): USB-Sticks suchen,
  // einhängen/auswerfen, ohne die Seite komplett neu zu laden.
  const refreshDrivesBtn = document.getElementById("refresh-drives");
  const driveSelect = document.getElementById("recording_target");
  if (refreshDrivesBtn && driveSelect) {
    const drivesHint = document.getElementById("refresh-drives-hint");
    const mountBtn = document.getElementById("mount-drive");
    const unmountBtn = document.getElementById("unmount-drive");
    const formatBtn = document.getElementById("format-drive");
    const driveStatusEl = document.getElementById("drive-action-status");

    function formatSize(bytes) {
      if (!bytes) return "";
      const gb = bytes / (1024 ** 3);
      return gb >= 1 ? ` (${gb.toFixed(1)} GB)` : ` (${Math.round(bytes / (1024 ** 2))} MB)`;
    }

    function renderDrives(drives, previousMountpoint) {
      driveSelect.innerHTML = "";
      if (drives.length === 0) {
        const opt = document.createElement("option");
        opt.value = previousMountpoint || "";
        opt.textContent = previousMountpoint || "Kein Stick erkannt";
        opt.selected = true;
        driveSelect.appendChild(opt);
        if (drivesHint) drivesHint.textContent = "Kein USB-Stick gefunden. Anschliessen und erneut suchen.";
        return;
      }
      drives.forEach((drive) => {
        const opt = document.createElement("option");
        opt.value = drive.mountpoint || "";
        opt.dataset.device = drive.device;
        opt.textContent = drive.label + formatSize(drive.free_bytes ?? drive.size_bytes)
          + (drive.mounted ? "" : " (nicht eingehängt)");
        opt.selected = drive.mounted && drive.mountpoint === previousMountpoint;
        driveSelect.appendChild(opt);
      });
      if (drivesHint) drivesHint.textContent = drives.length + " Stick(s) gefunden.";
    }

    async function fetchDrives() {
      const res = await fetch("/api/usb-drives");
      return res.json();
    }

    refreshDrivesBtn.addEventListener("click", async () => {
      refreshDrivesBtn.disabled = true;
      refreshDrivesBtn.textContent = "Suche...";
      try {
        const previouslySelected = driveSelect.value;
        const drives = await fetchDrives();
        renderDrives(drives, previouslySelected);
      } catch (e) {
        if (drivesHint) drivesHint.textContent = "Suche fehlgeschlagen. Bitte erneut versuchen.";
      } finally {
        refreshDrivesBtn.disabled = false;
        refreshDrivesBtn.textContent = "Neu suchen";
      }
    });

    if (mountBtn) {
      mountBtn.addEventListener("click", async () => {
        const opt = driveSelect.selectedOptions[0];
        const device = opt && opt.dataset.device;
        if (!device) {
          if (driveStatusEl) { driveStatusEl.hidden = false; driveStatusEl.textContent = "Kein Stick ausgewählt."; }
          return;
        }
        mountBtn.disabled = true;
        if (driveStatusEl) { driveStatusEl.hidden = false; driveStatusEl.textContent = "Hänge ein..."; }
        try {
          const res = await fetch("/api/usb-drives/mount", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device }),
          });
          const data = await res.json();
          if (res.ok) {
            const drives = await fetchDrives();
            renderDrives(drives, data.mountpoint);
            if (driveStatusEl) driveStatusEl.textContent = "Eingehängt unter " + data.mountpoint + ".";
          } else if (driveStatusEl) {
            driveStatusEl.textContent = data.error || "Einhängen fehlgeschlagen.";
          }
        } catch (e) {
          if (driveStatusEl) driveStatusEl.textContent = "Einhängen fehlgeschlagen.";
        } finally {
          mountBtn.disabled = false;
        }
      });
    }

    if (unmountBtn) {
      unmountBtn.addEventListener("click", async () => {
        const opt = driveSelect.selectedOptions[0];
        const device = opt && opt.dataset.device;
        if (!device) {
          if (driveStatusEl) { driveStatusEl.hidden = false; driveStatusEl.textContent = "Kein Stick ausgewählt."; }
          return;
        }
        unmountBtn.disabled = true;
        if (driveStatusEl) { driveStatusEl.hidden = false; driveStatusEl.textContent = "Werfe aus..."; }
        try {
          const res = await fetch("/api/usb-drives/unmount", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device }),
          });
          const data = await res.json();
          if (res.ok) {
            const drives = await fetchDrives();
            renderDrives(drives, null);
            if (driveStatusEl) driveStatusEl.textContent = "Sicher ausgeworfen - Stick kann jetzt abgezogen werden.";
          } else if (driveStatusEl) {
            driveStatusEl.textContent = data.error || "Auswerfen fehlgeschlagen.";
          }
        } catch (e) {
          if (driveStatusEl) driveStatusEl.textContent = "Auswerfen fehlgeschlagen.";
        } finally {
          unmountBtn.disabled = false;
        }
      });
    }

    if (formatBtn) {
      formatBtn.addEventListener("click", async () => {
        const opt = driveSelect.selectedOptions[0];
        const device = opt && opt.dataset.device;
        if (!device) {
          if (driveStatusEl) { driveStatusEl.hidden = false; driveStatusEl.textContent = "Kein Stick ausgewählt."; }
          return;
        }
        const label = opt.textContent.trim();
        const confirmed = window.confirm(
          "\"" + label + "\" wirklich komplett formatieren?\n\n" +
          "Alle Daten darauf werden unwiderruflich gelöscht - das lässt sich nicht rückgängig machen."
        );
        if (!confirmed) return;

        formatBtn.disabled = true;
        if (driveStatusEl) { driveStatusEl.hidden = false; driveStatusEl.textContent = "Formatiere... (nicht abziehen)"; }
        try {
          const res = await fetch("/api/usb-drives/format", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ device }),
          });
          const data = await res.json();
          if (res.ok) {
            const drives = await fetchDrives();
            renderDrives(drives, null);
            if (driveStatusEl) driveStatusEl.textContent = "Formatiert - bitte als Aufnahme-Ziel neu auswählen.";
          } else if (driveStatusEl) {
            driveStatusEl.textContent = data.error || "Formatieren fehlgeschlagen.";
          }
        } catch (e) {
          if (driveStatusEl) driveStatusEl.textContent = "Formatieren fehlgeschlagen.";
        } finally {
          formatBtn.disabled = false;
        }
      });
    }
  }

  // Netzwerk-Sektion (Teil der Settings-Seite): Netzwerke suchen, verbinden,
  // gespeicherte Profile entfernen.
  const scanBtn = document.getElementById("scan-networks");
  const scannedList = document.getElementById("scanned-networks");
  const savedList = document.getElementById("saved-networks");
  if (scanBtn && scannedList) {
    const connectCard = document.getElementById("wifi-connect-card");
    const connectSsidInput = document.getElementById("connect-ssid");
    const connectSsidLabel = document.getElementById("connect-ssid-label");
    const connectPasswordInput = document.getElementById("connect-password");
    const connectSubmitBtn = document.getElementById("connect-submit");
    const connectCancelBtn = document.getElementById("connect-cancel");
    const connectStatus = document.getElementById("wifi-connect-status");
    const activeConnectionsEl = document.getElementById("network-active-connections");
    const ipEl = document.getElementById("network-ip-addresses");

    function signalBar(percent) {
      const bar = document.createElement("span");
      bar.className = "signal-bar";
      const fill = document.createElement("span");
      fill.className = "signal-bar-fill";
      fill.style.width = Math.max(0, Math.min(100, percent)) + "%";
      bar.appendChild(fill);
      return bar;
    }

    function openConnectCard(ssid, secured) {
      connectSsidInput.value = ssid;
      connectSsidLabel.textContent = ssid;
      connectPasswordInput.value = "";
      connectPasswordInput.placeholder = secured ? "" : "offenes Netzwerk - kein Passwort nötig";
      connectStatus.hidden = true;
      connectCard.hidden = false;
      connectCard.scrollIntoView({ behavior: "smooth", block: "start" });
      if (secured) connectPasswordInput.focus();
    }

    scanBtn.addEventListener("click", async () => {
      scanBtn.disabled = true;
      scanBtn.textContent = "Suche...";
      scannedList.innerHTML = "<li class=\"hint\">Suche läuft...</li>";
      try {
        const res = await fetch("/api/wifi/scan", { method: "POST" });
        const networks = await res.json();
        scannedList.innerHTML = "";
        if (networks.length === 0) {
          scannedList.innerHTML = "<li class=\"hint\">Keine Netzwerke gefunden.</li>";
        }
        networks.forEach((net) => {
          const li = document.createElement("li");

          const nameSpan = document.createElement("span");
          nameSpan.className = "network-name";
          nameSpan.textContent = net.ssid + (net.secured ? "" : " (offen)");
          nameSpan.appendChild(signalBar(net.signal));

          const connectButton = document.createElement("button");
          connectButton.type = "button";
          connectButton.className = "btn btn-secondary";
          connectButton.textContent = "Verbinden";
          connectButton.addEventListener("click", () => openConnectCard(net.ssid, net.secured));

          li.appendChild(nameSpan);
          li.appendChild(connectButton);
          scannedList.appendChild(li);
        });
      } catch (e) {
        scannedList.innerHTML = "<li class=\"hint\">Suche fehlgeschlagen.</li>";
      } finally {
        scanBtn.disabled = false;
        scanBtn.textContent = "Suchen";
      }
    });

    async function submitConnect() {
      connectSubmitBtn.disabled = true;
      connectStatus.hidden = false;
      connectStatus.textContent = "Verbinde...";
      try {
        const res = await fetch("/api/wifi/connect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ssid: connectSsidInput.value,
            password: connectPasswordInput.value,
          }),
        });
        const data = await res.json();
        if (res.ok) {
          connectStatus.textContent = "Verbunden.";
          const statusRes = await fetch("/api/wifi/status");
          const status = await statusRes.json();
          if (activeConnectionsEl) {
            activeConnectionsEl.textContent = (status.active_connections || []).map((c) => c.label).join(", ") || "nicht verbunden";
          }
          if (ipEl) ipEl.textContent = (status.ip_addresses || []).join(", ") || "-";
          setTimeout(() => {
            connectCard.hidden = true;
          }, 1500);
        } else {
          connectStatus.textContent = data.error || "Verbindung fehlgeschlagen.";
        }
      } catch (e) {
        connectStatus.textContent = "Verbindung fehlgeschlagen.";
      } finally {
        connectSubmitBtn.disabled = false;
      }
    }

    if (connectSubmitBtn) {
      connectSubmitBtn.addEventListener("click", submitConnect);
    }
    if (connectPasswordInput) {
      // Enter im Passwortfeld soll verbinden, nicht das umschliessende
      // Settings-Formular absenden.
      connectPasswordInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          submitConnect();
        }
      });
    }
    if (connectCancelBtn) {
      connectCancelBtn.addEventListener("click", () => {
        connectCard.hidden = true;
      });
    }
  }

  if (savedList) {
    savedList.addEventListener("click", async (event) => {
      const btn = event.target.closest(".btn-forget");
      if (!btn) return;
      const name = btn.dataset.name;
      btn.disabled = true;
      try {
        const res = await fetch("/api/wifi/forget", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name }),
        });
        if (res.ok) {
          btn.closest("li").remove();
        } else {
          btn.disabled = false;
        }
      } catch (e) {
        btn.disabled = false;
      }
    });
  }

  // Netzwerk-Sektion: DHCP <-> Feste IP pro aktiver Verbindung.
  document.querySelectorAll(".ip-config").forEach((block) => {
    const connection = block.dataset.connection;
    const staticFields = block.querySelector(".ipv4-static-fields");
    const applyBtn = block.querySelector(".ipv4-apply");
    const statusEl = block.querySelector(".ipv4-status");
    if (!staticFields || !applyBtn) return;

    block.querySelectorAll(".ipv4-mode-radio").forEach((radio) => {
      radio.addEventListener("change", () => {
        staticFields.hidden = block.querySelector(".ipv4-mode-radio[value=manual]:checked") === null;
      });
    });

    applyBtn.addEventListener("click", async () => {
      const mode = block.querySelector(".ipv4-mode-radio[value=manual]:checked") ? "manual" : "auto";
      applyBtn.disabled = true;
      if (statusEl) {
        statusEl.hidden = false;
        statusEl.textContent = "Wird angewendet...";
      }
      try {
        let res;
        if (mode === "auto") {
          res = await fetch("/api/network/set-dhcp", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ connection }),
          });
        } else {
          const dnsRaw = block.querySelector(".ipv4-dns").value;
          res = await fetch("/api/network/set-static", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              connection,
              address: block.querySelector(".ipv4-address").value.trim(),
              prefix: parseInt(block.querySelector(".ipv4-prefix").value, 10),
              gateway: block.querySelector(".ipv4-gateway").value.trim(),
              dns: dnsRaw.split(",").map((s) => s.trim()).filter(Boolean),
            }),
          });
        }
        const data = await res.json();
        if (res.ok) {
          if (statusEl) statusEl.textContent = "Übernommen. Diese Seite kann jetzt kurz oder dauerhaft unter einer anderen Adresse laufen.";
        } else if (statusEl) {
          statusEl.textContent = data.error || "Fehlgeschlagen.";
        }
      } catch (e) {
        if (statusEl) statusEl.textContent = "Fehlgeschlagen (keine Antwort - Adresse evtl. bereits gewechselt).";
      } finally {
        applyBtn.disabled = false;
      }
    });
  });

  // Settings-Seite, ganz oben -> System: CPU-Last/-Temperatur, RAM,
  // SD-Karte als kompakte Stat-Kacheln in einer Reihe - rein informativ,
  // daher ohne Server-seitiges Rendering (wie der Stream-Status auch)
  // einfach per JS beim Laden befüllt und dann in grossem Abstand
  // aktualisiert (ändert sich nicht sekündlich). Jede Kachel zeigt einen
  // kompakten Prozentwert, RAM/SD-Karte zusätzlich klein darunter die
  // effektiven Byte-Werte (nicht nur %).
  function formatBytesAuto(bytes) {
    if (bytes === null || bytes === undefined) return "-";
    if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(1) + " GB";
    return Math.round(bytes / 1024 ** 2) + " MB";
  }

  const systemStatsEl = document.getElementById("system-stats");
  if (systemStatsEl) {
    const cpuLoadEl = document.getElementById("sys-cpu-load");
    const cpuTempEl = document.getElementById("sys-cpu-temp");
    const ramEl = document.getElementById("sys-ram");
    const ramDetailEl = document.getElementById("sys-ram-detail");
    const diskEl = document.getElementById("sys-disk");
    const diskDetailEl = document.getElementById("sys-disk-detail");

    async function refreshSystemStats() {
      try {
        const res = await fetch("/api/system/stats");
        if (!res.ok) return;
        const data = await res.json();
        if (cpuLoadEl) {
          if (data.load_average) {
            const [l1, l5, l15] = data.load_average;
            cpuLoadEl.textContent = l1.toFixed(2);
            cpuLoadEl.title = `5 Min.: ${l5.toFixed(2)} · 15 Min.: ${l15.toFixed(2)}`;
          } else {
            cpuLoadEl.textContent = "-";
            cpuLoadEl.title = "";
          }
        }
        if (cpuTempEl) {
          cpuTempEl.textContent =
            data.cpu_temp_celsius === null || data.cpu_temp_celsius === undefined
              ? "-"
              : data.cpu_temp_celsius.toFixed(1) + " °C";
        }
        if (ramEl) {
          if (data.memory) {
            ramEl.textContent = Math.round(data.memory.used_percent) + " %";
            if (ramDetailEl) {
              ramDetailEl.textContent =
                `${formatBytesAuto(data.memory.used_bytes)} / ${formatBytesAuto(data.memory.total_bytes)}`;
            }
          } else {
            ramEl.textContent = "-";
            if (ramDetailEl) ramDetailEl.textContent = "";
          }
        }
        if (diskEl) {
          if (data.disk) {
            diskEl.textContent = Math.round(data.disk.used_percent) + " %";
            if (diskDetailEl) {
              diskDetailEl.textContent =
                `${formatGB(data.disk.used_bytes)} / ${formatGB(data.disk.total_bytes)}`;
            }
          } else {
            diskEl.textContent = "-";
            if (diskDetailEl) diskDetailEl.textContent = "";
          }
        }
      } catch (e) {
        // Netzwerkfehler beim Polling ignorieren, naechster Tick versucht's erneut
      }
    }
    refreshSystemStats();
    setInterval(refreshSystemStats, 10000);
  }

  // Power: sauberes Herunterfahren/Neustarten statt am Eventende den
  // Stecker zu ziehen (siehe README) - zwei Icon-Knöpfe im Header, nur
  // sichtbar, während man sich in den Einstellungen befindet (siehe
  // base.html), bewusst ohne eigene Statuszeile - der Tooltip/Titel des
  // Knopfs selbst zeigt den Fortschritt, Fehler kommen per alert().
  const rebootBtn = document.getElementById("btn-reboot");
  const poweroffBtn = document.getElementById("btn-poweroff");

  async function triggerPower(action, confirmText, busyTitle) {
    if (!window.confirm(confirmText)) return;
    if (rebootBtn) rebootBtn.disabled = true;
    if (poweroffBtn) poweroffBtn.disabled = true;
    const activeBtn = action === "reboot" ? rebootBtn : poweroffBtn;
    const originalBtnTitle = activeBtn ? activeBtn.title : "";
    if (activeBtn) activeBtn.title = busyTitle;
    try {
      const res = await fetch("/api/system/power", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      const data = await res.json();
      if (!res.ok) {
        window.alert(data.error || "Fehlgeschlagen.");
        if (rebootBtn) rebootBtn.disabled = false;
        if (poweroffBtn) poweroffBtn.disabled = false;
        if (activeBtn) activeBtn.title = originalBtnTitle;
      }
      // Bei Erfolg bleiben die Knöpfe bewusst deaktiviert - die Seite ist
      // gleich ohnehin nicht mehr (bzw. bei Neustart erst nach einer Weile
      // wieder) erreichbar.
    } catch (e) {
      // Keine Antwort mehr - vermutlich ist die Aktion bereits im Gange
      // (Verbindung fällt weg, bevor die HTTP-Antwort ankommt). Knöpfe
      // bleiben absichtlich deaktiviert.
    }
  }

  if (rebootBtn) {
    rebootBtn.addEventListener("click", () =>
      triggerPower(
        "reboot",
        "Gerät jetzt neu starten? Ein laufender Stream/eine laufende Aufnahme wird zuerst automatisch gestoppt.",
        "Startet neu..."
      )
    );
  }
  if (poweroffBtn) {
    poweroffBtn.addEventListener("click", () =>
      triggerPower(
        "poweroff",
        "Gerät jetzt herunterfahren? Ein laufender Stream/eine laufende Aufnahme wird zuerst automatisch " +
          "gestoppt. Danach lässt es sich nur noch über den Netzschalter/Stecker wieder starten.",
        "Fährt herunter..."
      )
    );
  }
})();
