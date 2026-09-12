# RunPod VideoGen Studio – Setup & Troubleshooting Dokumentation

Dieses Dokument protokolliert alle technischen Erkenntnisse, Versionskonflikte und Lösungen, die bei der automatisierten Einrichtung von **Wan2GP** auf RunPod-Instanzen aufgetreten sind.

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
  Im automatisierten Bootstrap-Skript (`backend/orchestrator.py`) wird `triton.jit` gepatcht, um `do_not_specialize_on_alignment` abzufangen und transparent an das installierte Triton weiterzuleiten:
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
  Vollständige Entkopplung über:
  ```bash
  nohup python wgp.py --server-port 7860 --server-name 0.0.0.0 </dev/null > /workspace/wan2gp.log 2>&1 &
  ```

---

### Problem 4: Community Cloud GPU Verfügbarkeit
* **Symptom:**  
  `RunPod API Fehler: There are no longer any instances available with the requested specifications.`
* **Ursache:**  
  Günstige Consumer-Karten wie RTX 4090 sind in der Community Cloud oft temporär vergriffen.
* **Lösung:**  
  * **Empfehlung:** Wechsel auf **NVIDIA A40 (48 GB VRAM)** für ~0,35 $/h (nahezu identischer Preis wie RTX 4090, aber doppelt so viel VRAM – ideal für 20B Modelle).
  * **Interaktives Error-Handling:** Das Frontend bietet bei Nicht-Verfügbarkeit direkte 1-Klick-Buttons, um sofort auf A40 oder Secure Cloud zu wechseln.

---

## 2. Persistenz-Konzept (Zero-Reinstall)
* **Mount-Punkt:** `/workspace` ist das einzige persistente Volume auf RunPod.
* **Wan2GP & Modell-Speicherort:** `/workspace/Wan2GP/` und `/workspace/.cache/huggingface/` liegen vollständig auf diesem Volume.
* **Standby-Modus ("GPU Pausieren"):**  
  Beim Klick auf *GPU Pausieren* wird `stop_pod` aufgerufen. Die GPU wird freigegeben (0,00 $/h), während das Volume erhalten bleibt (~17¢/Tag).  
  Beim erneuten Starten (`Resume`) sind alle Dateien und Modelle bereits vorhanden – die Startzeit verkürzt sich von ~6 Minuten auf ~30 Sekunden.
