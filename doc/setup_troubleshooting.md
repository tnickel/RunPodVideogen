# RunPod VideoGen Studio – Setup & Troubleshooting Dokumentation

Dieses Dokument protokolliert alle technischen Erkenntnisse, Versionskonflikte, Lösungen und Automatisierungsskripte, die bei der Einrichtung von **Wan2GP** auf RunPod-Instanzen aufgetreten sind. 

Ziel ist es, bei jeder temporären Neu-Bereitstellung eines Pods eine vollständig automatisierte, fehlerfreie Installation in wenigen Minuten zu garantieren, ohne manuelle Debug-Schritte wiederholen zu müssen.

---

## 1. Gefundene Probleme & Lösungen

### Problem 1: Triton Inkompatibilität (`do_not_specialize_on_alignment`)
* **Symptom:**  
  Beim Start von `wgp.py` bricht der Prozess sofort ab mit:
  ```text
  File "/workspace/Wan2GP/shared/llm_engines/nanovllm/layers/activation.py", line 14, in <module>
    @triton.jit(do_not_specialize=("TOTAL",), do_not_specialize_on_alignment=("TOTAL",))
  TypeError: jit() got an unexpected keyword argument 'do_not_specialize_on_alignment'
  ```
* **Ursache:**  
  Das offizielle RunPod PyTorch 2.4.0 Container-Image (`runpod-torch-v240`) wird mit `triton==3.0.0` ausgeliefert. Das Argument `do_not_specialize_on_alignment` wurde erst in Triton 3.2+ bzw. Nightly eingeführt.
* **Lösung:**  
  Im automatisierten Bootstrap-Skript wird `triton.jit` gepatcht, um `do_not_specialize_on_alignment` abzufangen und transparent an das installierte Triton weiterzuleiten:
  ```python
  _orig_jit = jit
  def _compatible_jit(*args, **kwargs):
      kwargs.pop('do_not_specialize_on_alignment', None)
      return _orig_jit(*args, **kwargs)
  jit = _compatible_jit
  ```

---

### Problem 2: CLI-Argumente von Wan2GP (`--server-port` vs `--server_port`)
* **Symptom:**  
  `wgp.py: error: unrecognized arguments: --server_port 7860 --server_name 0.0.0.0`
* **Ursache:**  
  Wan2GP verwendet `argparse` mit Bindestrichen statt Unterstrichen.
* **Lösung:**  
  Aufruf wurde im Starter auf `--server-port 7860 --server-name 0.0.0.0` angepasst.

---

### Problem 3: SSH-Prozess-Daemonisierung (Hang bei Paramiko)
* **Symptom:**  
  Beim Starten von Hintergrund-Diensten per SSH blieb der SSH-Kanal hängen, weil `nohup` standardmäßig noch mit der Standard-Eingabe (stdin) der SSH-Sitzung verknüpft war.
* **Lösung:**  
  Vollständige Entkopplung über `setsid` und I/O-Redirection:
  ```bash
  setsid python wgp.py --server-port 7860 --server-name 0.0.0.0 </dev/null > /workspace/wan2gp.log 2>&1 &
  ```

---

### Problem 4: Community Cloud GPU Verfügbarkeit
* **Symptom:**  
  `RunPod API Fehler: There are no longer any instances available with the requested specifications.`
* **Ursache:**  
  Günstige Consumer-Karten wie RTX 4090 sind in der Community Cloud oft temporär vergriffen.
* **Lösung:**  
  * **Empfehlung:** Wechsel auf **NVIDIA A40 (48 GB VRAM)** für ~0,35 $/h bis 0,49 $/h (nahezu identischer Preis wie RTX 4090, aber doppelt so viel VRAM – ideal für 20B Modelle).
  * **Interaktives Error-Handling:** Das Dashboard bietet bei Nicht-Verfügbarkeit direkte 1-Klick-Buttons, um sofort auf A40 oder Secure Cloud zu wechseln.

