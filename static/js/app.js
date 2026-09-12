// RunPod VideoGen Studio Frontend Controller

document.addEventListener("DOMContentLoaded", () => {
  // Ampel & Cost Cockpit Elements
  const costCockpitCard = document.querySelector(".cost-cockpit-card");
  const ampelRed = document.getElementById("ampelRed");
  const ampelYellow = document.getElementById("ampelYellow");
  const ampelGreen = document.getElementById("ampelGreen");

  const ampelStatusTag = document.getElementById("ampelStatusTag");
  const ampelHeadline = document.getElementById("ampelHeadline");
  const ampelDesc = document.getElementById("ampelDesc");

  const cockpitHourlyRate = document.getElementById("cockpitHourlyRate");
  const cockpitDailyRate = document.getElementById("cockpitDailyRate");
  const cockpitWeeklyRate = document.getElementById("cockpitWeeklyRate");
  const cockpitDiskMonthly = document.getElementById("cockpitDiskMonthly");
  const cockpitRunTimeRemaining = document.getElementById("cockpitRunTimeRemaining");
  const displayBalance = document.getElementById("displayBalance");
  const btnRefreshBalance = document.getElementById("btnRefreshBalance");

  // Workflow Stepper & Guidance Elements
  const step1 = document.getElementById("step1");
  const step2 = document.getElementById("step2");
  const step3 = document.getElementById("step3");
  const step4 = document.getElementById("step4");
  const step1Status = document.getElementById("step1Status");
  const step2Status = document.getElementById("step2Status");
  const step3Status = document.getElementById("step3Status");
  const step4Status = document.getElementById("step4Status");
  const line1 = document.getElementById("line1");
  const line2 = document.getElementById("line2");
  const line3 = document.getElementById("line3");

  const guidanceBanner = document.getElementById("guidanceBanner");
  const guidanceIcon = document.getElementById("guidanceIcon");
  const guidanceTitle = document.getElementById("guidanceTitle");
  const guidanceDesc = document.getElementById("guidanceDesc");
  const guidanceAction = document.getElementById("guidanceAction");

  // Header & Status Elements
  const statusPill = document.getElementById("statusPill");
  const statusText = document.getElementById("statusText");
  const accountEmail = document.getElementById("accountEmail");
  const podDetailStatus = document.getElementById("podDetailStatus");

  // Action Buttons
  const btnStart = document.getElementById("btnStart");
  const btnPause = document.getElementById("btnPause");
  const btnTerminate = document.getElementById("btnTerminate");
  const btnRefresh = document.getElementById("btnRefresh");
  const btnOpenWebUI = document.getElementById("btnOpenWebUI");

  const btnCockpitPause = document.getElementById("btnCockpitPause");
  const btnCockpitResume = document.getElementById("btnCockpitResume");
  const btnCockpitTerminate = document.getElementById("btnCockpitTerminate");

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
  const cfgBalance = document.getElementById("cfgBalance");

  const videoModal = document.getElementById("videoModal");
  const closeVideoModalBtn = document.getElementById("closeVideoModalBtn");
  const videoModalTitle = document.getElementById("videoModalTitle");
  const videoModalMeta = document.getElementById("videoModalMeta");
  const modalVideoPlayer = document.getElementById("modalVideoPlayer");
  const videoDownloadBtn = document.getElementById("videoDownloadBtn");
  const modalDeleteBtn = document.getElementById("modalDeleteBtn");
  let currentModalFilename = "";

  // Cost & Status Info Modal
  const costInfoModal = document.getElementById("costInfoModal");
  const btnHeaderCostInfo = document.getElementById("btnHeaderCostInfo");
  const btnCockpitCostInfo = document.getElementById("btnCockpitCostInfo");
  const closeCostInfoModalBtn = document.getElementById("closeCostInfoModalBtn");
  const closeCostInfoModalBtn2 = document.getElementById("closeCostInfoModalBtn2");

  // Local State
  let currentUptime = 0;
  let currentHourlyRate = 0.34;
  let timerInterval = null;
  let activeProxyUrl = null;
  let currentBalance = 18.74;

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

  // Fetch Status & Update Cockpit
  async function fetchStatus() {
    try {
      const res = await fetch("/api/status");
      if (!res.ok) return;
      const data = await res.json();

      // Update Pill
      statusPill.className = `status-pill ${data.status}`;
      statusText.textContent = data.status;
      podDetailStatus.textContent = data.status_detail || data.status;

      // Account & Balance
      if (data.account) {
        if (data.account.email) {
          accountEmail.textContent = data.account.email;
          cfgEmail.value = data.account.email;
        }
        if (data.account.balance !== undefined) {
          currentBalance = parseFloat(data.account.balance);
          displayBalance.textContent = currentBalance.toFixed(2);
          cfgBalance.value = currentBalance.toFixed(2);
        }
      }

      // Cost Calculation Variables
      currentHourlyRate = data.hourly_rate || 0.34;
      hourlyRateDisplay.textContent = `$${currentHourlyRate.toFixed(2)} /h`;
      
      const volumeGb = data.config && data.config.volume_disk_gb ? data.config.volume_disk_gb : 75;
      const diskMonthly = volumeGb * 0.07; // RunPod storage: $0.07/GB/mo (~$5.25 for 75GB)
      const diskDaily = diskMonthly / 30.0; // ~$0.175 / day
      const diskWeekly = diskDaily * 7.0; // ~$1.225 / week
      const diskHourly = diskMonthly / 720.0; // ~$0.00729 / h

      const status = data.status;

      // ==========================================
      // AMPELSYSTEM & DICKE STATUSANZEIGE
      // ==========================================
      if (status === "RUNNING" || status === "STARTING" || status === "BOOTSTRAPPING") {
        // 🔴 ROT: GPU Aktiv
        ampelRed.classList.add("active");
        ampelYellow.classList.remove("active");
        ampelGreen.classList.remove("active");
        if (costCockpitCard) costCockpitCard.className = "card cost-cockpit-card state-red";

        ampelStatusTag.className = "ampel-status-tag red";
        ampelStatusTag.textContent = "🔴 GPU AKTIV – RECHENZEIT LÄUFT";
        ampelHeadline.textContent = "GPU rechnet aktuell";
        ampelDesc.textContent = "Die Grafikkarte ist eingeschaltet und rechnet. Pausiere den Pod mit 'GPU Pausieren', sobald deine Videos generiert sind, um GPU-Kosten zu stoppen.";

        const totalHourly = currentHourlyRate + diskHourly;
        const totalDaily = (currentHourlyRate * 24) + diskDaily;
        const totalWeekly = (currentHourlyRate * 168) + diskWeekly;

        cockpitHourlyRate.innerHTML = `$${totalHourly.toFixed(3)} <span class="rate-unit">/ Std.</span>`;
        cockpitDailyRate.textContent = `$${totalDaily.toFixed(2)}`;
        cockpitWeeklyRate.textContent = `$${totalWeekly.toFixed(2)}`;
        cockpitDiskMonthly.textContent = `$${diskMonthly.toFixed(2)}`;

        const hoursLeft = currentHourlyRate > 0 ? (currentBalance / currentHourlyRate).toFixed(1) : "0";
        cockpitRunTimeRemaining.textContent = `~${hoursLeft}h GPU`;

      } else if (status === "STOPPED") {
        // 🟡 GELB: Standby (GPU aus, Modelle erhalten)
        ampelRed.classList.remove("active");
        ampelYellow.classList.add("active");
        ampelGreen.classList.remove("active");
        if (costCockpitCard) costCockpitCard.className = "card cost-cockpit-card state-yellow";

        ampelStatusTag.className = "ampel-status-tag yellow";
        ampelStatusTag.textContent = "🟡 STANDBY (GPU AUS, 30GB MODELLE ERHALTEN)";
        ampelHeadline.textContent = "GPU pausiert (0,00 € GPU-Kosten)";
        ampelDesc.textContent = `GPU ist abgeschaltet (0,00 $/h). Deine 30 GB MiniMax H3 Modellgewichte bleiben auf der ${volumeGb} GB Cloud-Platte gespeichert (~${(diskDaily * 100).toFixed(0)}¢/Tag), sodass du beim nächsten Klick sofort in 45s startklar bist!`;

        cockpitHourlyRate.innerHTML = `$${diskHourly.toFixed(3)} <span class="rate-unit">/ Std.</span>`;
        cockpitDailyRate.textContent = `$${diskDaily.toFixed(2)}`;
        cockpitWeeklyRate.textContent = `$${diskWeekly.toFixed(2)}`;
        cockpitDiskMonthly.textContent = `$${diskMonthly.toFixed(2)}`;

        const daysLeft = diskDaily > 0 ? (currentBalance / diskDaily).toFixed(0) : "0";
        cockpitRunTimeRemaining.textContent = `~${daysLeft} Tage Standby`;

      } else if (status === "ERROR") {
        // ⚠️ ERROR Zustand
        ampelRed.classList.add("active");
        ampelYellow.classList.remove("active");
        ampelGreen.classList.remove("active");
        if (costCockpitCard) costCockpitCard.className = "card cost-cockpit-card state-red";

        ampelStatusTag.className = "ampel-status-tag red";
        ampelStatusTag.textContent = "⚠️ FEHLER IM POD-SETUP";
        ampelHeadline.textContent = "Status prüfen & intervenieren";
        ampelDesc.textContent = data.status_detail || "Beim Starten oder Ausführen des Pods ist ein Fehler aufgetreten. Überprüfe die Terminal-Logs unten oder lösche den Pod.";

        cockpitHourlyRate.innerHTML = `$${currentHourlyRate.toFixed(3)} <span class="rate-unit">/ Std. (falls Pod läuft)</span>`;
        cockpitDailyRate.textContent = "-";
        cockpitWeeklyRate.textContent = "-";
        cockpitDiskMonthly.textContent = `$${diskMonthly.toFixed(2)}`;
        cockpitRunTimeRemaining.textContent = "Intervention nötig";

      } else {
        // 🟢 GRÜN: Null-Kosten (Aus & Gelöscht)
        ampelRed.classList.remove("active");
        ampelYellow.classList.remove("active");
        ampelGreen.classList.add("active");
        if (costCockpitCard) costCockpitCard.className = "card cost-cockpit-card state-green";

        ampelStatusTag.className = "ampel-status-tag green";
        ampelStatusTag.textContent = "🟢 0,- € NULL-KOSTEN MODUS";
        ampelHeadline.textContent = "Keine laufenden Kosten";
        ampelDesc.textContent = "Es ist aktuell kein Pod aktiv und kein Cloud-Speicher reserviert. Dein Guthaben bleibt zu 100% unangetastet.";

        cockpitHourlyRate.innerHTML = `$0.000 <span class="rate-unit">/ Std.</span>`;
        cockpitDailyRate.textContent = "$0.00";
        cockpitWeeklyRate.textContent = "$0.00";
        cockpitDiskMonthly.textContent = "$0.00";

        const potentialHours = currentHourlyRate > 0 ? (currentBalance / currentHourlyRate).toFixed(0) : "54";
        cockpitRunTimeRemaining.textContent = `~${potentialHours}h GPU möglich`;
      }

      // Live Session Timer
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

      // Standby Mode Switch
      if (data.standby_mode === "terminate") {
        modeTermBtn.classList.add("active");
        modeStopBtn.classList.remove("active");
      } else {
        modeStopBtn.classList.add("active");
        modeTermBtn.classList.remove("active");
      }

      // Hardware / AutoShutdown
      if (data.config) {
        if (data.config.gpu_type_id && gpuSelect && Array.from(gpuSelect.options).some(o => o.value === data.config.gpu_type_id)) {
          if (document.activeElement !== gpuSelect) {
            gpuSelect.value = data.config.gpu_type_id;
          }
        }
        if (data.config.cloud_type && cloudTypeSelect && document.activeElement !== cloudTypeSelect) {
          cloudTypeSelect.value = data.config.cloud_type;
        }
        if (data.config.auto_shutdown_minutes !== undefined && document.activeElement !== autoShutdownSelect) {
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
        if (btnCockpitPause) btnCockpitPause.style.display = "inline-flex";
        if (btnCockpitResume) btnCockpitResume.style.display = "none";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = "inline-flex";
      } else if (data.status === "STARTING" || data.status === "BOOTSTRAPPING") {
        btnStart.disabled = true;
        btnStart.innerHTML = "<span>⏳</span> Startet...";
        btnPause.disabled = true;
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = true;
        if (btnCockpitPause) btnCockpitPause.style.display = "none";
        if (btnCockpitResume) btnCockpitResume.style.display = "none";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = "inline-flex";
      } else if (data.status === "STOPPED") {
        btnStart.disabled = false;
        btnStart.innerHTML = "<span>⚡</span> Pod Aufwecken (Resume 45s)";
        btnPause.disabled = true;
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = true;
        if (btnCockpitPause) btnCockpitPause.style.display = "none";
        if (btnCockpitResume) btnCockpitResume.style.display = "inline-flex";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = "inline-flex";
      } else { // OFFLINE, TERMINATED, ERROR
        btnStart.disabled = false;
        btnStart.innerHTML = "<span>🚀</span> Pod Starten";
        btnPause.disabled = true;
        btnTerminate.disabled = !data.pod_id;
        btnOpenWebUI.disabled = true;
        if (btnCockpitPause) btnCockpitPause.style.display = "none";
        if (btnCockpitResume) btnCockpitResume.style.display = "none";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = data.pod_id ? "inline-flex" : "none";
      }

      // Update Step-by-Step Guidance & Stepper
      updateWorkflowGuidance(data);

    } catch (err) {
      console.error("Status fetch error:", err);
    }
  }

  // Dynamic Workflow Guidance & Stepper
  function updateWorkflowGuidance(data) {
    const status = data.status;
    const detail = data.status_detail || "";

    // Reset lines
    if (line1) line1.className = "step-line";
    if (line2) line2.className = "step-line";
    if (line3) line3.className = "step-line";

    if (status === "ERROR") {
      const isOutOfStock = detail.toLowerCase().includes("no longer any instances available") ||
                           detail.toLowerCase().includes("ausgebucht") ||
                           (data.recent_logs && data.recent_logs.some(l => l.toLowerCase().includes("no longer any instances available")));

      if (step1) {
        step1.className = "step-item active error";
        if (step1Status) step1Status.textContent = isOutOfStock ? "GPU vergriffen" : "Fehler";
      }
      if (step2) { step2.className = "step-item"; if (step2Status) step2Status.textContent = "Wartet..."; }
      if (step3) { step3.className = "step-item"; if (step3Status) step3Status.textContent = "Wartet..."; }
      if (step4) { step4.className = "step-item"; if (step4Status) step4Status.textContent = "Wartet..."; }

      if (guidanceBanner) guidanceBanner.className = "guidance-banner error";
      if (guidanceIcon) guidanceIcon.textContent = "⚠️";

      if (isOutOfStock) {
        if (guidanceTitle) guidanceTitle.textContent = "Start abgebrochen: RTX 4090 aktuell ausgebucht!";
        if (guidanceDesc) guidanceDesc.textContent = "In der Community Cloud ist die 4090 gerade belegt (0€ Kosten). Wechsle direkt auf die NVIDIA A40 (48 GB VRAM, ~$0.35/h, doppelt so viel VRAM!) oder versuche Secure Cloud:";
        if (guidanceAction) {
          guidanceAction.innerHTML = `
            <button class="guidance-btn" id="btnGuidanceA40">👉 Auf A40 (48GB) wechseln & Starten</button>
            <button class="guidance-btn btn-secondary" id="btnGuidanceSecure">🔒 Secure Cloud versuchen</button>
            <button class="guidance-btn btn-secondary" id="btnGuidanceRetry">🔄 Nochmal versuchen</button>
          `;

          document.getElementById("btnGuidanceA40")?.addEventListener("click", async () => {
            gpuSelect.value = "NVIDIA A40";
            await fetch("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ gpu_type_id: "NVIDIA A40" })
            });
            btnStart.click();
          });

          document.getElementById("btnGuidanceSecure")?.addEventListener("click", async () => {
            cloudTypeSelect.value = "SECURE";
            await fetch("/api/config", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ cloud_type: "SECURE" })
            });
            btnStart.click();
          });

          document.getElementById("btnGuidanceRetry")?.addEventListener("click", () => {
            btnStart.click();
          });
        }
      } else {
        if (guidanceTitle) guidanceTitle.textContent = "Fehler aufgetreten:";
        if (guidanceDesc) guidanceDesc.textContent = detail || "Fehler beim Starten. Bitte Terminal prüfen.";
        if (guidanceAction) {
          guidanceAction.innerHTML = `<button class="guidance-btn" id="btnGuidanceRetry">🔄 Erneut versuchen</button>`;
          document.getElementById("btnGuidanceRetry")?.addEventListener("click", () => {
            btnStart.click();
          });
        }
      }

    } else if (status === "STARTING") {
      if (step1) {
        step1.className = "step-item active";
        if (step1Status) step1Status.textContent = "GPU wird gebucht...";
      }
      if (line1) line1.className = "step-line active";
      if (step2) { step2.className = "step-item"; if (step2Status) step2Status.textContent = "Wartet..."; }
      if (step3) { step3.className = "step-item"; if (step3Status) step3Status.textContent = "Wartet..."; }
      if (step4) { step4.className = "step-item"; if (step4Status) step4Status.textContent = "Wartet..."; }

      if (guidanceBanner) guidanceBanner.className = "guidance-banner info";
      if (guidanceIcon) guidanceIcon.textContent = "⏳";
      if (guidanceTitle) guidanceTitle.textContent = "Schritt 1 von 4: Pod wird bereitgestellt...";
      if (guidanceDesc) guidanceDesc.textContent = "RunPod weist die GPU zu und startet den PyTorch-Container (ca. 30–60 Sek.). Bitte kurz warten...";
      if (guidanceAction) guidanceAction.innerHTML = `<span style="font-size:0.75rem; color:var(--primary); font-weight:700;">⏳ Cloud-Provisionierung läuft...</span>`;

    } else if (status === "BOOTSTRAPPING") {
      if (step1) {
        step1.className = "step-item done";
        if (step1Status) step1Status.textContent = "Pod gebucht ✓";
      }
      if (line1) line1.className = "step-line done";
      if (step2) {
        step2.className = "step-item active";
        if (step2Status) step2Status.textContent = "Wan2GP & Modell-Setup...";
      }
      if (line2) line2.className = "step-line active";
      if (step3) { step3.className = "step-item"; if (step3Status) step3Status.textContent = "Wartet..."; }
      if (step4) { step4.className = "step-item"; if (step4Status) step4Status.textContent = "Wartet..."; }

      if (guidanceBanner) guidanceBanner.className = "guidance-banner info";
      if (guidanceIcon) guidanceIcon.textContent = "⚙️";
      if (guidanceTitle) guidanceTitle.textContent = "Schritt 2 von 4: Wan2GP & MiniMax H3 Modell einrichten...";
      if (guidanceDesc) guidanceDesc.textContent = "Container verbunden! Wan2GP wird eingerichtet. Beim 1. Mal werden Basisdateien auf dein Volume geladen. Siehe Terminal unten für Live-Logs:";
      if (guidanceAction) {
        guidanceAction.innerHTML = `<button class="guidance-btn" id="btnGuidanceScrollTerm">💻 Terminal öffnen</button>`;
        document.getElementById("btnGuidanceScrollTerm")?.addEventListener("click", () => {
          terminalLog.scrollIntoView({ behavior: "smooth" });
          terminalLog.style.borderColor = "var(--primary)";
          setTimeout(() => terminalLog.style.borderColor = "", 1500);
        });
      }

    } else if (status === "RUNNING") {
      if (step1) {
        step1.className = "step-item done";
        if (step1Status) step1Status.textContent = "Pod gebucht ✓";
      }
      if (line1) line1.className = "step-line done";
      if (step2) {
        step2.className = "step-item done";
        if (step2Status) step2Status.textContent = "Wan2GP bereit ✓";
      }
      if (line2) line2.className = "step-line done";
      if (step3) {
        step3.className = "step-item active";
        if (step3Status) step3Status.textContent = "WebUI online!";
      }
      if (line3) line3.className = "step-line active";
      if (step4) {
        step4.className = "step-item active";
        if (step4Status) step4Status.textContent = "Auto-Download aktiv";
      }

      if (guidanceBanner) guidanceBanner.className = "guidance-banner success";
      if (guidanceIcon) guidanceIcon.textContent = "🎉";
      if (guidanceTitle) guidanceTitle.textContent = "Schritt 3 von 4: Wan2GP WebUI ist online!";
      if (guidanceDesc) guidanceDesc.textContent = "Klicke jetzt auf 'WebUI öffnen' und erstelle dein Video mit MiniMax H3. Wenn du fertig bist: Klicke links auf 'GPU Pausieren', um GPU-Kosten auf 0 € zu stoppen!";
      if (guidanceAction) {
        guidanceAction.innerHTML = `<button class="guidance-btn" id="btnGuidanceOpenWebUI" style="background:var(--success); color:#022c22; font-weight:800;">🌐 Wan2GP WebUI öffnen</button>`;
        document.getElementById("btnGuidanceOpenWebUI")?.addEventListener("click", () => {
          if (activeProxyUrl) window.open(activeProxyUrl, "_blank");
          else alert("Proxy-URL noch nicht bereit, bitte wenige Sekunden warten.");
        });
      }

    } else if (status === "STOPPED") {
      if (step1) {
        step1.className = "step-item active";
        if (step1Status) step1Status.textContent = "Pausiert (0€ GPU)";
      }
      if (step2) {
        step2.className = "step-item done";
        if (step2Status) step2Status.textContent = "Modelle gesichert ✓";
      }
      if (step3) { step3.className = "step-item"; if (step3Status) step3Status.textContent = "Bereit für Start"; }
      if (step4) { step4.className = "step-item"; if (step4Status) step4Status.textContent = "Bereit"; }

      if (guidanceBanner) guidanceBanner.className = "guidance-banner warning";
      if (guidanceIcon) guidanceIcon.textContent = "🟡";
      if (guidanceTitle) guidanceTitle.textContent = "Standby aktiv (GPU aus, 30 GB Modelle erhalten):";
      if (guidanceDesc) guidanceDesc.textContent = "GPU ist aus (0,00 $/h). Deine 30 GB MiniMax H3 Modelle liegen sicher auf der Cloud-Platte (~17¢/Tag). Klicke auf 'Pod Aufwecken', um in 45s weiterzuarbeiten:";
      if (guidanceAction) {
        guidanceAction.innerHTML = `<button class="guidance-btn" id="btnGuidanceResume" style="background:var(--warning); color:#451a03; font-weight:800;">⚡ Pod Aufwecken (Resume)</button>`;
        document.getElementById("btnGuidanceResume")?.addEventListener("click", () => {
          btnStart.click();
        });
      }

    } else { // OFFLINE, TERMINATED
      if (step1) {
        step1.className = "step-item active";
        if (step1Status) step1Status.textContent = "GPU wählen";
      }
      if (step2) { step2.className = "step-item"; if (step2Status) step2Status.textContent = "Noch nicht gestartet"; }
      if (step3) { step3.className = "step-item"; if (step3Status) step3Status.textContent = "Noch nicht gestartet"; }
      if (step4) { step4.className = "step-item"; if (step4Status) step4Status.textContent = "Noch nicht gestartet"; }

      if (guidanceBanner) guidanceBanner.className = "guidance-banner info";
      if (guidanceIcon) guidanceIcon.textContent = "💡";
      if (guidanceTitle) guidanceTitle.textContent = "Schritt 1 von 4: Pod buchen & starten";
      if (guidanceDesc) guidanceDesc.textContent = "Wähle links deine GPU (Tipp: NVIDIA A40 mit 48 GB VRAM für 0,35 $/h ist sofort frei) und klicke auf '🚀 Pod Starten'.";
      if (guidanceAction) {
        guidanceAction.innerHTML = `<button class="guidance-btn" id="btnGuidanceStart">🚀 Pod Starten</button>`;
        document.getElementById("btnGuidanceStart")?.addEventListener("click", () => {
          btnStart.click();
        });
      }
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
        const badge = g.id === "NVIDIA A40" ? " ⭐ (Empfohlen)" : "";
        opt.textContent = `${g.displayName || g.id} (${g.vram} GB)${badge} • Community: ${commPrice}/h | Secure: ${secPrice}/h`;
        gpuSelect.appendChild(opt);
      });

      // Restore selection if exists (preferred default: NVIDIA A40)
      if (Array.from(gpuSelect.options).some(o => o.value === currentVal)) {
        gpuSelect.value = currentVal;
      } else if (Array.from(gpuSelect.options).some(o => o.value === "NVIDIA A40")) {
        gpuSelect.value = "NVIDIA A40";
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

      // Skip rebuilding gallery if user is currently confirming a delete
      if (document.querySelector(".video-delete-btn.confirming")) {
        return;
      }

      galleryGrid.innerHTML = "";
      data.videos.forEach(v => {
        const card = document.createElement("div");
        card.className = "video-card";
        const dateStr = new Date(v.modified * 1000).toLocaleString("de-DE");

        card.innerHTML = `
          <div class="video-thumb">
            <video src="${v.url}#t=0.5" preload="metadata"></video>
            <div class="play-overlay">
              <svg width="18" height="18" fill="#fff" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            </div>
            <button class="video-delete-btn" title="Video löschen">
              &times;
            </button>
          </div>
          <div class="video-info">
            <div class="video-title"></div>
            <div class="video-meta">
              <span>${v.size_mb} MB</span>
              <span>${dateStr}</span>
            </div>
          </div>
        `;

        card.querySelector(".video-title").textContent = v.filename;
        card.querySelector(".video-title").title = v.filename;

        // Click on thumbnail opens video modal (unless delete button clicked)
        card.querySelector(".video-thumb").addEventListener("click", (e) => {
          if (e.target.closest(".video-delete-btn")) return;
          openVideoModal(v.url, v.filename, `${v.size_mb} MB • ${dateStr}`);
        });

        // Delete button with 2-step inline confirmation (no browser popup!)
        const delBtn = card.querySelector(".video-delete-btn");
        let delTimeout = null;

        delBtn.addEventListener("click", async (e) => {
          e.stopPropagation();
          e.preventDefault();

          if (!delBtn.classList.contains("confirming")) {
            // Step 1: Request confirmation
            delBtn.classList.add("confirming");
            delBtn.innerHTML = "<span>Löschen?</span>";
            delBtn.title = "Klicke noch einmal, um endgültig zu löschen";

            delTimeout = setTimeout(() => {
              delBtn.classList.remove("confirming");
              delBtn.innerHTML = "&times;";
              delBtn.title = "Video löschen";
            }, 3500);
            return;
          }

          // Step 2: User clicked second time -> Execute deletion
          clearTimeout(delTimeout);
          delBtn.disabled = true;
          delBtn.innerHTML = '<span class="del-spinner"></span>';

          try {
            const delRes = await fetch("/api/videos/delete", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ filename: v.filename })
            });
            if (delRes.ok) {
              card.style.transition = "all 0.3s cubic-bezier(0.4, 0, 0.2, 1)";
              card.style.opacity = "0";
              card.style.transform = "scale(0.85)";
              setTimeout(() => {
                card.remove();
                if (galleryGrid.querySelectorAll(".video-card").length === 0) {
                  fetchVideos();
                }
              }, 300);
            } else {
              const errData = await delRes.json();
              alert("Fehler beim Löschen: " + (errData.detail || "Unbekannter Fehler"));
              delBtn.disabled = false;
              delBtn.innerHTML = "&times;";
            }
          } catch (delErr) {
            alert("Netzwerkfehler beim Löschen: " + delErr);
            delBtn.disabled = false;
            delBtn.innerHTML = "&times;";
          }
        });

        galleryGrid.appendChild(card);
      });
    } catch (err) {
      console.error("Video gallery fetch error:", err);
    }
  }

  function openVideoModal(url, title, meta) {
    currentModalFilename = title;
    videoModalTitle.textContent = title;
    videoModalMeta.textContent = meta;
    modalVideoPlayer.src = url;
    videoDownloadBtn.href = url;
    videoDownloadBtn.setAttribute("download", title);
    if (modalDeleteBtn) {
      modalDeleteBtn.classList.remove("confirming");
      modalDeleteBtn.innerHTML = "<span>🗑️ Löschen</span>";
      modalDeleteBtn.disabled = false;
    }
    videoModal.classList.add("active");
    modalVideoPlayer.play().catch(() => {});
  }

  function closeVideoModal() {
    videoModal.classList.remove("active");
    modalVideoPlayer.pause();
    modalVideoPlayer.src = "";
    currentModalFilename = "";
  }

  if (modalDeleteBtn) {
    let modalDelTimeout = null;
    modalDeleteBtn.addEventListener("click", async () => {
      if (!currentModalFilename) return;

      if (!modalDeleteBtn.classList.contains("confirming")) {
        modalDeleteBtn.classList.add("confirming");
        modalDeleteBtn.innerHTML = "<span>⚠️ Wirklich löschen?</span>";
        modalDelTimeout = setTimeout(() => {
          modalDeleteBtn.classList.remove("confirming");
          modalDeleteBtn.innerHTML = "<span>🗑️ Löschen</span>";
        }, 3500);
        return;
      }

      clearTimeout(modalDelTimeout);
      modalDeleteBtn.disabled = true;
      modalDeleteBtn.innerHTML = "<span>Lösche...</span>";

      try {
        const res = await fetch("/api/videos/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ filename: currentModalFilename })
        });
        if (res.ok) {
          closeVideoModal();
          fetchVideos();
        } else {
          alert("Fehler beim Löschen des Videos.");
          modalDeleteBtn.disabled = false;
          modalDeleteBtn.innerHTML = "<span>🗑️ Löschen</span>";
        }
      } catch (err) {
        alert("Netzwerkfehler: " + err);
        modalDeleteBtn.disabled = false;
        modalDeleteBtn.innerHTML = "<span>🗑️ Löschen</span>";
      }
    });
  }

  closeVideoModalBtn.addEventListener("click", closeVideoModal);
  videoModal.addEventListener("click", (e) => {
    if (e.target === videoModal) closeVideoModal();
  });

  // Cost & Status Info Modal (Pause vs. Stop vs. Löschen)
  function openCostInfoModal() {
    if (costInfoModal) costInfoModal.classList.add("active");
  }
  function closeCostInfoModal() {
    if (costInfoModal) costInfoModal.classList.remove("active");
  }

  if (btnHeaderCostInfo) btnHeaderCostInfo.addEventListener("click", openCostInfoModal);
  if (btnCockpitCostInfo) btnCockpitCostInfo.addEventListener("click", openCostInfoModal);
  if (closeCostInfoModalBtn) closeCostInfoModalBtn.addEventListener("click", closeCostInfoModal);
  if (closeCostInfoModalBtn2) closeCostInfoModalBtn2.addEventListener("click", closeCostInfoModal);
  if (costInfoModal) {
    costInfoModal.addEventListener("click", (e) => {
      if (e.target === costInfoModal) closeCostInfoModal();
    });
  }

  // Close modals on Escape key
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeCostInfoModal();
      closeVideoModal();
      closeSettings();
    }
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
    const msg = "GPU pausieren (Stop Pod)?\n\n" +
                "✓ Die GPU-Stundenkosten stoppen sofort (0,00 $/h).\n" +
                "✓ Deine 30 GB MiniMax H3 Modellgewichte bleiben auf der Cloud-Platte erhalten (~17¢/Tag).\n" +
                "✓ Beim nächsten Start bist du in 45 Sekunden startklar!";
    if (!confirm(msg)) return;
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
    const msg = "⚠️ ACHTUNG: POD WIRKLICH UNWIDERRUFLICH LÖSCHEN?\n\n" +
                "• Die Standby-Kosten sinken auf exakt 0,00 €.\n" +
                "• ABER: Das gesamte Cloud-Volume und die 30 GB heruntergeladenen MiniMax H3 Modellgewichte werden GELÖSCHT!\n" +
                "• Beim nächsten Start müssen alle 30 GB Modelle neu heruntergeladen werden (~6 Minuten Bootzeit).\n\n" +
                "TIPP: Normalerweise reicht 'GPU Pausieren' völlig aus, um die GPU-Kosten zu stoppen und die Modelle zu behalten.\n\n" +
                "Möchtest du den Pod trotzdem endgültig löschen?";
    if (!confirm(msg)) return;
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

  // Cockpit Action Listeners (delegated to primary buttons)
  if (btnCockpitPause) {
    btnCockpitPause.addEventListener("click", () => btnPause.click());
  }
  if (btnCockpitResume) {
    btnCockpitResume.addEventListener("click", () => btnStart.click());
  }
  if (btnCockpitTerminate) {
    btnCockpitTerminate.addEventListener("click", () => btnTerminate.click());
  }

  if (btnRefreshBalance) {
    btnRefreshBalance.addEventListener("click", async () => {
      btnRefreshBalance.classList.add("spinning");
      try {
        const res = await fetch("/api/account/refresh");
        if (res.ok) {
          const data = await res.json();
          if (data.account && data.account.balance !== undefined) {
            currentBalance = parseFloat(data.account.balance);
            displayBalance.textContent = currentBalance.toFixed(2);
            if (cfgBalance) cfgBalance.value = currentBalance.toFixed(2);
            if (currentHourlyRate > 0) {
              const hours = (currentBalance / currentHourlyRate).toFixed(0);
              cockpitRunTimeRemaining.textContent = `~${hours}h GPU möglich`;
            }
          }
        }
      } catch (err) {
        console.error("Fehler beim Aktualisieren des Guthabens:", err);
      } finally {
        setTimeout(() => {
          btnRefreshBalance.classList.remove("spinning");
        }, 600);
      }
    });
  }

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
        cfgApiKey.value = cfg.runpod_api_key_masked || "";
        cfgApiKey.placeholder = "Neuen Key nur eingeben, wenn er geändert werden soll";
        cfgEmail.value = cfg.account_email || "";
        cfgBalance.value = (cfg.account_balance !== undefined ? cfg.account_balance : currentBalance).toFixed(2);
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
    const bal = parseFloat(cfgBalance.value);
    const body = { account_balance: isNaN(bal) ? currentBalance : bal };
    if (key && !key.includes("...")) {
      body.runpod_api_key = key;
    }
    await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
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
