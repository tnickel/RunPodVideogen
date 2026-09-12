# BUGFIX-TASKS: Verdeckte Fehler in RunPod VideoGen Studio

> **Anweisung für das bearbeitende KI-System (z. B. Claude / Copilot / Cursor):**
>
> Dieses Dokument enthält das Ergebnis eines vollständigen Code-Reviews des Projekts
> (Stand: Commit `c923fb0`, alle Zeilenangaben beziehen sich auf diesen Stand — bei
> Zeilenversatz bitte über die mitgelieferten Code-Snippets suchen).
>
> Arbeitsweise:
> 1. **Prüfe jeden Befehl zuerst nach** (Verifikations-Abschnitt), bevor du ihn fixt.
>    Manche Angaben können sich seit dem Review verschoben haben.
> 2. Fixe die Tasks **in Prioritäts-Reihenfolge** (P0 → P1 → P2 → P3).
> 3. Ändere nur das beschriebene Verhalten, kein Refactoring beyond scope.
> 4. **Sicherheit:** In `config.json` steht ein echter RunPod-API-Key.
>    Niemals den Key ausgeben, loggen, in Diff-Kommentare schreiben oder committen.
>    `config.json` bleibt ignoriert (`.gitignore`).
> 5. Nach allen Fixes: Smoke-Test (siehe letzter Abschnitt) und Zusammenfassung
>    der Änderungen pro Task erstellen.

**Projekt-Kontext:** FastAPI-Backend (`server.py`, `backend/`), statisches Frontend
(`static/`), steuert RunPod-GPU-Pods per GraphQL-API und SSH/SFTP (Wan2GP-Video-Generierung).
Python 3.12, pydantic 2.7, FastAPI 0.111, paramiko 5.0, requests 2.32 (aktuell installiert).

---

## P0 — Kritisch

### [P0-1] Resume-Logik ist toter Code → jeder „Pod Aufwecken“-Klick deployt einen NEUEN Pod, alte Pods verwaisten (Kostenleck)

**Ort:** `backend/orchestrator.py`, Zeile ~137, in `_deploy_or_resume_worker()`:

```python
if pod_info and pod_info.get("desiredStatus") in ["STOPPED", "PAUSED"]:
    can_resume = True
```

**Problem:** Die RunPod-API liefert für `desiredStatus` nur die Werte
`RUNNING`, `EXITED`, `TERMINATED` (Quelle: RunPod-API-Doku, List Pods).
Die Werte `"STOPPED"` / `"PAUSED"` existieren dort **nicht** → die Bedingung ist
immer `False` → `can_resume` wird nie `True`.

**Folge:** Der Button „Pod Aufwecken (Resume 45s)“ deployt immer einen **neuen** Pod
(leeres Volume → voller Bootstrap statt 45 s). Der alte, gestoppte Pod bleibt mit
seinem 75-GB-Volume auf dem RunPod-Account liegen und kostet unbemerkt weiter
Speichergebühren (~$5,25/Monat). `active_pod_id` wird überschrieben → die App
verliert die Spur zu den Alt-Pods dauerhaft.