---

### Problem 5: `module 'torch.nn' has no attribute 'Buffer'` (z. B. bei MiniMax H3 / mmaudio)
* **Symptom:**  
  In der WanGP WebUI erscheint beim Auswählen des Profils *MiniMax H3* oder beim Laden von Audio-Synthese-Modulen (`mmaudio`) eine rote Fehlermeldung:
  ```text
  Error
  module 'torch.nn' has no attribute 'Buffer'
  ```
* **Ursache:**  
  `torch.nn.Buffer` wurde erst in **PyTorch 2.5** eingeführt. Das Standard-RunPod Container-Image verwendet **PyTorch 2.4.1**. Neuere Repositories und Audio-Pipelines nutzen `nn.Buffer(..., persistent=...)` für Parameterdeklarationen.
* **Lösung:**  
  Transparentes Polyfill am Anfang von `/workspace/Wan2GP/wgp.py` und im Bootstrap-Skript:
  ```python
  if not hasattr(nn, "Buffer"):
      class _Buffer(torch.Tensor):
          def __new__(cls, data=None, *, persistent=True):
              if data is None: data = torch.empty(0)
              t = data.detach().as_subclass(cls) if isinstance(data, torch.Tensor) else torch.tensor(data).as_subclass(cls)
              t.persistent = persistent
              return t
      nn.Buffer = _Buffer
      _orig_module_setattr = nn.Module.__setattr__
      def _compatible_module_setattr(self, name, value):
          if isinstance(value, _Buffer):
              self.register_buffer(name, value, persistent=getattr(value, "persistent", True))
          else:
              _orig_module_setattr(self, name, value)
      nn.Module.__setattr__ = _compatible_module_setattr
  ```
  Damit verhält sich PyTorch 2.4 exakt wie PyTorch 2.5 und alle abhängigen Bibliotheken wie `mmaudio` laden fehlerfrei.

---

### Problem 6: „Connection to server lost. Reconnecting…“ trotz laufendem WanGP

Am 17.09.2026 bei WanGP v13.10 reproduziert: Die Startseite und Gradio-SSE
antworteten, aber Browser-POSTs wurden mit `403: Cross-origin request rejected`
abgewiesen. Auch der Deepy-WebSocket erhielt HTTP 403. Der RunPod-Proxy ersetzt
den `Host` durch seine interne IP mit Port; die `Origin` des Browsers bleibt die
öffentliche HTTPS-Adresse. WanGPs `same_origin()` verglich diese unterschiedlichen
Adressen direkt. Ein `404: Session not found` im Gradio-Log war deshalb allein
keine ausreichende Erklärung des sichtbaren Verbindungsfehlers.

Der Bootstrap führt `scripts/patch_wan2gp_proxy.py` mit der konkreten öffentlichen
Pod-Adresse aus. Der Patch ergänzt genau diese HTTPS-Origin; fremde Origins und
andere Pod-Adressen bleiben abgewiesen. Er übernimmt keine Freigaben aus
eingehenden Forwarded-Headern und verändert die Passwortprüfung nicht. Die
Originaldatei wird einmalig als `web.py.runpod.bak` gesichert. Änderungen an einem
bereits laufenden WanGP werden erst nach dessen Prozessneustart aktiv.

Für den manuellen Bootstrap `bootstrap_pod.sh`, `patch_wan2gp_proxy.py` und
`check_pod_cuda.py` nach `/workspace` kopieren und starten:

```bash
WANGP_PUBLIC_ORIGIN=https://POD_ID-7860.proxy.runpod.net bash /workspace/bootstrap_pod.sh
```

Die automatische Readiness-Prüfung prüft zusätzlich eine leere Queue-Anfrage mit
der Browser-Origin. Erwartet wird HTTP 422 (fehlende Pflichtdaten), nicht HTTP 403;
dabei wird kein Generierungsauftrag angelegt. Bei erfolgloser Startprüfung meldet
das Dashboard einen Fehler und liest `/workspace/wan2gp.log` aus.

