// RunPod VideoGen Studio Frontend Controller

document.addEventListener("DOMContentLoaded", () => {
  // Elements
  const statusPill = document.getElementById("statusPill");
  const statusText = document.getElementById("statusText");
  const accountEmail = document.getElementById("accountEmail");
  const podDetailStatus = document.getElementById("podDetailStatus");

  const btnStart = document.getElementById("btnStart");
  const btnPause = document.getElementById("btnPause");
  const btnTerminate = document.getElementById("btnTerminate");
  const btnRefresh = document.getElementById("btnRefresh");
  const btnOpenWebUI = document.getElementById("btnOpenWebUI");

  const modeStopBtn = document.getElementById("modeStopBtn");
  const modeTermBtn = document.getElementById("modeTermBtn");

  const podDetailsBox = document.getElementById("podDetailsBox");
  const podConnectionText = document.getElementById("podConnectionText");
  const podProxyLink = document.getElementById("podProxyLink");

  const sessionTimer = document.getElementById("sessionTimer");
  const hourlyRateDisplay = document.getElementById("hourlyRateDisplay");
  const sessionCostDisplay = document.getElementById("sessionCostDisplay");
  const autoShutdownSelect = document.getElementById("autoShutdownSelect");

  const gpuSelect = document.getElementById("gpuSelect");
  const cloudTypeSelect = document.getElementById("cloudTypeSelect");
  const volumeDiskDisplay = document.getElementById("volumeDiskDisplay");

  const galleryGrid = document.getElementById("galleryGrid");
  const btnSyncNow = document.getElementById("btnSyncNow");
  const btnOpenFolder = document.getElementById("btnOpenFolder");

  const terminalLog = document.getElementById("terminalLog");
  const btnClearLogs = document.getElementById("btnClearLogs");
  const btnCopyLogs = document.getElementById("btnCopyLogs");

  // Modals
  const settingsModal = document.getElementById("settingsModal");
  const openSettingsBtn = document.getElementById("openSettingsBtn");
  const closeSettingsBtn = document.getElementById("closeSettingsBtn");
  const cancelSettingsBtn = document.getElementById("cancelSettingsBtn");
  const saveSettingsBtn = document.getElementById("saveSettingsBtn");
  const cfgApiKey = document.getElementById("cfgApiKey");
  const cfgEmail = document.getElementById("cfgEmail");

  const videoModal = document.getElementById("videoModal");
  const closeVideoModalBtn = document.getElementById("closeVideoModalBtn");
  const videoModalTitle = document.getElementById("videoModalTitle");
  const videoModalMeta = document.getElementById("videoModalMeta");
  const modalVideoPlayer = document.getElementById("modalVideoPlayer");
  const videoDownloadBtn = document.getElementById("videoDownloadBtn");

  // Local State
  let currentUptime = 0;
  let currentHourlyRate = 0.34;
  let timerInterval = null;
  let activeProxyUrl = null;

  // Format Seconds to HH:MM:SS
  function formatTime(seconds) {
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    return `${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }

  // Update Live Stopwatch
  function tickTimer() {
    if (currentUptime > 0) {
      currentUptime++;
      sessionTimer.textContent = formatTime(currentUptime);
      const cost = (currentUptime / 3600.0) * currentHourlyRate;
      sessionCostDisplay.textContent = `$${cost.toFixed(4)}`;
    }
  }

  // Fetch Status
  async function fetchStatus() {
    try {
      const res = await fetch("/api/status");
      if (!res.ok) return;
      const data = await res.json();

      // Update Pill
      statusPill.className = `status-pill ${data.status}`;
      statusText.textContent = data.status;
      podDetailStatus.textContent = data.status_detail || data.status;

      // Account
      if (data.account && data.account.email) {
        accountEmail.textContent = data.account.email;
        cfgEmail.value = data.account.email;
      }

      // Cost & Timer
      currentHourlyRate = data.hourly_rate || 0.34;
      hourlyRateDisplay.textContent = `$${currentHourlyRate.toFixed(2)} /h`;
      
      if (data.status === "RUNNING") {
        currentUptime = data.uptime_seconds || 0;
        sessionTimer.textContent = formatTime(currentUptime);
        sessionCostDisplay.textContent = `$${(data.accumulated_cost || 0).toFixed(4)}`;
        if (!timerInterval) {
          timerInterval = setInterval(tickTimer, 1000);
        }
      } else {
        if (timerInterval) {
          clearInterval(timerInterval);
          timerInterval = null;
        }
        if (data.status === "OFFLINE" || data.status === "TERMINATED") {
          currentUptime = 0;
          sessionTimer.textContent = "00:00:00";
          sessionCostDisplay.textContent = "$0.0000";
        }
      }

      // Standby Mode
      if (data.standby_mode === "terminate") {
        modeTermBtn.classList.add("active");
        modeStopBtn.classList.remove("active");
      } else {
        modeStopBtn.classList.add("active");
        modeTermBtn.classList.remove("active");
      }

      // Hardware / AutoShutdown
      if (data.config) {
        if (data.config.auto_shutdown_minutes !== undefined) {
          autoShutdownSelect.value = String(data.config.auto_shutdown_minutes);
        }
        if (data.config.volume_disk_gb) {
          volumeDiskDisplay.textContent = data.config.volume_disk_gb;
        }
      }

      // Pod Connection Details
      if (data.pod_id && (data.status === "RUNNING" || data.status === "BOOTSTRAPPING" || data.status === "STOPPED")) {
        podDetailsBox.style.display = "flex";
        podConnectionText.textContent = `Pod-ID: ${data.pod_id.substring(0, 14)}... | IP: ${data.pod_ip || 'Warten...'} | SSH: ${data.pod_ssh_port || '-'}`;
        if (data.proxy_url) {
          podProxyLink.href = data.proxy_url;
          podProxyLink.style.display = "inline-flex";
          activeProxyUrl = data.proxy_url;
        } else {
          podProxyLink.style.display = "none";
        }
      } else {
        podDetailsBox.style.display = "none";
        activeProxyUrl = null;
      }

      // Button States
      if (data.status === "RUNNING") {
        btnStart.disabled = true;
        btnStart.innerHTML = "<span>🚀</span> Pod läuft";
        btnPause.disabled = false;
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = false;
      } else if (data.status === "STARTING" || data.status === "BOOTSTRAPPING") {
        btnStart.disabled = true;
        btnStart.innerHTML = "<span>⏳</span> Startet...";
        btnPause.disabled = true;
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = true;
      } else if (data.status === "STOPPED") {
        btnStart.disabled = false;
        btnStart.innerHTML = "<span>⚡</span> Pod Aufwecken (Resume)";
        btnPause.disabled = true;
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = true;
      } else { // OFFLINE, TERMINATED, ERROR
        btnStart.disabled = false;
        btnStart.innerHTML = "<span>🚀</span> Pod Starten";
        btnPause.disabled = true;
        btnTerminate.disabled = true;
        btnOpenWebUI.disabled = true;
      }

    } catch (err) {
      console.error("Status fetch error:", err);
    }
  }

  // Fetch Available GPUs
  async function fetchGpus() {
    try {
      const res = await fetch("/api/gpus");
      if (!res.ok) return;
      const data = await res.json();
      if (!data.gpus || !data.gpus.length) return;

      const currentVal = gpuSelect.value;
      gpuSelect.innerHTML = "";
      data.gpus.forEach(g => {
        const opt = document.createElement("option");
        opt.value = g.id;
        const commPrice = g.communityPrice ? `$${g.communityPrice.toFixed(2)}` : "n/a";
        const secPrice = g.securePrice ? `$${g.securePrice.toFixed(2)}` : "n/a";
        opt.textContent = `${g.displayName || g.id} (${g.vram} GB) • Community: ${commPrice}/h | Secure: ${secPrice}/h`;
        gpuSelect.appendChild(opt);
      });

      // Restore selection if exists
      if (Array.from(gpuSelect.options).some(o => o.value === currentVal)) {
        gpuSelect.value = currentVal;
      } else if (Array.from(gpuSelect.options).some(o => o.value === "NVIDIA GeForce RTX 4090")) {
        gpuSelect.value = "NVIDIA GeForce RTX 4090";
      }
    } catch (err) {
      console.error("GPU fetch error:", err);
    }
  }

  // Fetch Video Gallery
  async function fetchVideos() {
    try {
      const res = await fetch("/api/videos");
      if (!res.ok) return;
      const data = await res.json();
      
      if (!data.videos || data.videos.length === 0) {
        galleryGrid.innerHTML = `
          <div style="grid-column: 1/-1; text-align: center; padding: 40px; color: var(--text-dim);">
            Noch keine generierten Videos im lokalen outputs/-Ordner gefunden.<br>
            Sobald dein Pod ein Video fertigstellt, wird es automatisch hierher synchronisiert.
          </div>
        `;
        return;
      }

      galleryGrid.innerHTML = "";
      data.videos.forEach(v => {
        const card = document.createElement("div");
        card.className = "video-card";
        const dateStr = new Date(v.modified * 1000).toLocaleString("de-DE");

        card.innerHTML = `
          <div class="video-thumb" data-url="${v.url}" data-title="${v.filename}" data-meta="${v.size_mb} MB • ${dateStr}">
            <video src="${v.url}#t=0.5" preload="metadata"></video>
            <div class="play-overlay">
              <svg width="18" height="18" fill="#fff" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            </div>
          </div>
          <div class="video-info">
            <div class="video-title" title="${v.filename}">${v.filename}</div>
            <div class="video-meta">
              <span>${v.size_mb} MB</span>
              <span>${dateStr}</span>
            </div>
          </div>
        `;

        card.querySelector(".video-thumb").addEventListener("click", () => {
          openVideoModal(v.url, v.filename, `${v.size_mb} MB • ${dateStr}`);
        });

        galleryGrid.appendChild(card);
      });
    } catch (err) {
      console.error("Video gallery fetch error:", err);
    }
  }

  function openVideoModal(url, title, meta) {
    videoModalTitle.textContent = title;
    videoModalMeta.textContent = meta;
    modalVideoPlayer.src = url;
    videoDownloadBtn.href = url;
    videoDownloadBtn.setAttribute("download", title);
    videoModal.classList.add("active");
    modalVideoPlayer.play().catch(() => {});
  }

  function closeVideoModal() {
    videoModal.classList.remove("active");
    modalVideoPlayer.pause();
    modalVideoPlayer.src = "";
  }

  closeVideoModalBtn.addEventListener("click", closeVideoModal);
  videoModal.addEventListener("click", (e) => {
    if (e.target === videoModal) closeVideoModal();
  });

  // Action Button Listeners
  btnStart.addEventListener("click", async () => {
    btnStart.disabled = true;
    try {
      const res = await fetch("/api/pod/start", { method: "POST" });
      const data = await res.json();
      if (!res.ok) alert("Fehler: " + data.detail);
      fetchStatus();
    } catch (err) {
      alert("Netzwerkfehler: " + err);
    }
  });

  btnPause.addEventListener("click", async () => {
    if (!confirm("GPU pausieren? Die GPU-Stundenkosten stoppen sofort. Die Festplatte bleibt für ~0,15$/Tag erhalten.")) return;
    try {
      const res = await fetch("/api/pod/stop", { method: "POST" });
      const data = await res.json();
      if (!res.ok) alert("Fehler: " + data.detail);
      fetchStatus();
    } catch (err) {
      alert("Netzwerkfehler: " + err);
    }
  });

  btnTerminate.addEventListener("click", async () => {
    if (!confirm("ACHTUNG: Pod vollständig terminieren (löschen)?\n\n- Standby-Kosten sinken auf exakt 0,00 €.\n- Das Cloud-Volume wird gelöscht.\n- Beim nächsten Start müssen Modelle neu geladen werden (~6 Min).")) return;
    try {
      const res = await fetch("/api/pod/terminate", { method: "POST" });
      const data = await res.json();
      if (!res.ok) alert("Fehler: " + data.detail);
      fetchStatus();
    } catch (err) {
      alert("Netzwerkfehler: " + err);
    }
  });

  btnRefresh.addEventListener("click", fetchStatus);

  btnOpenWebUI.addEventListener("click", () => {
    if (activeProxyUrl) {
      window.open(activeProxyUrl, "_blank");
    } else {
      alert("Proxy-URL noch nicht verfügbar.");
    }
  });

  // Standby Mode Switch
  modeStopBtn.addEventListener("click", async () => {
    modeStopBtn.classList.add("active");
    modeTermBtn.classList.remove("active");
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ standby_mode: "stop" })
    });
  });

  modeTermBtn.addEventListener("click", async () => {
    modeTermBtn.classList.add("active");
    modeStopBtn.classList.remove("active");
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ standby_mode: "terminate" })
    });
  });

  // Auto-Shutdown change
  autoShutdownSelect.addEventListener("change", async () => {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_shutdown_minutes: parseInt(autoShutdownSelect.value) })
    });
  });

  // Hardware change
  gpuSelect.addEventListener("change", async () => {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ gpu_type_id: gpuSelect.value })
    });
  });

  cloudTypeSelect.addEventListener("change", async () => {
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cloud_type: cloudTypeSelect.value })
    });
  });

  // Explorer Open
  btnOpenFolder.addEventListener("click", async () => {
    await fetch("/api/videos/open-folder", { method: "POST" });
  });

  btnSyncNow.addEventListener("click", fetchVideos);

  // Settings Modal
  openSettingsBtn.addEventListener("click", async () => {
    try {
      const res = await fetch("/api/config");
      if (res.ok) {
        const cfg = await res.json();
        cfgApiKey.value = cfg.runpod_api_key || "";
        cfgEmail.value = cfg.account_email || "";
      }
    } catch (_) {}
    settingsModal.classList.add("active");
  });

  function closeSettings() {
    settingsModal.classList.remove("active");
  }

  closeSettingsBtn.addEventListener("click", closeSettings);
  cancelSettingsBtn.addEventListener("click", closeSettings);

  saveSettingsBtn.addEventListener("click", async () => {
    const key = cfgApiKey.value.trim();
    if (!key) {
      alert("API Key darf nicht leer sein.");
      return;
    }
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ runpod_api_key: key })
    });
    closeSettings();
    fetchStatus();
  });

  // Terminal Logs
  btnClearLogs.addEventListener("click", () => {
    terminalLog.textContent = "";
  });

  btnCopyLogs.addEventListener("click", () => {
    navigator.clipboard.writeText(terminalLog.textContent);
    alert("Logs in die Zwischenablage kopiert!");
  });

  // WebSocket Live Log Stream
  function connectLogsWebSocket() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/logs`);

    ws.onopen = () => {
      terminalLog.textContent += "\n[SYSTEM] Verbunden mit Live-Log Stream.\n";
    };

    ws.onmessage = (event) => {
      terminalLog.textContent += event.data + "\n";
      terminalLog.scrollTop = terminalLog.scrollHeight;
    };

    ws.onclose = () => {
      setTimeout(connectLogsWebSocket, 4000);
    };
  }

  // Init
  fetchStatus();
  fetchGpus();
  fetchVideos();
  connectLogsWebSocket();

  setInterval(fetchStatus, 3000);
  setInterval(fetchVideos, 10000);
});