**Verifikation:**
1. Code-Stelle oben finden.
2. RunPod-Doku prüfen (https://docs.runpod.io/api-reference/pods/GET/pods):
   Enum `desiredStatus` = `RUNNING | EXITED | TERMINATED`.
3. Optional live: Pod stoppen, dann „Aufwecken“ klicken und im Terminal-Log prüfen,
   ob „Reaktiviere bestehenden Pod…“ erscheint (erscheint aktuell nie).

**Fix:**
```python
if pod_info and pod_info.get("desiredStatus") == "EXITED":
    can_resume = True
```

**Zusätzlich (gleiche Funktion):** Fall „Pod existiert und läuft bereits“ behandeln,
damit nach einem Fehler/Restart kein zweiter Pod deployed wird:
```python
elif pod_info and pod_info.get("desiredStatus") == "RUNNING":
    # Pod läuft schon (z. B. nach ERROR oder Server-Restart) -> wiederverwenden,
    # kein resume_pod und kein deploy; direkt in die Warteschleife auf IP/SSH-Port springen.
```
(Dazu `can_resume` zu einem Drei-Wege-Zustand machen oder eine Variable `reuse_existing` einführen.)

**Abnahme:** Pod stoppen → „Aufwecken“ → Log zeigt „Reaktiviere bestehenden Pod“,
es wird **kein** zweiter Pod in der RunPod-Konsole erzeugt.

**Hinweis für den Nutzer (nicht Code):** In der RunPod-Web-Konsole nach
verwaisten gestoppten Pods suchen und diese ggf. terminieren.

---

### [P0-2] `/api/config` (GET) leakt den unmaskierten RunPod-API-Key

**Ort:** `server.py`, Zeilen ~217–223:

```python
@app.get("/api/config")
def get_config_endpoint():
    cfg = load_config()
    key = cfg.get("runpod_api_key", "")
    masked = key[:6] + "..." + key[-4:] if len(key) > 10 else key
    return {**cfg, "runpod_api_key_masked": masked}
```

**Problem:** `**cfg` spreads **alle** Config-Felder — inklusive des Klartext-Keys.
Die Maskierung ist wirkungslos. Kombiniert mit `uvicorn.run(..., host="0.0.0.0")`
(`server.py`, Zeile ~265) kann jeder Rechner im LAN den Key per
`GET http://<host>:7880/api/config` auslesen.

**Verifikation:** Server starten, `curl http://localhost:7880/api/config` —
Antwort enthält Feld `runpod_api_key` im Klartext.

**Fix:**
```python
@app.get("/api/config")
def get_config_endpoint():
    cfg = load_config()
    key = cfg.get("runpod_api_key", "")
    masked = key[:6] + "..." + key[-4:] if len(key) > 10 else key
    safe = {k: v for k, v in cfg.items() if k != "runpod_api_key"}
    safe["runpod_api_key_masked"] = masked
    return safe
```

**Folge-Fix im Frontend** (`static/js/app.js`, ~Zeile 924, Settings-Modal-Load),
weil es bisher den Klartext-Key ins Feld schreibt:
```javascript
cfgApiKey.value = cfg.runpod_api_key_masked || "";
cfgApiKey.placeholder = "Neuen Key nur eingeben, wenn er geändert werden soll";
```
Und beim Speichern (~Zeile 939–953) den Key nur mitschicken, wenn er wirklich
geändert wurde (maskiertes Value mit „...“ erkennen und weglassen; leeres Feld
darf dann **keinen** Fehler mehr auslösen, sondern lässt den Key unverändert):
```javascript
const body = { account_balance: bal };
const key = cfgApiKey.value.trim();
if (key && !key.includes("...")) body.runpod_api_key = key;
// backend: fehlendes Feld = keine Änderung (bereits über `if v is not None` abgedeckt)
```

**Optional härter:** Host-Binding konfigurierbar machen (Default `127.0.0.1`),
z. B. Config-Schlüssel `server_host`, verwendet in `server.py` am Ende.

**Abnahme:** `curl /api/config` enthält kein Klartext-Key-Feld mehr; Settings-Modal
zeigt Maskierung; Speichern ohne Key-Eingabe löscht den vorhandenen Key nicht.

---

### [P0-3] WebSocket-Live-Logs funktionieren nicht (Logs aus Threads werden nie gebroadcastet)

**Ort:** `server.py`, Zeilen ~44–50:

```python
def forward_log_to_ws(entry: str):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(entry), loop)
    except Exception:
        pass
```

**Problem:** Der Callback läuft in Worker-Threads (Deploy-Thread, Background-Loop,
FastAPI-Threadpool für sync-Endpoints). `asyncio.get_event_loop()` wirft in
Nicht-Main-Threads unter Python 3.12 `RuntimeError` → `except: pass` schluckt alles.
**Alle** Log-Einträge stammen aus Threads → es wird nie etwas gesendet. Das
Terminal zeigt nur den einmaligen Replay der letzten 50 Einteile beim Verbinden
(`recent_logs`), danach still bis zum Page-Reload.

**Verifikation:** Server starten, zwei Browser-Terminals öfften; eine Aktion
auslösen (z. B. „Status“-Refresh oder Pod Start) → im zweiten Terminal kommt
keine neue Zeile an. Alternativ Breakpoint/Log in `forward_log_to_ws` setzen.

**Fix:** Den Loop beim Startup in einer async-Kontext einsammeln:
```python
_main_loop: asyncio.AbstractEventLoop | None = None

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    yield

app = FastAPI(title="RunPod VideoGen Studio", version="1.0.0", lifespan=lifespan)
# ( vorhandene app = FastAPI(...) Zeile entsprechend erweitern )

def forward_log_to_ws(entry: str):
    if _main_loop is not None and _main_loop.is_running():
        asyncio.run_coroutine_threadsafe(ws_manager.broadcast(entry), _main_loop)
```

**Abnahme:** Log-Zeilen (z. B. „Einstellungen aktualisiert…“) erscheinen ohne
Reload live im Terminal beider offener Tabs.

---

### [P0-4] ERROR-Zustand mit existierendem Pod: UI zeigt „0 € GPU-Kosten“ und Terminieren ist deaktiviert

**Ort:** `static/js/app.js`

a) Ampel-Bedingung, ~Zeile 197:
```javascript
} else if (status === "STOPPED" || data.pod_id) {
```
b) Button-Zustände, ~Zeilen 321–329 (Else-Zweig für OFFLINE/TERMINATED/**ERROR**):
```javascript
btnPause.disabled = true;
btnTerminate.disabled = true;
```

**Problem:** Wenn ein Pod deployed wurde, aber ein Folgeschritt fehlschlägt
(z. B. SSH-Port-Timeout → Status `ERROR`, `pod_id` persistiert, Pod **läuft auf
RunPod weiter und kostet Geld**), dann:
- schaltet die Ampel wegen `|| data.pod_id` auf **Gelb „GPU ist abgeschaltet (0,00 $/h)“** — falsch,
- ist der Löschen-Button deaktiviert → der Pod lässt sich über die UI **nicht** terminieren,
- deployt ein Klick auf „Pod Starten“ (siehe [P0-1]) einen **zweiten** Pod.

**Verifikation:** Code-Lesereicht: Ampel-Zweig und Else-Zweig der Button-Logik
prüfen; `orchestrator.terminate_pod()` hätte die nötige `pod_id`.

**Fix:**
- Ampel: `} else if (status === "STOPPED") {` (ohne `|| data.pod_id`).
- Button-Logik: `btnTerminate.disabled = !data.pod_id;` im ERROR-/OFFLINE-Zweig
  (d. h. immer aktiv, wenn eine `pod_id` existiert). `btnStart` darf im ERROR-Fall
  aktiv bleiben, aber durch [P0-1]-Fix (Wiederverwenden laufender Pods) entsteht
  dann kein Doppel-Pod mehr.

**Abnahme:** Simulierten ERROR-Zustand herstellen (z. B. `active_pod_status:
"ERROR"` + gültige `active_pod_id` in `config.json`, Server-Neustart) → Ampel
zeigt nicht Gelb/„0 €“, Löschen-Button ist klickbar.

---

## P1 — Schwerwiegend

### [P1-5] Guthaben-Pflege ist dreifach kaputt

**Ort / Probleme:**

a) **Settings-Speichern verwirft die Balance kommentarlos.** Das Frontend schickt
   `account_balance` mit (`static/js/app.js`, ~Zeilen 946–950), aber
   `ConfigUpdateModel` (`server.py`, ~Zeilen 54–61) hat dieses Feld nicht →
   Pydantic 2 ignoriert unbekannte Felder stillschweigend.
   **Fix:** `account_balance: float | None = None` in `ConfigUpdateModel` ergänzen
   (Backend), plus Frontend-Anpassung aus [P0-2] (Body baut Balance immer ein).

b) **Totes Feld:** `VideoDeleteModel.account_balance` (`server.py`, ~Zeile 65) wird
   im Delete-Endpoint nie gelesen → Feld entfernen.

c) **Widersprüchliche Balance-Anzeige.** `/api/status` liefert den Rohwert
   `cfg.account_balance` **ohne** Abzug der Session-Kosten (`server.py`, ~Zeile 76),
   während `/api/account/refresh` `base - accumulated_cost` rechnet, aber nichts
   persistiert. Der 3-Sekunden-Poll des Frontends überschreibt den
   Refresh-Wert sofort → der Button wirkt „kaputt“.
   **Fix (einheitliche Formel):** In `get_status()`:
   ```python
   accumulated = float(state.get("accumulated_cost", 0.0))
   base = float(cfg.get("account_balance", 18.74))
   account_info["balance"] = max(0.0, round(base - accumulated, 2))
   ```
   `/api/account/refresh` berechnet exakt dieselbe Formel (bereits vorhanden) →
   kein Flackern mehr. Dokumentieren: Ein echter Kontostand ist über die genutzte
   GraphQL-`myself`-Query nicht verfügbar; der Refresh-Button-Titel im Frontend
   („von RunPod aktualisieren“, `static/index.html` ~Zeile 41) ist irreführend →
   Title-Text zu „Guthaben neu berechnen“ ändern.

**Verifikation:** Settings öffnen, Balance ändern, speichern, `config.json` prüfen
(aktuell: Wert bleibt unverändert). Während RUNNING Session Refresh klicken →
Wert springt und wird 3 s später wieder überschrieben.

**Abnahme:** Balance-Änderung wird persistiert; `/api/status` und
`/api/account/refresh` liefern denselben Wert; kein Überspringen im UI-Poll.

---

### [P1-6] Server-Restart bei laufendem Pod: halber Zustand (kein Sync, keine Kosten, kein Proxy)

**Ort:** `backend/orchestrator.py`, `__init__`, ~Zeilen 32–35:

```python
cfg = load_config()
self.pod_id = cfg.get("active_pod_id")
self.status = cfg.get("active_pod_status", "OFFLINE")
```

**Problem:** Bei Status `RUNNING` bleiben `pod_ip`, `pod_ssh_port`, `proxy_url`,
`session_start_time` = `None` und `hourly_rate` = Default 0.34. Folgen:
Auto-Sync macht nichts (braucht IP/Port), Kostenzähler bleibt 0, „WebUI öffnen“
ohne Ziel, und der Auto-Shutdown kann den Pod mitten in der Generierung stoppen
(`last_activity_time` = Serverstart; Sync als Aktivitätsquelle fällt aus).

**Verifikation:** Mit laufendem Pod den Server neu starten → Statusanzeige
„RUNNING“, aber `pod_ip` fehlt in `/api/status`, Galerie synchronisiert nicht.

**Fix:** Reconciliation beim Start — in `__init__` einen Daemon-Thread starten,
der einmalig `client.get_pod(pod_id)` aufruft:
- `desiredStatus == "RUNNING"`: aus `runtime.ports` den Eintrag mit
  `privatePort == 22` übernehmen (`ip`, `publicPort`), `proxy_url` per
  `RunPodClient.get_proxy_url(pod_id, 7860)` setzen,
  `hourly_rate` aus `costPerHr`, und
  `session_start_time = time.time() - runtime.uptimeInSeconds` setzen.
- `desiredStatus == "EXITED"`: `self.status = "STOPPED"` setzen.
- Pod nicht gefunden: `self.status = "OFFLINE"`, `pod_id = None`,
  Config bereinigen, Log-Hinweis.

**Abnahme:** Server-Restart mit laufendem Pod → nach wenigen Sekunden stehen
IP/SSH/Proxy/Kostenrate korrekt in `/api/status`, Sync läuft.

---

### [P1-7] Auto-Sync lädt unvollständige (korrupte) Videos herunter

**Ort:** `backend/ssh_client.py`, `sync_videos()`, ~Zeile 109:

```python
# Ignore files modified in the last 5 seconds (might still be writing)
```

**Problem:** Der Kommentar verspricht einen Frische-Check — er ist **nicht
implementiert**. Eine remote noch geschriebene Datei wird als „fertige“ mp4
geladen und erscheint korrupt in der Galerie (lokal stimmt die Größe zufällig
„überein“, sobald der Remote-Prozess stirbt, bleibt der Schrott für immer).

**Fix:** In der Schleife über `files` (vor dem Größenvergleich):
```python
import time
if time.time() - attr.st_mtime < 15:
    continue  # Datei wurde in den letzten 15 s verändert -> evtl. noch im Schreiben
```
(`attr.st_mtime` ist UTC-Epoch-Sekunden, direkt vergleichbar mit `time.time()`.)

**Verifikation:** Code inspizieren; optional: auf dem Pod eine große Datei in
`/workspace/Wan2GP/outputs` anlegen (`dd`) und Sync-Intervall abwarten →
Datei darf erst ~15 s nach letztem Schreiben heruntergeladen werden.

---

### [P1-8] `standby_mode` ist eine tote Einstellung

**Ort:** Frontend-Switch `modeStopBtn`/`modeTermBtn` (`static/js/app.js`,
~Zeilen 864–883) schreibt nur die Config. Das Backend liest `standby_mode`
**nirgends**; der Auto-Shutdown ruft immer `self.stop_pod()`
(`backend/orchestrator.py`, ~Zeile 356).

**Problem:** Wer „Standby: Löschen“ wählt, erwartet Terminierung bei Inaktivität —
es passiert immer nur ein Stop.

**Fix:** Im Auto-Shutdown-Zweig der `_background_loop`:
```python
if cfg.get("standby_mode", "stop") == "terminate":
    self.log("Auto-Shutdown: Inaktivität erreicht! Terminiere Pod (Modus 'Löschen')…", level="WARNING")
    self.terminate_pod()
else:
    self.stop_pod()
```
**Zusätzlich:** Tote Config-Schlüssel aufräumen oder dokumentieren —
`model_preset` wird ebenfalls nirgends gelesen.

---

## P2 — Robustheit

### [P2-9] Background-Thread stirbt still + nicht-atomare Config-Schreibvorgänge

**Ort:** `backend/orchestrator.py`, `_background_loop()` (~Zeilen 336–371) hat
kein try/except um den Loop-Body. `backend/config_manager.py`,
`save_config_unsafe()` (~Zeilen 52–54) schreibt direkt in die Zieldatei.

**Problem:**
- Ein einziger unerwarteter Fehler im Loop (z. B. `sync_interval_seconds` als
  String in `config.json` → `int >= str` → `TypeError`; `auto_shutdown_minutes:
  "abc"` → `ValueError`) killt Kosten-Tracking, Auto-Shutdown **und** Sync
  dauerhaft und unsichtbar — ausgerechnet den Kostenschutz-Watchdog.
- Ein Crash während `json.dump` korrumpt `config.json` → `load_config` fällt auf
  `DEFAULT_CONFIG` zurück → `active_pod_id` verloren → laufender Pod verwaist
  (Kostenfolge wie [P0-1]).

**Fix:**
```python
while not self._stop_bg_thread:
    time.sleep(2)
    try:
        ...  # bisheriger Body
    except Exception as e:
        self.log(f"Hintergrund-Dienst-Fehler (wird überlebt): {e}", level="ERROR")
        time.sleep(5)
```
Und atomar schreiben (in `config_manager.py`):
```python
import tempfile, os
def save_config_unsafe(cfg: dict):
    d = os.path.dirname(_CONFIG_PATH) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, _CONFIG_PATH)
    except Exception:
        try: os.remove(tmp)
        except OSError: pass
        raise
```
Zusätzlich defensive Typumwandlungen im BG-Loop:
`int(cfg.get("auto_shutdown_minutes", 30) or 0)` in try/except int(..., fallback).

---

### [P2-10] Bootstrap-Fehler wird als „bereit“ gemeldet; „bereit“ kommt zu früh

**Ort:** `backend/orchestrator.py`, `_bootstrap_wan2gp()` (~Zeilen 230–334) und
aufrufender Worker (~Zeilen 208–216).

**Problem:**
- Exit-Code != 0 erzeugt nur ein `WARNING`-Log, danach setzt der Worker trotzdem
  `status = "RUNNING"` / „Wan2GP aktiv & bereit“ → User öffnet Proxy → 502.
- Erst-Installation (git clone + pip + Modell-Laden) kann das 300-s-Channel-Timeout
  sprengen → `socket.timeout` Exception.
- Status „bereit“ wird bereits ~4 s nach dem Start von `wgp.py` gesetzt, obwohl
  das Modell noch Minuten lädt.

**Fix:**
1. `_bootstrap_wan2gp` gibt `bool` zurück (`False` bei `code != 0` oder Exception);
   der Worker bricht bei `False` mit `status = "ERROR"` +
   `status_detail = "Bootstrap fehlgeschlagen — Terminal prüfen"` ab.
2. SSH-Timeout für den Bootstrap auf 900 s erhöhen.
3. Nach dem Start von `wgp.py` readiness-pollen, bevor `RUNNING` gesetzt wird:
```python
for i in range(90):  # bis 7,5 Min
    code, out, _ = ssh.exec_command(
        "curl -sf -o /dev/null http://127.0.0.1:7860 && echo READY", timeout=10)
    if out.strip().endswith("READY"):
        break
    time.sleep(5)
```

---

### [P2-11] Dateinamen-Sonderzeichen: Sync-Endlosretry, kaputte URLs, unescaped Shell-String

**Ort/Probleme:**
a) Wan2GP leitet Output-Dateinamen aus Prompts ab; Linux erlaubt `: " < > ? * |`.
   Auf Windows schlägt das Anlegen der Datei in `sync_videos()` fehl →
   Endlos-Retry alle 15 s + Log-Spam.
   **Fix** (`backend/ssh_client.py`): Namen vor dem Download sanitisieren:
   ```python
   import re
   safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', fname).rstrip(' .')
   if not safe:
       continue
   if safe != fname:
       self._log(f"Dateiname für Windows angepasst: {fname!r} -> {safe!r}")
   fname = safe   # für local_path verwenden; remote_path mit Original-Namen lesen
   ```
b) Video-URLs sind nicht encodet (`server.py`, ~Zeile 154:
   `"url": f"/api/videos/file/{f}"`). Ein `#`, `%`, `+` oder `&` im Dateinamen
   macht Player/Download-Link kaputt (404).
   **Fix:** `"url": f"/api/videos/file/{urllib.parse.quote(f)}"`
   (`import urllib.parse` in `server.py`).
c) Remote-Löschen setzt den Dateinamen roh in Single-Quotes
   (`backend/orchestrator.py`, ~Zeile 411: `rm -f '...{safe_name}'`) — ein `'`
   im Namen bricht das Quoting (Shell-Injektion in die Root-Shell des Pods).
   **Fix:** `import shlex` … `rm -f {shlex.quote(remote_path)}`
d) Galerie-Bau setzt Dateinamen roh in `innerHTML`-Attribute
   (`static/js/app.js`, ~Zeilen 585–602). Die `data-url`/`data-title`/`data-meta`
   -Attribute werden dort gar nicht gelesen (der Click-Handler nutzt die
   Closure-Variablen) → Attribute einfach entfernen; Titel/Tooltips über
   `element.title = v.filename` setzen (textContent-Sicherheit).

---

### [P2-12] `/api/status` pollt bei jedem 3-s-Tick die RunPod-API (`get_myself`)

**Ort:** `server.py`, ~Zeile 79 (`get_status`), plus Retry-Strategie in
`backend/runpod_client.py` (3 Versuche mit Backoff).

**Problem:** Pro offenem Tab alle 3 s ein API-Call; bei Key-/Netz-Problemen
stauchen sich die FastAPI-Threadpool-Worker (anyio-Default 40 Threads) mit
mehrssekündigen Blocks → UI wirkt eingefroren.

**Fix:** E-Mail/Timestamp im Orchestrator cachen (TTL ~300 s), z. B. Methode
`orchestrator.get_account_email()` mit `self._email_cache`/`_email_ts`;
`get_status` verwendet den Cache. Bei Fehlern: letzten bekannten Wert
(`cfg.account_email`) liefern statt Exception.

---

### [P2-13] Retry-Policy liegt auch auf Mutations-POSTs → Doppel-Deploy-Risiko

**Ort:** `backend/runpod_client.py`, ~Zeilen 9–17:
`allowed_methods=("GET", "POST")` auf dem Session-Adapter, den **alle** Requests
nutzen — auch `deploy_pod` / `stop_pod` / `resume_pod` / `terminate_pod`.

**Problem:** Geht die Antwort nach erfolgreichem `podFindAndDeployOnDemand`
verloren (Timeout/Connection-Reset), wird erneut gesendet → **zwei gebuchte
Pods** (doppelte Kosten).

**Fix:** In `_request()` einen Parameter `mutating: bool = False` einführen;
für Mutationen einen Adapter ohne Retries verwenden:
```python
_no_retry_session = requests.Session()  # Modul-Ebene

def _request(self, query, variables=None, mutating=False):
    ...
    session = _no_retry_session if mutating else _session
    res = session.post(url, json=payload, headers=headers, timeout=25)
```
`deploy_pod`, `resume_pod`, `stop_pod`, `terminate_pod` rufen mit
`mutating=True` auf; Queries (`get_myself`, `get_gpu_types`, `get_pod`) bleiben
mit Retry.

---

### [P2-14] UI zeigt falsche GPU/Cloud-Typ an

**Ort:** `static/js/app.js` — `fetchGpus()` restauriert nur die Vorauswahl
(~Zeilen 551–556); `fetchStatus()` schreibt `data.config.gpu_type_id` /
`cloud_type` nie in die Dropdowns. Config sagt z. B. `NVIDIA A40`, das Dropdown
zeigt `RTX 4090`.

**Fix:** In `fetchStatus()`:
```javascript
if (data.config) {
  if (data.config.gpu_type_id &&
      Array.from(gpuSelect.options).some(o => o.value === data.config.gpu_type_id)) {
    gpuSelect.value = data.config.gpu_type_id;
  }
  if (data.config.cloud_type) cloudTypeSelect.value = data.config.cloud_type;
}
```
Und in `fetchGpus()` nach dem Neuaufbau der Options die Config-Bevorzugung
beibehalten (aktuelle Auswahl-Merker reicht, da fetchStatus alle 3 s nachzieht).

---

## P3 — Klein / Doku

### [P3-15] `requirements.txt` fehlt — README-Anleitung bricht bei frischem Clone
`pip install -r requirements.txt` (README) existiert nicht. Neue Datei erstellen:
```
fastapi>=0.111
uvicorn[standard]>=0.30
pydantic>=2.7
paramiko>=3.4
requests>=2.32
```
README korrigieren: `httpx` wird nicht benutzt (Code nutzt `requests`);
`websockets` kommt über `uvicorn[standard]`; `pydantic>=2` ist Pflicht
(`model_dump`).

### [P3-16] README-Falschmeldungen
- Behaupteter „Pre-Commit-Hook“ für `config.json` existiert nicht → Aussage
  entfernen oder Hook tatsächlich anlegen (`.pre-commit-config.yaml`).
- Beispiel-Config im README nutzt Schlüssel `default_gpu` — real ist der
  Schlüssel `gpu_type_id` → Beispiel korrigieren.

### [P3-17] `start.bat`: Browser-Race und hartkodierter Port
`start "" "http://localhost:7880"` öffnet den Browser, bevor der Server lauscht,
und ignoriert den konfigurierbaren `server_port`. Fix: Browser erst nach ~3 s
Verzögerung öffnen (z. B. `start "" cmd /c "timeout /t 3 >nul & start http://localhost:7880"`)
und idealerweise den Port aus `config.json` lesen (PowerShell-Einzeiler oder
Python-Hilfsaufruf).

### [P3-18] Hardcodierte Werte
- E-Mail als sichtbarer Fallback-Text in `static/index.html` (~Zeile 38) →
  neutraler Platzhalter („Account wird geladen…“).
- Disk-Preis `$0.07/GB/Monat` und Default-Balance `18.74` sind an mehreren
  Stellen dupliziert (server.py ×2, app.js) → zentral definieren (Config oder
  Konstante). Hinweis: Die 30-GB-Container-Disk fehlt in der Laufkosten-Rechnung
  des Cockpits (nur Volume wird gezählt) — zumindest im Info-Modal erwähnen.

### [P3-19] Frontend-Kleinigkeiten (optional)
- WS-Reconnect sendet den Log-Replay erneut → Duplikate im Terminal
  (z. B. Terminal vor dem Replay leeren oder Replay markieren).
- Galerie-Rebuild alle 10 s (`setInterval(fetchVideos, 10000)`) resettet die
  2-stufige Löschbestätigung → Rebuild überspringen, wenn ein Delete-Button
  im „confirming“-Zustand ist, oder Diffing einführen.
- Session-Timer jittert alle 3 s (Server-Wert überschreibt lokalen Sekundentakt)
  → lokalen Takt nur zwischen den Server-Updates interpolieren statt
  zurücksetzen.

### [P3-20] `delete_video()` liefert immer `True`
`backend/orchestrator.py`, ~Zeilen 395–416: Rückgabewert spiegelt nicht wider,
ob lokal/remote wirklich gelöscht wurde. → `bool` nach `os.path.exists`-Check
zurückgeben und im Endpoint entsprechend 404/400 liefern, wenn nichts gelöscht
werden konnte.

### [P3-21] Strategisch: RunPod-GraphQL ist deprecated
RunPod kündigt die Einstellung der GraphQL-API für früh 2027 an → mittelfristig
auf die Nachfolge-API migrieren (kein sofortiger Handlungsbedarf, aber im
Backlog halten). Ref: https://docs.runpod.io/sdks/graphql/configurations

---

## Smoke-Test nach allen Fixes (Definition of Done)

1. `python -c "import server"` — kein ImportError.
2. `python server.py` startet; `curl http://localhost:7880/api/status` liefert JSON
   mit `account.balance` = `base - accumulated_cost`.
3. `curl http://localhost:7880/api/config` enthält **keinen** Klartext-API-Key.
4. Zwei Browser-Tabs: Aktion auslösen → Live-Logs erscheinen in beiden (ohne Reload).
5. Pod stoppen → „Aufwecken“ → Log zeigt „Reaktiviere bestehenden Pod“, RunPod-Konsole
   zeigt weiterhin genau **einen** Pod.
6. Settings öffnen → Balance ändern → speichern → `config.json` enthält neuen Wert;
   API-Key bleibt unangetastet, wenn das Feld nicht ausgefüllt wurde.
7. `config.json` temporär einen ungültigen Wert geben (z. B. `sync_interval_seconds:
   "abc"`) → Server läuft weiter, Background-Dienst loggt Fehler statt zu sterben
   (danach wieder zurücksetzen!).
8. `git status`: `config.json` darf **niemals** in der Staging-Area landen.

**Quellen:** RunPod API-Referenz (desiredStatus-Enum):
https://docs.runpod.io/api-reference/pods/GET/pods · GraphQL-Deprecation:
https://docs.runpod.io/sdks/graphql/configurations