### Problem 7: CUDA-Fehler trotz funktionierendem `nvidia-smi`

Am 17.09.2026 ließ sich beim L40S-Pod `s28ajy08hp3s8s` die GPU auflisten,
aber bereits `cuInit(0)` aus `libcuda.so.1` lieferte Fehler 999
(`CUDA_ERROR_UNKNOWN`). Das Öffnen von `/dev/nvidia-uvm` scheiterte zusätzlich
mit `EIO` / errno 5. Der Fehler trat damit auch unabhängig von WanGP und PyTorch
auf; eine Neuinstallation von WanGP ist dafür keine begründete Lösung.

Der Bootstrap prüft jetzt vor jeder Paketinstallation mit
`scripts/check_pod_cuda.py` den Zugriff auf das UVM-Gerät und die
CUDA-Treiberinitialisierung. Fehler erscheinen direkt im Dashboard. Das Skript
verändert keine Geräte oder Treiber. Bei diesem Befund muss der GPU-Zugriff auf
Host-/Container-Ebene wiederhergestellt werden; ein funktionierendes
`nvidia-smi` allein genügt als Prüfung nicht.

### Startfortschritt im Dashboard

Der Start meldet die Phasen Buchung, Container, SSH/GPU-Prüfung, Installation
und WebUI-Start. Installationsausgaben werden während des SSH-Kommandos
übertragen. Die Anzeige enthält Phase, Laufzeit, letzte Meldung und deren Alter.
Da Buchung und Paketinstallation keine verlässliche Gesamtmenge liefern, zeigt
der Balken Aktivität ohne erfundene Prozentzahl oder Restzeit. Bei einem Fehler
stoppt er und behält die letzte Meldung. Stoppen oder Löschen entwertet ältere
Startaufträge, damit deren spätere Ergebnisse den aktuellen Zustand nicht
überschreiben.

---

## 2. Persistenz beim Stoppen und Wiederaufnehmen

* **Mount-Punkt:** In dieser Pod-Konfiguration liegt das persistente Pod-Volume unter `/workspace`.
* **Wan2GP & Modell-Speicherort:** `/workspace/Wan2GP/` und `/workspace/.cache/huggingface/` liegen vollständig auf diesem Volume.
* **Standby-Modus ("GPU Pausieren"):**  
  Beim Klick auf *GPU Pausieren* wird `stop_pod` aufgerufen. Die GPU wird freigegeben, während das Pod-Volume kostenpflichtig erhalten bleibt. Speichertarife sind in [cost_management.md](cost_management.md) beschrieben.
  Beim erneuten Starten (`Resume`) bleiben Dateien und Modelle auf diesem Volume erhalten. Python-Pakete außerhalb des Volumes können fehlen; deshalb prüft der Bootstrap die Abhängigkeiten erneut. Eine feste Startdauer lässt sich daraus nicht ableiten.
* **Terminieren (Pod Löschen):**  
  Wird nur auf expliziten Klick im Dashboard durchgeführt. Löscht das Volume unwiderruflich (0,00 € laufende Kosten).

---

## 3. Vollautomatisierter 1-Klick-Bootstrap (`scripts/bootstrap_pod.sh`)

