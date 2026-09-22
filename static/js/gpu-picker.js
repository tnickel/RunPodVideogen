/* GPU comparison uses published image timings, never extrapolated video timings. */
(() => {
  const stockOrder = { available: 0, limited: 1, unknown: 2, unavailable: 3 };
  const positive = value => typeof value === "number" && Number.isFinite(value) && value > 0;
  const format = (value, digits = 2) => value.toLocaleString("de-DE", {
    minimumFractionDigits: digits, maximumFractionDigits: digits
  });
  const cloudName = value => ({ ALL: "Alle Clouds", SECURE: "Secure", COMMUNITY: "Community" }[value] || "Cloud unbekannt");

  function metrics(gpu, reference, seconds) {
    const duration = seconds[gpu.id];
    const speed = positive(duration) && positive(seconds[reference]) ? seconds[reference] / duration : null;
    const price = positive(gpu.hourly_price) ? gpu.hourly_price : null;
    return { ...gpu, seconds: positive(duration) ? duration : null, speed, price,
      efficiency: speed !== null && price !== null ? speed / price : null };
  }

  function compareRows(gpus, options, seconds) {
    const query = options.search.trim().toLocaleLowerCase("de");
    const rows = gpus.map(gpu => metrics(gpu, options.reference, seconds)).filter(gpu =>
      `${gpu.id} ${gpu.displayName || ""}`.toLocaleLowerCase("de").includes(query) &&
      gpu.vram >= options.minVram &&
      (!options.minSpeed || (gpu.speed !== null && gpu.speed >= options.minSpeed)) &&
      (!options.availableOnly || ["available", "limited"].includes(gpu.availability))
    );
    const descending = (a, b, field) => (b[field] ?? -Infinity) - (a[field] ?? -Infinity);
    return rows.sort((a, b) => {
      let order = 0;
      if (options.sort === "price") order = (a.price ?? Infinity) - (b.price ?? Infinity);
      else if (["speed", "efficiency", "vram"].includes(options.sort)) order = descending(a, b, options.sort);
      else order = (stockOrder[a.availability] ?? 2) - (stockOrder[b.availability] ?? 2) || descending(a, b, "efficiency");
      return order || a.id.localeCompare(b.id);
    });
  }
  // Small, side-effect-free core also used by the comparison regression checks.
  window.GpuComparison = Object.freeze({ metrics, compareRows });

  window.GpuPicker = {
    init({ getSelected, getCloud, onSaved }) {
      const el = id => document.getElementById(id);
      const dialog = el("gpuPickerDialog");
      const body = el("gpuTableBody");
      const message = el("gpuPickerMessage");
      const apply = el("applyGpuPickerBtn");
      const cloud = el("gpuPickerCloud");
      const reference = el("gpuReferenceSelect");
      const benchmark = window.GpuBenchmarkData;
      let data = null, pending = "", loading = false, saving = false, request = null;
      const node = (tag, className, text) => {
        const result = document.createElement(tag);
        if (className) result.className = className;
        if (text !== undefined) result.textContent = text;
        return result;
      };
      Object.keys(benchmark.seconds).forEach(id => {
        if (id === "NVIDIA A40") return;
        reference.appendChild(new Option(id.replace(/^NVIDIA /, ""), id));
      });
      el("gpuBenchmarkDescription").textContent = `${benchmark.description} 2× hier bedeutet nicht automatisch 2× schnelleres MiniMax-Video.`;
      el("gpuBenchmarkSource").href = benchmark.source_url;
      el("gpuBenchmarkSource").title = `${benchmark.note} Quelle abgerufen: ${benchmark.retrieved_at}.`;

      function announce(text, error = false) {
        message.textContent = text;
        message.classList.toggle("is-error", error);
      }

      function updatePending() {
        const selected = data?.gpus.find(gpu => gpu.id === pending);
        const label = selected?.displayName || pending || "Keine Karte gewählt";
        el("gpuPendingName").textContent = label;
        let detail = "Für die nächste Pod-Erstellung. Ein vorhandener Pod behält seine GPU.";
        if (selected) {
          const price = positive(selected.hourly_price) ? `${format(selected.hourly_price, 3)} $/h` : "Preis unbekannt";
          detail = `${cloudName(cloud.value)} · ${price} · ${detail}`;
          if (selected.availability === "unknown") detail += " Verfügbarkeit unbestätigt.";
          if (selected.availability === "unavailable") detail = "In dieser Cloud aktuell nicht verfügbar. Bitte eine andere Karte oder Cloud wählen.";
        }
        el("gpuPendingDetail").textContent = detail;
        apply.disabled = loading || saving || !selected || selected.availability === "unavailable";
        apply.textContent = saving ? "Wird gespeichert …" : "Auswahl übernehmen";
      }

      function render() {
        body.replaceChildren();
        const rows = compareRows(data?.gpus || [], {
          search: el("gpuSearchInput").value, reference: reference.value,
          minVram: Number(el("gpuMinVram").value), minSpeed: Number(el("gpuMinSpeed").value),
          availableOnly: el("gpuAvailableOnly").checked, sort: el("gpuSort").value
        }, benchmark.seconds);
        el("gpuResultCount").textContent = `${rows.length} von ${data?.gpus.length || 0} Karten`;
        if (!rows.length) {
          const cell = node("td", "gpu-empty", loading ? "Preise und Verfügbarkeit werden abgefragt …" :
            data ? "Keine Karte passt zu diesen Filtern. VRAM, Tempo oder Suche anpassen." : "Keine aktuellen Daten. Bitte erneut aktualisieren.");
          cell.colSpan = 7;
          const row = node("tr"); row.appendChild(cell); body.appendChild(row);
        }
        rows.forEach(gpu => {
          const stock = Object.hasOwn(stockOrder, gpu.availability) ? gpu.availability : "unknown";
          const row = node("tr", `gpu-row stock-${stock}${pending === gpu.id ? " is-selected" : ""}`);
          const name = node("td", "gpu-name-cell");
          name.appendChild(node("strong", "", gpu.displayName || gpu.id.replace(/^NVIDIA /, "")));
          if (gpu.id === getSelected()) name.appendChild(node("small", "gpu-saved-tag", "Gespeicherte Auswahl"));
          if (gpu.vram < 48) name.appendChild(node("small", "gpu-vram-warning", "Weniger VRAM als A40 · ggf. mehr Auslagern"));
          row.appendChild(name);
          row.appendChild(node("td", "gpu-number", `${gpu.vram} GB`));
          const stockCell = node("td");
          const stockBadge = node("span", `gpu-stock-badge ${stock}`, gpu.availability_label || "Unbekannt");
          const sources = { runpod_live: "Aktuelle RunPod-Bestandsabfrage", runpod_cloud_catalog: "Laut RunPod in dieser Cloud nicht angeboten", recent_booking_failure: "Letzter Buchungsversuch, keine Live-Bestandsauskunft", unknown: "RunPod liefert keinen Bestand für diese Cloud" };
          stockBadge.title = sources[gpu.availability_source] || sources.unknown;
          stockCell.appendChild(stockBadge); row.appendChild(stockCell);
          const priceCell = node("td", "gpu-number");
          priceCell.appendChild(node("strong", "", gpu.price === null ? "—" : `${format(gpu.price, 3)} $`));
          priceCell.appendChild(node("small", "", gpu.price_source === "runpod_catalog" ? "Katalogpreis" : gpu.price_source === "runpod_live" ? "Live-Angebot" : "Preis fehlt"));
          if (cloud.value === "ALL" && gpu.price !== null) priceCell.appendChild(node("small", "", cloudName(gpu.price_cloud_type)));
          row.appendChild(priceCell);
          const speedCell = node("td", "gpu-number");
          speedCell.appendChild(node("strong", gpu.speed !== null && gpu.speed >= 2 ? "gpu-fast" : "", gpu.speed === null ? "—" : `${format(gpu.speed)}×`));
          speedCell.appendChild(node("small", "", gpu.seconds === null ? "Kein Messwert" : `${format(gpu.seconds)} s / Bild`));
          row.appendChild(speedCell);
          const efficiencyCell = node("td", "gpu-number");
          efficiencyCell.appendChild(node("strong", "", gpu.efficiency === null ? "—" : format(gpu.efficiency)));
          efficiencyCell.appendChild(node("small", "", gpu.efficiency === null ? "Daten fehlen" : "höher ist besser"));
          row.appendChild(efficiencyCell);
          const choiceCell = node("td", "gpu-choice-cell");
          const choice = node("input", "gpu-choice");
          choice.type = "radio"; choice.name = "gpuChoice"; choice.value = gpu.id;
          choice.setAttribute("aria-label", `${gpu.displayName || gpu.id} auswählen`);
          choice.checked = pending === gpu.id;
          choice.disabled = loading || saving || stock === "unavailable";
          choice.addEventListener("change", () => {
            pending = gpu.id;
            body.querySelectorAll("tr").forEach(item => item.classList.toggle("is-selected", item.querySelector("input")?.checked === true));
            updatePending();
          });
          choiceCell.appendChild(choice); row.appendChild(choiceCell); body.appendChild(row);
        });
        updatePending();
      }

      async function refresh() {
        request?.abort();
        const controller = new AbortController(); request = controller;
        loading = true; data = null;
        el("gpuPickerRefresh").disabled = true;
        el("gpuCheckedAt").textContent = "";
        announce("RunPod wird abgefragt …"); render();
        try {
          const response = await fetch(`/api/gpus?cloud_type=${encodeURIComponent(cloud.value)}`, { signal: controller.signal });
          if (!response.ok) throw new Error("Preise und Bestand konnten nicht geladen werden. API-Key und Verbindung prüfen, dann erneut aktualisieren.");
          const result = await response.json();
          if (request !== controller || controller.signal.aborted) return;
          if (!Array.isArray(result.gpus)) throw new Error("RunPod lieferte keine gültige GPU-Liste.");
          data = result;
          announce(result.availability_notice || "Bestand und Preise können sich bis zur Buchung ändern.");
          const checked = new Date(result.checked_at);
          el("gpuCheckedAt").textContent = Number.isNaN(checked.getTime()) ? "Zeitpunkt unbekannt" :
            `Abgefragt: ${checked.toLocaleTimeString("de-DE")} · 1 GPU`;
        } catch (error) {
          if (controller.signal.aborted || request !== controller) return;
          announce(error.message, true);
        } finally {
          if (request === controller) {
            loading = false; el("gpuPickerRefresh").disabled = false; render();
          }
        }
      }

      const close = () => { if (!saving) dialog.close(); };
      el("closeGpuPickerBtn").addEventListener("click", close);
      el("cancelGpuPickerBtn").addEventListener("click", close);
      dialog.addEventListener("cancel", event => { if (saving) event.preventDefault(); });
      dialog.addEventListener("close", () => { request?.abort(); request = null; });
      el("gpuPickerRefresh").addEventListener("click", refresh);
      cloud.addEventListener("change", refresh);
      ["gpuReferenceSelect", "gpuMinVram", "gpuMinSpeed", "gpuSort", "gpuAvailableOnly"].forEach(id => el(id).addEventListener("change", render));
      el("gpuSearchInput").addEventListener("input", render);
      apply.addEventListener("click", async () => {
        if (apply.disabled) return;
        saving = true;
        cloud.disabled = true; el("gpuPickerRefresh").disabled = true;
        render();
        try {
          const response = await fetch("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ gpu_type_id: pending, cloud_type: cloud.value }) });
          if (!response.ok) throw new Error("Auswahl konnte nicht gespeichert werden. Bitte erneut versuchen.");
          onSaved(pending, cloud.value);
          dialog.close();
        } catch (error) {
          announce(error.message, true);
        } finally {
          saving = false; cloud.disabled = false; el("gpuPickerRefresh").disabled = false; render();
        }
      });
      this.open = () => {
        if (dialog.open) return;
        pending = getSelected(); cloud.value = getCloud();
        dialog.showModal();
        refresh();
      };
      el("openGpuPickerBtn").addEventListener("click", this.open);
    }
  };
})();
