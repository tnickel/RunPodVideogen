import os
import sys
import asyncio
import math
import subprocess
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Any, List
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from backend.config_manager import load_config, update_config, get_outputs_dir, DEFAULT_CONFIG
from backend.runpod_client import RunPodClient, gpu_offer_details
from backend.orchestrator import PodOrchestrator

_main_loop: asyncio.AbstractEventLoop | None = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    yield

app = FastAPI(title="RunPod VideoGen Studio", version="1.0.0", lifespan=lifespan)
orchestrator = PodOrchestrator()

# Lokale App: HTML/JS/CSS immer neu validieren lassen (ETag/304 bleibt gültig),
# damit nach einem Update nicht eine alte, defekte Version aus dem Browser-Cache läuft.
@app.middleware("http")
async def no_cache_for_ui(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "no-cache"
    return response

# Mount outputs directory for direct streaming if needed
outputs_dir = get_outputs_dir()

# WebSocket active connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)

ws_manager = ConnectionManager()

# Hook orchestrator logs to WebSocket
def forward_log_to_ws(entry: str):
    if _main_loop is not None and _main_loop.is_running():
        try:
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(entry), _main_loop)
        except Exception:
            pass

orchestrator.add_listener(forward_log_to_ws)

class ConfigUpdateModel(BaseModel):
    runpod_api_key: str | None = None
    account_balance: float | None = None
    gpu_type_id: str | None = None
    cloud_type: str | None = None
    volume_disk_gb: int | None = None
    standby_mode: str | None = None
    auto_shutdown_minutes: int | None = None
    auto_sync_videos: bool | None = None

class VideoDeleteModel(BaseModel):
    filename: str

# Routes
@app.get("/api/status")
def get_status():
    # Session booking moves accumulated_cost into spent_total under this lock.
    # Read both sides together so a stop cannot subtract the same cost twice.
    with orchestrator.lock:
        state = orchestrator.get_state()
        cfg = load_config()

    base_balance = float(cfg.get("account_balance", DEFAULT_CONFIG.get("account_balance", 0.0)))
    spent_total = float(cfg.get("spent_total", 0.0) or 0.0)
    accumulated = float(state.get("accumulated_cost", 0.0))
    balance = max(0.0, round(base_balance - spent_total - accumulated, 2))

    account_info = {
        "email": orchestrator.get_account_email(),
        "balance": balance
    }

    return {
        **state,
        "account": account_info,
        "config": {
            "gpu_type_id": cfg.get("gpu_type_id"),
            "cloud_type": cfg.get("cloud_type"),
            "volume_disk_gb": cfg.get("volume_disk_gb"),
            "standby_mode": cfg.get("standby_mode"),
            "auto_shutdown_minutes": cfg.get("auto_shutdown_minutes"),
            "auto_sync_videos": cfg.get("auto_sync_videos")
        }
    }

@app.get("/api/gpus")
def get_gpus(cloud_type: str | None = None):
    cfg = load_config()
    selected_cloud = (cloud_type or cfg.get("cloud_type", "ALL")).upper()
    if selected_cloud not in {"ALL", "SECURE", "COMMUNITY"}:
        raise HTTPException(status_code=400, detail="Ungültiger Cloud-Typ.")
    client = orchestrator.get_client()
    try:
        all_gpus = client.get_gpu_types()
        # This bootstrap uses NVIDIA CUDA and requires at least 24 GB VRAM.
        unavailable = orchestrator.get_gpu_availability()
        filtered = []
        for g in all_gpus:
            vram = g.get("memoryInGb") or 0
            if vram >= 24 and g.get("id", "").startswith("NVIDIA "):
                offer = gpu_offer_details(g, selected_cloud)
                # A past failed booking is a fallback only. Fresh live stock
                # wins; a failure in another cloud says nothing about this one.
                if (offer["availability"] == "unknown" and g["id"] in unavailable
                        and selected_cloud == cfg.get("cloud_type", "ALL").upper()):
                    offer.update({
                        "availability": "unavailable",
                        "availability_label": "Letzter Buchungsversuch: ausgebucht",
                        "availability_source": "recent_booking_failure",
                        "available": False,
                    })
                filtered.append({
                    "id": g["id"],
                    "displayName": g["displayName"],
                    "vram": vram,
                    "communityPrice": g.get("communityPrice"),
                    "securePrice": g.get("securePrice"),
                    **offer,
                })
        availability_order = {"available": 0, "limited": 1, "unknown": 2, "unavailable": 3}
        filtered.sort(key=lambda x: (availability_order[x["availability"]],
                                    x["hourly_price"] if x["hourly_price"] is not None else float("inf")))
        return {
            "gpus": filtered,
            "cloud_type": selected_cloud,
            "selected_gpu_id": cfg.get("gpu_type_id"),
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "availability_notice": "RunPod-Momentaufnahme für 1 GPU; Verfügbarkeit und Preis können "
                                   "sich bis zur Buchung ändern. Speicher- und Standortanforderungen "
                                   "können die Auswahl weiter einschränken. Katalogpreise sind keine "
                                   "Buchungszusage; Datenträgerkosten kommen hinzu.",
        }
    except Exception as e:
        # requests exceptions can include the authenticated URL. Never pass
        # that URL (and therefore the API key) through to the browser.
        message = str(e)
        api_key = getattr(client, "api_key", "")
        if api_key:
            message = message.replace(api_key, "[redacted]")
        raise HTTPException(status_code=500, detail=message)

