// RunPod VideoGen Studio Frontend Controller

// Only a price returned for the current pod may drive the cost projection.
// The GPU catalogue/next-pod selection must never substitute a billed pod rate.
window.PodCostDisplay = Object.freeze({
  project(data, balance) {
    const value = data.hourly_rate;
    const hourlyRate = data.pod_id && data.hourly_rate_source === "runpod"
      && typeof value === "number" && Number.isFinite(value) && value >= 0
      ? value : null;
    return {
      hourlyRate,
      dailyRate: hourlyRate === null ? null : hourlyRate * 24,
      weeklyRate: hourlyRate === null ? null : hourlyRate * 168,
      hoursRemaining: hourlyRate > 0 && Number.isFinite(balance) && balance >= 0
        ? balance / hourlyRate : null,
      sessionCost: data.status !== "ERROR" && hourlyRate !== null
        && typeof data.accumulated_cost === "number" && Number.isFinite(data.accumulated_cost) && data.accumulated_cost >= 0
        ? data.accumulated_cost : null,
    };
  },
});

window.PodActionsDisplay = Object.freeze({
  canPause(data) {
    return Boolean(data.pod_id) && (data.status === "RUNNING" || data.status === "ERROR");
  },
});

window.StartupProgressDisplay = Object.freeze({
  project(data, now = Date.now() / 1000) {
    const progress = data.startup_progress || {};
    const starting = data.status === "STARTING" || data.status === "BOOTSTRAPPING";
    const error = data.status === "ERROR";
    const cancelled = progress.phase === "cancelled";
    const active = starting && progress.active === true && !cancelled && progress.phase !== "error";
    const timestamp = value => typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
    const startedAt = timestamp(progress.started_at);
    const updatedAt = timestamp(progress.updated_at);
    const end = active ? now : updatedAt;
    return {
      visible: starting || error,
      active,
      error: error || progress.phase === "error",
      label: typeof progress.label === "string" && progress.label.trim()
        ? progress.label : (error ? "Start fehlgeschlagen" : "Start wird vorbereitet"),
      detail: typeof progress.detail === "string" && progress.detail.trim()
        ? progress.detail : (data.status_detail || "Warte auf die nächste Rückmeldung …"),
      state: error || progress.phase === "error" ? "Fehler" : (cancelled ? "Abgebrochen" : (active ? "Läuft" : "Wartet")),
      elapsedSeconds: startedAt !== null && end !== null ? Math.max(0, Math.floor(end - startedAt)) : null,
      feedbackAgeSeconds: updatedAt === null ? null : Math.max(0, Math.floor(now - updatedAt)),
      updatedAt,
    };
  },
});

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
  const cockpitRateSource = document.getElementById("cockpitRateSource");
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
  const startupProgressPanel = document.getElementById("startupProgressPanel");
  const startupPhaseLabel = document.getElementById("startupPhaseLabel");
  const startupStateBadge = document.getElementById("startupStateBadge");
  const startupProgressBar = document.getElementById("startupProgressBar");
  const startupProgressDetail = document.getElementById("startupProgressDetail");
  const startupElapsed = document.getElementById("startupElapsed");
  const startupLastFeedback = document.getElementById("startupLastFeedback");
  let startupStatusData = null;

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

  // GPU-Verfügbarkeit: Hinweiszeile, Aktualisieren-Button und "ausgebucht"-Modal
  const gpuAvailabilityHint = document.getElementById("gpuAvailabilityHint");
  const refreshGpuBtn = document.getElementById("refreshGpuBtn");
  const gpuStockModal = document.getElementById("gpuStockModal");
  const gpuStockFailedGpu = document.getElementById("gpuStockFailedGpu");
  const gpuStockDetail = document.getElementById("gpuStockDetail");
  const gpuStockAlternatives = document.getElementById("gpuStockAlternatives");
  const retryGpuStockBtn = document.getElementById("retryGpuStockBtn");
  const laterGpuStockBtn = document.getElementById("laterGpuStockBtn");
  const closeGpuStockBtn = document.getElementById("closeGpuStockBtn");
  let gpusCache = [];
  let gpuFetchSequence = 0;
  let gpuStockModalShown = false;
  let gpuMarksRefreshedForError = false;

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
  let currentHourlyRate = null;
  let timerInterval = null;
  let activeProxyUrl = null;
  let currentBalance = 0;

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
      const cost = currentHourlyRate === null ? null : (currentUptime / 3600.0) * currentHourlyRate;
      sessionCostDisplay.textContent = cost === null ? "—" : `~$${cost.toFixed(4)}`;
    }
  }

  function renderStartupProgress() {
    if (!startupStatusData || !startupProgressPanel) return;
    const view = window.StartupProgressDisplay.project(startupStatusData);
    startupProgressPanel.hidden = !view.visible;
    if (!view.visible) return;
    startupProgressPanel.classList.toggle("is-active", view.active);
    startupProgressPanel.classList.toggle("is-error", view.error);
    if (startupPhaseLabel.textContent !== view.label) startupPhaseLabel.textContent = view.label;
    if (startupProgressDetail.textContent !== view.detail) startupProgressDetail.textContent = view.detail;
    startupStateBadge.textContent = view.state;
    startupProgressBar.setAttribute("aria-label", `${view.label}: ${view.state}`);
    // No aria-valuenow: the installation has no measurable overall percentage.
    startupElapsed.textContent = view.elapsedSeconds === null ? "—" : formatTime(view.elapsedSeconds);
    startupLastFeedback.textContent = view.feedbackAgeSeconds === null
      ? "Noch keine" : (view.feedbackAgeSeconds < 2 ? "Gerade eben" : `vor ${formatTime(view.feedbackAgeSeconds)}`);
    startupLastFeedback.title = view.updatedAt === null ? "" : new Date(view.updatedAt * 1000).toLocaleTimeString("de-DE");
  }

  // Fetch Status & Update Cockpit
  async function fetchStatus() {
    try {
      const res = await fetch("/api/status");
      if (!res.ok) return;
      const data = await res.json();
      startupStatusData = data;
      renderStartupProgress();

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

      // The reported GPU rate belongs to this pod, not the next-pod selection.
      // Storage is separate; do not add an unrelated network-volume tariff.
      const costs = window.PodCostDisplay.project(data, currentBalance);
      currentHourlyRate = costs.hourlyRate;
      hourlyRateDisplay.textContent = currentHourlyRate === null ? "— /h" : `$${currentHourlyRate.toFixed(3)} /h`;

      const volumeGb = data.config && data.config.volume_disk_gb ? data.config.volume_disk_gb : 75;
      const status = data.status;
      const hasPod = Boolean(data.pod_id);
      const priceKnown = currentHourlyRate !== null;
      cockpitHourlyRate.innerHTML = `${priceKnown ? `$${currentHourlyRate.toFixed(3)}` : "—"} <span class="rate-unit">/ Std.</span>`;
      cockpitDailyRate.textContent = costs.dailyRate === null ? "—" : `$${costs.dailyRate.toFixed(2)}`;
      cockpitWeeklyRate.textContent = costs.weeklyRate === null ? "—" : `$${costs.weeklyRate.toFixed(2)}`;
      cockpitRunTimeRemaining.textContent = costs.hoursRemaining === null ? "—" : `~${costs.hoursRemaining.toFixed(1)}h GPU`;
      cockpitDiskMonthly.textContent = hasPod ? "Separat" : "—";
      cockpitDiskMonthly.title = hasPod ? "Zusätzliche Speicherkosten werden separat von RunPod berechnet." : "";
      cockpitRateSource.textContent = priceKnown
        ? `RunPod${data.pod_gpu_name ? ` · ${data.pod_gpu_name}` : ""} · GPU, zzgl. Speicher`
        : "Pod-Preis wird abgefragt …";

      // ==========================================
      // AMPELSYSTEM & DICKE STATUSANZEIGE
      // ==========================================
      if (status === "RUNNING" || status === "STARTING" || status === "BOOTSTRAPPING") {
        // A booked pod may already incur costs while it is provisioning.
        ampelRed.classList.toggle("active", hasPod);
        ampelYellow.classList.toggle("active", !hasPod);
        ampelGreen.classList.remove("active");
        if (costCockpitCard) costCockpitCard.className = `card cost-cockpit-card ${hasPod ? "state-red" : "state-yellow"}`;

        ampelStatusTag.className = `ampel-status-tag ${hasPod ? "red" : "yellow"}`;
        ampelStatusTag.textContent = hasPod ? "🔴 POD GEBUCHT – GPU-KOSTEN" : "🟡 POD WIRD ANGEFRAGT";
        ampelHeadline.textContent = status === "RUNNING" ? "GPU ist eingeschaltet" : "Pod wird eingerichtet";
        ampelDesc.textContent = status === "RUNNING"
          ? "Die GPU ist eingeschaltet und wird auch im Leerlauf berechnet. Pausiere den Pod mit 'GPU Pausieren', sobald deine Videos generiert sind."
          : "RunPod stellt den Pod bereit. Ein gebuchter Pod kann bereits während der Einrichtung Kosten verursachen. Der Preis wird direkt beim aktuellen Pod abgefragt.";

      } else if (status === "STOPPED") {
        // 🟡 GELB: Standby (GPU aus, Modelle erhalten)
        ampelRed.classList.remove("active");
        ampelYellow.classList.add("active");
        ampelGreen.classList.remove("active");
        if (costCockpitCard) costCockpitCard.className = "card cost-cockpit-card state-yellow";

        ampelStatusTag.className = "ampel-status-tag yellow";
        ampelStatusTag.textContent = "🟡 STANDBY (GPU AUS, SPEICHER ERHALTEN)";
        ampelHeadline.textContent = "GPU pausiert";
        ampelDesc.textContent = `Die GPU ist abgeschaltet. Die ${volumeGb} GB Pod-Platte und gespeicherte Modelle bleiben erhalten. Für den Speicher fallen weiterhin Kosten bei RunPod an.`;

        cockpitHourlyRate.innerHTML = '$0.000 <span class="rate-unit">/ Std. GPU</span>';
        cockpitDailyRate.textContent = "$0.00";
        cockpitWeeklyRate.textContent = "$0.00";
        cockpitRunTimeRemaining.textContent = "—";
        cockpitRateSource.textContent = "GPU pausiert · Speicherkosten separat";

      } else if (status === "ERROR") {
        // ⚠️ ERROR Zustand
        ampelRed.classList.toggle("active", hasPod);
        ampelYellow.classList.remove("active");
        ampelGreen.classList.toggle("active", !hasPod);
        if (costCockpitCard) costCockpitCard.className = `card cost-cockpit-card ${hasPod ? "state-red" : "state-green"}`;

        ampelStatusTag.className = "ampel-status-tag red";
        ampelStatusTag.textContent = "⚠️ FEHLER IM POD-SETUP";
        ampelHeadline.textContent = "Status prüfen & intervenieren";
        ampelDesc.textContent = data.status_detail || "Beim Starten oder Ausführen des Pods ist ein Fehler aufgetreten. Überprüfe die Terminal-Logs unten oder lösche den Pod.";

        cockpitHourlyRate.innerHTML = hasPod
          ? `${priceKnown ? `$${currentHourlyRate.toFixed(3)}` : "—"} <span class="rate-unit">/ Std. GPU bei Betrieb</span>`
          : '$0.000 <span class="rate-unit">/ Std.</span>';
        cockpitDailyRate.textContent = "-";
        cockpitWeeklyRate.textContent = "-";
        cockpitDiskMonthly.textContent = hasPod ? "Separat" : "$0.00";
        cockpitRunTimeRemaining.textContent = hasPod ? "Status prüfen" : "Kein Pod";
        if (!hasPod) cockpitRateSource.textContent = "Kein Pod gebucht · keine Kosten für diesen Pod";

      } else {
        // 🟢 GRÜN: Null-Kosten (Aus & Gelöscht)
        ampelRed.classList.remove("active");
        ampelYellow.classList.remove("active");
        ampelGreen.classList.add("active");
        if (costCockpitCard) costCockpitCard.className = "card cost-cockpit-card state-green";

        ampelStatusTag.className = "ampel-status-tag green";
        ampelStatusTag.textContent = "🟢 0,- € NULL-KOSTEN MODUS";
        ampelHeadline.textContent = "Keine laufenden Kosten";
        ampelDesc.textContent = "Für diesen Pod ist keine GPU und kein Pod-Speicher reserviert.";

        cockpitHourlyRate.innerHTML = `$0.000 <span class="rate-unit">/ Std.</span>`;
        cockpitDailyRate.textContent = "$0.00";
        cockpitWeeklyRate.textContent = "$0.00";
        cockpitDiskMonthly.textContent = "$0.00";

        cockpitRunTimeRemaining.textContent = "Kein Pod";
        cockpitRateSource.textContent = "Kein Pod gebucht";
      }

      // Live Session Timer
      if (data.status === "RUNNING") {
        const serverUptime = data.uptime_seconds || 0;
        // Nur bei größerer Abweichung vom lokalen Takt übernehmen (verhindert 3s-Jitter)
        if (currentUptime === 0 || Math.abs(serverUptime - currentUptime) > 5) {
          currentUptime = serverUptime;
          sessionTimer.textContent = formatTime(currentUptime);
        }
        sessionCostDisplay.textContent = costs.sessionCost === null ? "—" : `~$${costs.sessionCost.toFixed(4)}`;
        if (!timerInterval) {
          timerInterval = setInterval(tickTimer, 1000);
        }
      } else {
        if (timerInterval) {
          clearInterval(timerInterval);
          timerInterval = null;
        }
        currentUptime = typeof data.uptime_seconds === "number" && Number.isFinite(data.uptime_seconds)
          ? Math.max(0, data.uptime_seconds) : 0;
        sessionTimer.textContent = data.status === "ERROR" ? "Status unklar" : formatTime(currentUptime);
        sessionCostDisplay.textContent = costs.sessionCost === null ? "—" : `~$${costs.sessionCost.toFixed(4)}`;
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
        if (data.config.gpu_type_id) setSelectedGpu(data.config.gpu_type_id);
        if (data.config.cloud_type && cloudTypeSelect && !cloudTypeSelect.disabled && document.activeElement !== cloudTypeSelect) {
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
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = false;
        if (btnCockpitResume) btnCockpitResume.style.display = "none";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = "inline-flex";
      } else if (data.status === "STARTING" || data.status === "BOOTSTRAPPING") {
        btnStart.disabled = true;
        btnStart.innerHTML = "<span>⏳</span> Startet...";
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = true;
        if (btnCockpitResume) btnCockpitResume.style.display = "none";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = "inline-flex";
      } else if (data.status === "STOPPED") {
        btnStart.disabled = false;
        btnStart.innerHTML = "<span>⚡</span> Pod Aufwecken (Resume)";
        btnTerminate.disabled = false;
        btnOpenWebUI.disabled = true;
        if (btnCockpitResume) btnCockpitResume.style.display = "inline-flex";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = "inline-flex";
      } else { // OFFLINE, TERMINATED, ERROR
        btnStart.disabled = false;
        btnStart.innerHTML = "<span>🚀</span> Pod Starten";
        btnTerminate.disabled = !data.pod_id;
        btnOpenWebUI.disabled = true;
        if (btnCockpitResume) btnCockpitResume.style.display = "none";
        if (btnCockpitTerminate) btnCockpitTerminate.style.display = data.pod_id ? "inline-flex" : "none";
      }
      // A setup error does not stop RunPod billing; keep Stop available for that pod.
      const canPause = window.PodActionsDisplay.canPause(data);
      btnPause.disabled = !canPause;
      if (btnCockpitPause) btnCockpitPause.style.display = canPause ? "inline-flex" : "none";

      // GPU ausgebucht: Dropdown-Markierungen auffrischen und Alternativ-Modal zeigen
      const outOfStockNow = data.status === "ERROR" && (
        isOutOfStockText(data.status_detail || "") ||
        (data.recent_logs || []).some(l => isOutOfStockText(l))
      );
      if (data.status !== "ERROR") {
        gpuStockModalShown = false;
        gpuMarksRefreshedForError = false;
      } else {
        if (outOfStockNow && !gpuMarksRefreshedForError) {
          gpuMarksRefreshedForError = true;
          fetchGpus();
        }
        if (outOfStockNow && !gpuStockModalShown && gpusCache.length) {
          gpuStockModalShown = true;
          showGpuStockModal(data.config && data.config.gpu_type_id, data.status_detail || "");
        }
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
        if (guidanceTitle) guidanceTitle.textContent = "Start abgebrochen: Gewählte GPU aktuell ausgebucht";
        if (guidanceDesc) guidanceDesc.textContent = "RunPod konnte keine passende Instanz bereitstellen. Vergleiche die aktuelle Verfügbarkeit und Preise anderer Grafikkarten oder Clouds.";
        if (guidanceAction) {
          guidanceAction.innerHTML = `
            <button class="guidance-btn" id="btnGuidanceCompare">Grafikkarten vergleichen</button>
            <button class="guidance-btn btn-secondary" id="btnGuidanceRetry">🔄 Nochmal versuchen</button>
          `;

          document.getElementById("btnGuidanceCompare")?.addEventListener("click", () => window.GpuPicker.open());

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
      if (guidanceDesc) guidanceDesc.textContent = "RunPod weist die GPU zu und startet den Container. Den aktuellen Schritt und die letzte Rückmeldung siehst du im Startfortschritt.";
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
      if (guidanceTitle) guidanceTitle.textContent = "Schritt 2 von 4: Wan2GP einrichten und starten …";
      if (guidanceDesc) guidanceDesc.textContent = "Die Umgebung wird geprüft, fehlende Komponenten werden installiert und die WebUI gestartet. Einzelheiten stehen im Startfortschritt und Live-Protokoll.";
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
      if (guidanceTitle) guidanceTitle.textContent = "Standby aktiv (GPU aus, gespeicherte Modelle erhalten):";
      if (guidanceDesc) guidanceDesc.textContent = "Die GPU ist aus. Die Pod-Platte bleibt erhalten und wird weiter berechnet. Klicke auf 'Pod Aufwecken', um die Umgebung erneut zu starten.";
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
      if (guidanceDesc) guidanceDesc.textContent = "Vergleiche über die GPU-Auswahl Preise und Verfügbarkeit. Übernimm eine Karte und klicke anschließend auf '🚀 Pod Starten'.";
      if (guidanceAction) {
        guidanceAction.innerHTML = `<button class="guidance-btn" id="btnGuidanceStart">🚀 Pod Starten</button>`;
        document.getElementById("btnGuidanceStart")?.addEventListener("click", () => {
          btnStart.click();
        });
      }
    }
  }

  function setSelectedGpu(id) {
    if (!Array.from(gpuSelect.options).some(option => option.value === id)) {
      gpuSelect.appendChild(new Option(id, id));
    }
    gpuSelect.value = id;
    document.getElementById("selectedGpuName").textContent = gpusCache.find(gpu => gpu.id === id)?.displayName || id;
  }

  window.GpuPicker.init({
    getSelected: () => gpuSelect.value,
    getCloud: () => cloudTypeSelect.value,
    onSaved: (id, cloud) => {
      setSelectedGpu(id);
      cloudTypeSelect.value = cloud;
      fetchGpus();
      fetchStatus();
    }
  });

  // Read current prices/stock without discarding failed booking observations.
  async function fetchGpus() {
    const sequence = ++gpuFetchSequence;
    const cloud = cloudTypeSelect.value;
    try {
      const res = await fetch(`/api/gpus?cloud_type=${encodeURIComponent(cloud)}`);
      if (!res.ok) throw new Error("GPU-Abfrage fehlgeschlagen");
      const data = await res.json();
      if (sequence !== gpuFetchSequence || cloud !== cloudTypeSelect.value) return;
      if (!Array.isArray(data.gpus)) throw new Error("GPU-Liste fehlt");

      gpusCache = data.gpus;
      setSelectedGpu(gpuSelect.value);
      const availCount = data.gpus.filter(g => ["available", "limited"].includes(g.availability)).length;
      const unknownCount = data.gpus.filter(g => g.availability === "unknown").length;
      if (gpuAvailabilityHint) {
        gpuAvailabilityHint.textContent = `${availCount} verfügbar · ${unknownCount} mit unbekanntem Bestand · Details im Vergleich`;
      }
    } catch (err) {
      if (sequence !== gpuFetchSequence) return;
      if (gpuAvailabilityHint) gpuAvailabilityHint.textContent = "Preise und Verfügbarkeit konnten nicht aktualisiert werden.";
      console.error("GPU fetch error:", err);
    }
  }

  // Booking failures can still occur after a positive stock snapshot.
  function isOutOfStockText(text) {
    const t = (text || "").toLowerCase();
    return t.includes("instances available") || t.includes("sold out") || t.includes("ausgebucht");
  }

  function closeGpuStockModal() {
    gpuStockModal.classList.remove("active");
  }

  function showGpuStockModal(failedGpuId, detail) {
    gpuStockFailedGpu.textContent = `⛔ ${failedGpuId || "Gewählte GPU"} ist gerade ausgebucht`;
    gpuStockDetail.textContent = detail || "RunPod hat keine freie Instanz gefunden. Wähle eine Alternative oder versuche es später erneut.";

    gpuStockAlternatives.replaceChildren();
    const compare = document.createElement("button");
    compare.className = "guidance-btn";
    compare.textContent = "Grafikkarten, Preise und Verfügbarkeit vergleichen";
    compare.addEventListener("click", () => {
      closeGpuStockModal();
      window.GpuPicker.open();
    });
    gpuStockAlternatives.appendChild(compare);

    gpuStockModal.classList.add("active");
  }

  if (refreshGpuBtn) {
    refreshGpuBtn.addEventListener("click", async () => {
      refreshGpuBtn.disabled = true;
      await fetchGpus();
      refreshGpuBtn.disabled = false;
    });
  }

  closeGpuStockBtn?.addEventListener("click", closeGpuStockModal);
  laterGpuStockBtn?.addEventListener("click", closeGpuStockModal);
  retryGpuStockBtn?.addEventListener("click", () => {
    closeGpuStockModal();
    // Explicit retry requested by the user.
    btnStart.click();
  });

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
                "✓ Gespeicherte Modelle bleiben auf der Pod-Platte erhalten.\n" +
                "✓ Die Speicherkosten laufen weiter. Die Wiederanlaufzeit hängt vom Pod und seiner Umgebung ab.";
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
                "• Das Pod-Volume und die dort heruntergeladenen Modelle werden GELÖSCHT!\n" +
                "• Beim nächsten Start muss die Umgebung neu eingerichtet werden; fehlende Modelle werden bei Bedarf erneut heruntergeladen.\n\n" +
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

  cloudTypeSelect.addEventListener("change", async () => {
    cloudTypeSelect.disabled = true;
    try {
      const response = await fetch("/api/config", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cloud_type: cloudTypeSelect.value })
      });
      if (!response.ok) throw new Error("Cloud-Auswahl konnte nicht gespeichert werden.");
      await fetchGpus();
    } catch (error) {
      gpuAvailabilityHint.textContent = error.message;
      await fetchStatus();
    } finally {
      cloudTypeSelect.disabled = false;
    }
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
      // Terminal leeren, damit der Log-Replay nach einem Reconnect keine Duplikate erzeugt
      terminalLog.textContent = "[SYSTEM] Verbunden mit Live-Log Stream.\n";
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
  fetchStatus().then(fetchGpus);
  fetchVideos();
  connectLogsWebSocket();

  setInterval(fetchStatus, 3000);
  setInterval(renderStartupProgress, 1000);
  setInterval(fetchVideos, 10000);
});
