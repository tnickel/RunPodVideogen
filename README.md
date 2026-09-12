# 🚀 RunPod VideoGen Studio (Wan2GP Orchestrator)

Professionelles, modernes Steuerungs-Dashboard zur automatisierten Orchestrierung von RunPod-GPU-Instanzen für die Video-Generierung mit **Wan2GP** und dem Modell **MiniMax H3 FL2VA Pruned 20B (PDD 8-Step)**.

---

## ✨ Features

- **Komfortables Dark-Mode Web-Dashboard:**
  - Glassmorphism UI mit Echtzeit-Statusindikatoren und Animationen.
  - Live-Kosten-Tracker & Stoppuhr (sekundengenaue Kostenabrechnung der laufenden Session).
  - 1-Klick Aktionen: *Pod Starten / Aufwecken*, *GPU Pausieren (Stop)*, *Beenden (0€ Terminate)*.
  - Direkter One-Click Button zur **Wan2GP WebUI** über den sicheren RunPod HTTP-Proxy.
- **Kostenfalle-Schutz (Auto-Shutdown Watchdog):**
  - Einstellbarer Timer (z. B. 30 Minuten Inaktivität) schaltet die GPU automatisch ab.
- **Dual-Standby-Modus:**
  - **⚡ Schnellstart:** GPU pausiert, Cloud-Volume (75 GB) bleibt für ca. 0,15 $/Tag erhalten (Startzeit ~45s).
  - **💰 0,- € Sparfuchs:** Pod wird vollständig terminiert (0,00 € Standby-Kosten).
- **Vorkonfiguriert für MiniMax H3:**
  - Automatisches Setup von Wan2GP im persistenten Verzeichnis `/workspace/Wan2GP`.
  - Umlenkung aller Caches auf das Volume (`HF_HOME=/workspace/.cache/huggingface`).
- **Integrierte Video-Galerie & Auto-Sync:**
  - Lädt neu gerenderte MP4-Videos im Hintergrund per SFTP automatisch auf deinen lokalen PC nach `outputs/`.
  - Integrierter Video-Player im Browser.
  - 1-Klick-Öffnen des lokalen Windows-Ordners.
- **Live Cloud-Terminal:**
  - Echtzeit-Streaming aller Pod-Logs (Bootstrap, Git Clone, Pip Install, Modell-Downloads).

---

## 🛠️ Schnellstart

1. Doppelklick auf `start.bat`
2. Das Dashboard öffnet sich automatisch unter:
   ```
   http://localhost:7880
   ```
3. Klicke auf **„🚀 Pod Starten“** – das System bucht die GPU, richtet Wan2GP ein und schaltet den direkten Link zur WebUI frei.