@app.post("/api/gpus/availability/reset")
def reset_gpu_availability():
    """Setzt die per fehlgeschlagener Buchung gelernten 'ausgebucht'-Markierungen zurück."""
    orchestrator.reset_gpu_availability()
    return {"success": True}

@app.post("/api/pod/start")
def start_pod():
    res = orchestrator.start_pod()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/api/pod/stop")
def stop_pod():
    res = orchestrator.stop_pod()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/api/pod/terminate")
def terminate_pod():
    res = orchestrator.terminate_pod()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.get("/api/videos")
def list_videos():
    folder = get_outputs_dir()
    files = []
    for f in os.listdir(folder):
        if f.lower().endswith((".mp4", ".png", ".jpg")):
            path = os.path.join(folder, f)
            stat = os.stat(path)
            files.append({
                "filename": f,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": stat.st_mtime,
                "url": f"/api/videos/file/{urllib.parse.quote(f)}"
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"videos": files}

@app.get("/api/videos/file/{filename}")
def get_video_file(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(get_outputs_dir(), safe_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    return FileResponse(path)

@app.post("/api/videos/open-folder")
def open_folder():
    folder = get_outputs_dir()
    try:
        if sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/account/refresh")
def refresh_account(set_balance: float | None = None):
    if set_balance is not None and (not math.isfinite(set_balance) or set_balance < 0):
        raise HTTPException(status_code=400, detail="Kontostand muss eine endliche, nicht negative Zahl sein.")
    cfg = load_config()
    client = orchestrator.get_client()
    
    email = cfg.get("account_email", "")
    try:
        user_info = client.get_myself()
        if user_info and "email" in user_info:
            email = user_info["email"]
            update_config({"account_email": email})
    except Exception:
        pass

    # Network I/O above must not hold the lifecycle lock. Only after it has
    # finished, read the current accounting state and persist the rebase as one
    # transaction relative to stop/terminate and the background cost update.
    with orchestrator.lock:
        cfg = load_config()
        spent_total = float(cfg.get("spent_total", 0.0) or 0.0)
        accumulated = float(orchestrator.accumulated_cost or 0.0)
        if set_balance is not None:
            new_base = round(float(set_balance) + spent_total + accumulated, 4)
            update_config({"account_balance": new_base})
            balance = round(set_balance, 2)
        else:
            base_balance = float(cfg.get("account_balance", DEFAULT_CONFIG.get("account_balance", 0.0)))
            balance = max(0.0, round(base_balance - spent_total - accumulated, 2))

    return {
        "success": True,
        "account": {
            "email": email,
            "balance": balance
        }
    }

@app.post("/api/videos/delete")
def delete_video_endpoint(model: VideoDeleteModel):
    safe_name = os.path.basename(model.filename)
    success = orchestrator.delete_video(safe_name)
    if not success:
        raise HTTPException(status_code=404, detail="Datei konnte lokal oder remote nicht gefunden werden")
    return {"success": True, "filename": safe_name}


def _masked_config(cfg: dict) -> dict:
    """Zentrale Aufbereitung von Konfigurationsantworten: Der API-Key wird
    vollständig entfernt und nur maskiert zurückgegeben (GET und POST gleich)."""
    safe = {k: v for k, v in cfg.items() if k != "runpod_api_key"}
    key = cfg.get("runpod_api_key", "")
    safe["runpod_api_key_masked"] = key[:6] + "..." + key[-4:] if len(key) > 10 else ("***" if key else "")
    return safe


@app.get("/api/config")
def get_config_endpoint():
    return _masked_config(load_config())


@app.post("/api/config")
def update_config_endpoint(model: ConfigUpdateModel):
    updates = {k: v for k, v in model.model_dump().items() if v is not None}
    # Der Kontostand ist KEINE Einstellung: Routine-Saves dürfen das Guthaben
    # nicht verändern (der Wert im Dialog ist ein berechneter Restbetrag).
    # Manuelle Korrektur nur über /api/account/refresh?set_balance=...
    updates.pop("account_balance", None)
    previous_cloud = load_config().get("cloud_type", "ALL").upper()
    updated = update_config(updates)
    if "cloud_type" in updates and updated["cloud_type"].upper() != previous_cloud:
        # Learned booking failures belong to the old cloud selection. A plain
        # settings save or a GPU list refresh must leave those marks intact.
        orchestrator.reset_gpu_availability()
    orchestrator.log(f"Einstellungen aktualisiert: {list(updates.keys())}")
    return {"success": True, "config": _masked_config(updated)}

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await ws_manager.connect(websocket)
    # Send recent logs immediately
    state = orchestrator.get_state()
    for entry in state["recent_logs"]:
        await websocket.send_text(entry)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

# Serve Frontend static files
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>RunPod VideoGen Studio lädt...</h1>")

if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    port = int(cfg.get("server_port", 7880))
    # Default: nur lokal erreichbar. Für LAN-Zugriff "server_host": "0.0.0.0" in config.json setzen.
    host = cfg.get("server_host", "127.0.0.1")
    print(f"=======================================================")
    print(f"  RunPod VideoGen Studio startet auf {host}:{port}")
    print(f"  Öffne im Browser: http://localhost:{port}")
    print(f"=======================================================")
    uvicorn.run(app, host=host, port=port)