Sollte der Pod jemals komplett gelöscht oder auf einem neuen Account/Provider aufgesetzt werden, fasst das Skript [`scripts/bootstrap_pod.sh`](file:///d:/AntiGravitySoftware/GitWorkspace/RunPodVideogen/scripts/bootstrap_pod.sh) alle Schritte zusammen:

1. GPU-Zugriff mit `check_pod_cuda.py` prüfen
2. Verzeichnisse anlegen (`/workspace/.cache/huggingface`, `/workspace/Wan2GP/outputs`, `ckpts`)
3. Wan2GP Repository bei Bedarf klonen und Abhängigkeiten mit `pip install -r requirements.txt` prüfen
4. Triton-Patch und Buffer-Polyfill anwenden (Probleme 1 und 5)
5. RunPod-Origin mit `patch_wan2gp_proxy.py` eintragen (Problem 6)
6. WebUI im Hintergrund starten via `setsid` (Probleme 2 und 3)

Der automatische Start in `backend/orchestrator.py` enthält die entsprechende
SSH-Bootstrap-Sequenz und bettet die beiden Python-Helfer ein. Das Shell-Skript
ist zusätzlich für einen manuellen Start vorgesehen. Änderungen an dieser
Sequenz liegen lokal im Projekt und werden beim nächsten Bootstrap verwendet.

---

## 4. Schnell-Checkliste zur Verifikation

Nach Bereitstellung oder Wiederaufnahme eines Pods kann der Zustand in Sekunden geprüft werden:

```bash
# 1. Läuft der WanGP-Prozess?
ps aux | grep wgp.py | grep -v grep

# 2. Antwortet der lokale Webserver?
curl -s -I http://127.0.0.1:7860 | head -n 5

# 3. Importiert mmaudio ohne Buffer-Fehler?
python3 -c "import sys; sys.path.insert(0, '/workspace/Wan2GP'); import wgp; from shared.modules.mmaudio.model.flow import FlowMatching; print('OK')"

# 4. Logs einsehen:
tail -n 30 /workspace/wan2gp.log
```

---

## 5. Speicher-Diagnose: System-RAM vs. GPU VRAM

* **Symptom:**  
  Im RunPod-Dashboard oder in Monitoring-Tools erscheint die Warnung:  
  `high memory utilization - 42.98GiB / 46.57GiB` (oder `43.15GiB / 46.57GiB`).
* **Ursache:**  
  Dies ist **nicht** der GPU-VRAM (die NVIDIA A40 hat 48 GB dedizierten Grafikspeicher), sondern der **Container System-RAM** (vom Linux cgroup Memory Limit auf ca. 46,57 GiB gedeckelt).
  - **Wan2GP nutzt MMGP Offloading:** Die unkomprimierten 20-Milliarden-Parameter-Modellgewichte von MiniMax H3 FL2VA liegen im Host-Arbeitsspeicher gepinnt und werden schichtweise in den VRAM geschaufelt.
  - **Linux Page Cache:** Linux cached geladene Modell-Checkpoints aggressiv im freien RAM.
* **Erkenntnis:**  
  Dieses Verhalten ist für 20B Video-Modelle auf RunPod völlig normal und erwünscht. Es liegt kein Memory-Leak vor, solange der Prozess nicht vom Linux OOM-Killer (Out of Memory) beendet wird.

---

## 6. Performance-Benchmarks: Auflösung vs. Berechnungsdauer

Die Pixelmenge skaliert quadratisch und bestimmt direkt die Tensorskalierung in Wan2GP:

| Auflösung | Pixel pro Frame | Pixel-Faktor | Zeit pro Step | Gesamtdauer (8 Steps) | Praxistipp |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **256p** (`448x256`) | 114.688 | 1.0x | ~4.9s | **~39 Sekunden** | Ideal für schnelle Vorab-Tests & Motion-Checks |
| **480p** (`832x480`) | 399.360 | 3.5x | ~14.5s | **~1.9 Minuten** | **Standard-Empfehlung:** Optimales Verhältnis aus Schärfe & Speed |
| **720p** (`1280x720`) | 921.600 | 8.0x | ~58s | **~7.7 Minuten** | Hochwertige finale Renderings |
| **1080p** (`1920x1088`) | 2.088.960 | **18.2x** | ~260s | **~32 Minuten** | Nur für finales Upscaling / High-End Master |

> [!TIP]
> Beginne immer mit **480p** (`832x480`). Wenn Bewegung und Bildaufbau gefallen, kann das Video gezielt in 720p oder 1080p neu gerechnet werden.
