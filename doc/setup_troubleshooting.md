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

## 2. Persistenz-Konzept (Zero-Reinstall)

* **Mount-Punkt:** `/workspace` ist das einzige persistente Volume auf RunPod.
* **Wan2GP & Modell-Speicherort:** `/workspace/Wan2GP/` und `/workspace/.cache/huggingface/` liegen vollständig auf diesem Volume.
* **Standby-Modus ("GPU Pausieren"):**  
  Beim Klick auf *GPU Pausieren* wird `stop_pod` aufgerufen. Die GPU wird freigegeben (**0,00 $/h**), während das Volume erhalten bleibt (~17¢/Tag bei 75 GB).  
  Beim erneuten Starten (`Resume`) sind alle Dateien und Modelle bereits vorhanden – die Startzeit verkürzt sich von ~6 Minuten auf ~30 Sekunden.
* **Terminieren (Pod Löschen):**  
  Wird nur auf expliziten Klick im Dashboard durchgeführt. Löscht das Volume unwiderruflich (0,00 € laufende Kosten).

---

## 3. Vollautomatisierter 1-Klick-Bootstrap (`scripts/bootstrap_pod.sh`)

Sollte der Pod jemals komplett gelöscht oder auf einem neuen Account/Provider aufgesetzt werden, fasst das Skript [`scripts/bootstrap_pod.sh`](file:///d:/AntiGravitySoftware/GitWorkspace/RunPodVideogen/scripts/bootstrap_pod.sh) alle Schritte zusammen:

1. Verzeichnisse anlegen (`/workspace/.cache/huggingface`, `/workspace/Wan2GP/outputs`, `ckpts`)
2. Wan2GP Repository klonen & `pip install -r requirements.txt`
3. Triton-Patch anwenden (Problem 1)
4. PyTorch 2.5 Buffer-Polyfill injizieren (Problem 5)
5. WebUI im Hintergrund starten via `setsid` (Problem 2 & 3)

Dieses Skript wird vom Backend ([backend/orchestrator.py](file:///d:/AntiGravitySoftware/GitWorkspace/RunPodVideogen/backend/orchestrator.py)) bei jedem neuen Pod automatisch per SSH ausgeführt.

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
